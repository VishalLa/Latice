from __future__ import annotations

import uuid
from datetime import date as date_, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from schema import (
    Account,
    AccountGroup,
    DrCr,
    EntryLine,
    JournalEntry,
    TDSEntry,
    DeducteeType,
    TDSRegister
)

from database import (
    DatabaseManager,
    JournalEntryModel,
    JournalLineModel,
    TDSEntryModel,
    FiscalPeriod,
    PeriodAccountBalance
)

from ledger import LedgerBuilder


class PeriodAlreadyClosedError(Exception):
    pass


class RebuildService:
    """
    Rebuilds domain objects (JournalEntry, TDSEntry) from their persisted
    SQLAlchemy models, and closes accounting periods.

    The rebuild_* methods are pure — no DB access, no side effects, just
    model → domain-object conversion — so they're staticmethods. close_period
    is the one method that actually needs a database session, which is why
    `db_manager` is injected in __init__: it opens and manages its own
    transaction via `db_manager.session_scope()` rather than requiring the
    caller to pass a session in.
    """

    _NOMINAL_GROUPS = {
        AccountGroup.SALES_ACCOUNTS,
        AccountGroup.DIRECT_INCOME,
        AccountGroup.INDIRECT_INCOME,
        AccountGroup.PURCHASE_ACCOUNTS,
        AccountGroup.DIRECT_EXPENSES,
        AccountGroup.INDIRECT_EXPENSES,
    }

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager


    @staticmethod
    def _account_group_from_value(value: Optional[str]) -> AccountGroup:
        _GROUP_BY_VALUE = {g.value: g for g in AccountGroup}
        return _GROUP_BY_VALUE.get(value or "", AccountGroup.CURRENT_ASSETS)


    @staticmethod
    def rebuild_journal_entry(model: JournalEntryModel) -> JournalEntry:
        lines = [
            EntryLine(
                account=Account(name=l.account_name, group=RebuildService._account_group_from_value(l.account_group)),
                dr_cr=DrCr(l.dr_cr),
                amount=l.amount,
                narration=l.narration or "",
            )
            for l in model.lines
        ]
        return JournalEntry(
            entry_id=model.entry_id,
            date=model.date,
            voucher_type=model.voucher_type,
            narration=model.narration,
            lines=lines,
            source_file=model.source_file or "",
            invoice_number=model.invoice_number or "",
            vendor_name=model.vendor_name or "",
            direction=model.direction or "",
        )


    @staticmethod
    def rebuild_journal_entries(models: List[JournalEntryModel]) -> List[JournalEntry]:
        return sorted(
            (RebuildService.rebuild_journal_entry(m) for m in models), key=lambda e: e.date
        )


    @staticmethod
    def rebuild_tds_entry(model: TDSEntryModel) -> TDSEntry:
        return TDSEntry(
            section_code=model.section_code,
            deductee_name=model.deductee_name,
            gross_amount=model.gross_amount,
            tds_base=model.tds_base,
            tds_rate=model.tds_rate,
            tds_amount=model.tds_amount,
            net_payment=model.net_payment,
            entry_id=model.entry_id,
            date=model.date,
            deductee_pan=model.deductee_pan,
            deductee_type=DeducteeType(model.deductee_type),
            deductee_gstin=model.deductee_gstin,
            source_journal_id=model.journal_entry.entry_id if model.journal_entry else None,
            invoice_number=model.invoice_number,
            rate_enhanced_206aa=model.rate_enhanced_206aa,
            deposit_date=model.deposit_date,
            challan_bsr_code=model.challan_bsr_code,
            challan_serial=model.challan_serial,
            challan_date=model.challan_date,
        )


    @staticmethod
    def rebuild_tds_register(
        models: List[TDSEntryModel],
        period_start: date_,
        period_end: date_,
    ) -> TDSRegister:
        entries = [
            RebuildService.rebuild_tds_entry(m) for m in models
        ]
        return TDSRegister(
            entries=entries,
            period_start=period_start,
            period_end=period_end
        )


    @staticmethod
    def _default_period_start(
        period_end: date_,
        period_type: str
    ) -> date_:
        if period_type == "quarter":
            q_start_month = ((period_end.month - 1) // 3) * 3 + 1
            return date_(period_end.year, q_start_month, 1)

        if period_type == "year":
            return date_(period_end.year, 4, 1) if period_end.month >= 4 else date_(period_end.year - 1, 4, 1)
        # default: month
        return date_(period_end.year, period_end.month, 1)


    @staticmethod
    def _journal_entry_to_model(
        entry: JournalEntry, 
        user_id: str
    ) -> JournalEntryModel:
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
        self,
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
           entries via approve_journal_entries, since those write into the
           same JournalEntryModel table).
        2. Run LedgerBuilder.build(..., close_books_on=period_end) to compute
           Trading A/c / P&L A/c / Capital-transfer closing entries and
           gross/net profit.
        3. Post those closing entries as a real JournalEntryModel (so they
           show up in future trial balances exactly like any other entry).
        4. Snapshot every account's closing balance as of period_end into
           PeriodAccountBalance, and mark the FiscalPeriod row closed.

        Manages its own transaction via self.db_manager.session_scope() —
        commits on success, rolls back on any exception, including
        PeriodAlreadyClosedError.

        Raises PeriodAlreadyClosedError if this (financial_year, period_label)
        has already been closed for this user.
        """
        with self.db_manager.session_scope() as session:
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

            period_start = period_start or self._default_period_start(period_end, period_type)

            # 1. Rebuild the ledger up to period_end
            q = session.query(JournalEntryModel).filter(
                JournalEntryModel.user_id == user_id,
                JournalEntryModel.date <= period_end,
            )
            models = q.order_by(JournalEntryModel.date.asc()).all()
            entries: List[JournalEntry] = self.rebuild_journal_entries(models)

            # 2. Compute closing entries
            builder, all_entries, closing_result = LedgerBuilder.build(
                [],
                _prebuilt_entries=entries,
                close_books_on=period_end,
                period_label=period_label,
            )

            # 3. Post the closing entries to the ledger
            posted_entry_ids: List[str] = []
            if closing_result is not None and closing_result.entries:
                for closing_entry in closing_result.entries:
                    je_model = self._journal_entry_to_model(closing_entry, user_id)
                    session.add(je_model)
                    session.flush()
                    posted_entry_ids.append(je_model.entry_id)

                entries_with_closing = entries + closing_result.entries
                builder, all_entries, _ = LedgerBuilder.build([], _prebuilt_entries=entries_with_closing)

            # 4. Snapshot balances + mark the period closed
            tb = LedgerBuilder(gl=builder.gl, as_on=period_end).trial_balance()

            if existing is not None:
                period_row = existing
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

            for acc in builder.gl.accounts:
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
                    is_nominal=acc.group in self._NOMINAL_GROUPS,
                ))

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

