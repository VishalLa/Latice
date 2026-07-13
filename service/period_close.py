from __future__ import annotations

import uuid
from datetime import date as date_, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from schema import AccountGroup, JournalEntry
from database.ledger_tax_models import JournalEntryModel, JournalLineModel
from database.period_model import FiscalPeriod, PeriodAccountBalance
from .rebuild_ledger_data import rebuild_journal_entries
from ledger import build_ledger, trial_balance

# Accounts in these groups are nominal (P&L) accounts — their balances are
# transferred to Trading A/c / Profit & Loss A/c on close and start the next
# period at zero. Everything else is a real (balance-sheet) account, whose
# closing balance simply carries forward as-is.
_NOMINAL_GROUPS = {
    AccountGroup.SALES_ACCOUNTS,
    AccountGroup.DIRECT_INCOME,
    AccountGroup.INDIRECT_INCOME,
    AccountGroup.PURCHASE_ACCOUNTS,
    AccountGroup.DIRECT_EXPENSES,
    AccountGroup.INDIRECT_EXPENSES,
}


class PeriodAlreadyClosedError(Exception):
    pass


def _default_period_start(period_end: date_, period_type: str) -> date_:
    if period_type == "quarter":
        q_start_month = ((period_end.month - 1) // 3) * 3 + 1
        return date_(period_end.year, q_start_month, 1)
    if period_type == "year":
        return date_(period_end.year, 4, 1) if period_end.month >= 4 else date_(period_end.year - 1, 4, 1)
    # default: month
    return date_(period_end.year, period_end.month, 1)


def _journal_entry_to_model(entry: JournalEntry, user_id: str) -> JournalEntryModel:
    je = JournalEntryModel(
        entry_id=entry.entry_id or str(uuid.uuid4())[:8].upper(),
        user_id=user_id,
        date=entry.date,
        voucher_type=entry.voucher_type,
        narration=entry.narration,
        source_file=entry.source_file or None,
        invoice_number=entry.invoice_number or None,
        vendor_name=entry.vendor_name or None,
        direction=entry.direction or None,
    )
    for line in entry.lines:
        je.lines.append(JournalLineModel(
            account_name=line.account.name,
            account_group=line.account.group.value,
            dr_cr=line.dr_cr.value,
            amount=line.amount,
            narration=line.narration or "",
        ))
    return je


def close_period(
    session: Session,
    user_id: str,
    period_end: date_,
    period_label: str,
    financial_year: str,
    period_type: str = "month",
    period_start: Optional[date_] = None,
    closed_by: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Close the books for one period for a single user:

    1. Rebuild the general ledger from every JournalEntryModel up to and
       including period_end (this already includes reconciliation-posted
       entries via approve_journal_entries, since those write into the same
       JournalEntryModel table).
    2. Run ledger.journal.close_books to compute Trading A/c / P&L A/c /
       Capital-transfer closing entries and gross/net profit.
    3. Post those closing entries as a real JournalEntryModel (so they show
       up in future trial balances exactly like any other entry).
    4. Snapshot every account's closing balance as of period_end into
       PeriodAccountBalance, and mark the FiscalPeriod row closed.

    Raises PeriodAlreadyClosedError if this (financial_year, period_label)
    has already been closed for this user.
    """
    existing = (
        session.query(FiscalPeriod)
        .filter(
            FiscalPeriod.financial_year == financial_year,
            FiscalPeriod.period_label == period_label,
        )
        .first()
    )
    if existing is not None and existing.is_closed:
        raise PeriodAlreadyClosedError(
            f"Period {period_label!r} ({financial_year}) is already closed "
            f"(closed_at={existing.closed_at})."
        )

    period_start = period_start or _default_period_start(period_end, period_type)

    # --- 1. Rebuild the ledger up to period_end -----------------------
    q = session.query(JournalEntryModel).filter(
        JournalEntryModel.user_id == user_id,
        JournalEntryModel.date <= period_end,
    )
    models = q.order_by(JournalEntryModel.date.asc()).all()
    entries: List[JournalEntry] = rebuild_journal_entries(models)

    # --- 2. Compute closing entries -------------------------------------
    gl, all_entries, closing_result = build_ledger(
        [],
        _prebuilt_entries=entries,
        close_books_on=period_end,
        period_label=period_label,
    )

    # --- 3. Post the closing entries to the ledger ----------------------
    posted_entry_ids: List[str] = []
    if closing_result is not None and closing_result.entries:
        try:
            for closing_entry in closing_result.entries:
                je_model = _journal_entry_to_model(closing_entry, user_id)
                session.add(je_model)
                session.flush()
                posted_entry_ids.append(je_model.entry_id)
        except SQLAlchemyError as exc:
            session.rollback()
            raise

        # Re-run the ledger with the closing entries included so the
        # per-account snapshot below reflects post-closing balances
        # (nominal accounts should now be zero).
        entries = entries + closing_result.entries
        gl, all_entries, _ = build_ledger([], _prebuilt_entries=entries)

    # --- 4. Snapshot balances + mark the period closed -------------------
    tb = trial_balance(gl, as_on=period_end)

    if existing is not None:
        period_row = existing
        # Clear any stale balances from a prior (re-)run before rewriting.
        for bal in list(period_row.account_balances):
            session.delete(bal)
    else:
        period_row = FiscalPeriod(
            financial_year=financial_year,
            period_type=period_type,
            period_label=period_label,
            period_start=period_start,
            period_end=period_end,
        )
        session.add(period_row)

    period_row.is_closed = True
    period_row.closed_at = datetime.utcnow()
    period_row.closed_by = closed_by
    period_row.net_profit = closing_result.net_profit if closing_result else 0.0
    period_row.books_closed = closing_result is not None
    period_row.notes = notes
    session.flush()

    for acc in gl.accounts:
        if not acc.postings:
            continue
        bal_amt, bal_side = acc.closing_balance
        session.add(PeriodAccountBalance(
            period_id=period_row.id,
            account_name=acc.name,
            account_group=acc.group.value,
            total_debits=acc.total_debits,
            total_credits=acc.total_credits,
            closing_balance=bal_amt,
            balance_side=bal_side,
            is_nominal=acc.group in _NOMINAL_GROUPS,
        ))

    session.commit()

    return {
        "period_id": period_row.id,
        "financial_year": financial_year,
        "period_label": period_label,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "books_closed": closing_result is not None,
        "gross_profit": closing_result.gross_profit if closing_result else 0.0,
        "net_profit": closing_result.net_profit if closing_result else 0.0,
        "closing_entry_ids": posted_entry_ids,
        "closing_warnings": closing_result.warnings if closing_result else [],
        "trial_balance": tb.to_dict(),
    }
