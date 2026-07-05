from __future__ import annotations

from datetime import date as date_
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from database.bank_renc_model import BankStatementModel, MatchResultModel
from database.journal_model import (
    DrCr,
    EntryLineModel,
    FiscalPeriodModel,
    JournalEntryModel,
    PeriodAccountBalanceModel,
)

def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None

def approve_journal_entries(
    approved_entries: List[Dict[str, Any]],
    period_id:        int,
    db_session:       Session,
    run_id:           Optional[int] = None,
) -> Dict[str, Any]:
    posted, skipped, errors = 0, 0, []

    period = db_session.get(FiscalPeriodModel, period_id)
    if period is None:
        return {
            "posted": 0,
            "skipped": len(approved_entries),
            "errors": [f"FiscalPeriodModel id={period_id} not found"],
        }

    for entry in approved_entries:
        status = str(entry.get("status", "")).upper()
        if status not in {"APPROVED", "MODIFIED"}:
            skipped += 1
            continue

        dr_account = str(entry.get("debit_account", "")).strip()
        cr_account = str(entry.get("credit_account", "")).strip()
        amount     = _safe_float(entry.get("amount"))
        narration  = str(entry.get("entry_narrative") or entry.get("narration") or "").strip()
        bank_id    = entry.get("bank_id")

        if not dr_account or not cr_account or not amount:
            errors.append(
                f"Entry bank_id={bank_id}: missing debit_account, credit_account, "
                f"or amount - skipped."
            )
            skipped += 1
            continue

        try:
            je = JournalEntryModel(
                period_id=period.id,
                entry_date=date_.today(),
                voucher_type="Journal Voucher",
                narration=narration or f"Bank reconciliation entry - bank row {bank_id}",
                is_reconciliation_entry=True,
            )
            db_session.add(je)
            db_session.flush()  

            db_session.add(EntryLineModel(
                journal_entry_id=je.id, account_name=dr_account,
                dr_cr=DrCr.DEBIT, amount=amount, narration=narration,
            ))
            db_session.add(EntryLineModel(
                journal_entry_id=je.id, account_name=cr_account,
                dr_cr=DrCr.CREDIT, amount=amount, narration=narration,
            ))

            _update_period_balance(db_session, period.id, dr_account, amount, DrCr.DEBIT)
            _update_period_balance(db_session, period.id, cr_account, amount, DrCr.CREDIT)

            if run_id is not None:
                _mark_draft_posted(db_session, run_id, bank_id, je.id)

            posted += 1

        except SQLAlchemyError as exc:
            errors.append(f"Entry bank_id={bank_id}: {exc}")
            db_session.rollback()
            skipped += 1
            continue

    try:
        db_session.commit()
    except SQLAlchemyError as exc:
        db_session.rollback()
        errors.append(f"Final commit failed: {exc}")

    return {"posted": posted, "skipped": skipped, "errors": errors}


def _mark_draft_posted(
    session: Session, run_id: int, bank_id: Any, journal_entry_id: int,
) -> None:
    bank_row = (
        session.query(BankStatementModel)
        .filter_by(run_id=run_id, row_index=bank_id)
        .first()
    )
    if bank_row is None:
        return

    mr = (
        session.query(MatchResultModel)
        .filter_by(run_id=run_id, match_type="residual_draft", bank_statement_id=bank_row.id)
        .first()
    )
    if mr is None:
        return

    mr.details = (mr.details or "") + "  | POSTED"

    from ..database.journal_model import JournalEntryModel  
    je = session.get(JournalEntryModel, journal_entry_id)
    if je is not None:
        je.source_match_result_id = mr.id

def _update_period_balance(
    session:      Session,
    period_id:    int,
    account_name: str,
    amount:       float,
    dr_cr:        DrCr,
) -> None:
    existing = (
        session.query(PeriodAccountBalanceModel)
        .filter_by(period_id=period_id, account_name=account_name)
        .first()
    )

    if existing:
        if dr_cr == DrCr.DEBIT:
            if existing.normal_balance == DrCr.DEBIT:
                existing.closing_balance = (existing.closing_balance or 0) + amount
            else:
                existing.closing_balance = (existing.closing_balance or 0) - amount
        else:
            if existing.normal_balance == DrCr.CREDIT:
                existing.closing_balance = (existing.closing_balance or 0) + amount
            else:
                existing.closing_balance = (existing.closing_balance or 0) - amount
        existing.net_movement = (existing.net_movement or 0) + amount
    else:
        session.add(PeriodAccountBalanceModel(
            period_id=period_id,
            account_name=account_name,
            normal_balance=dr_cr,
            opening_balance=0.0,
            closing_balance=amount if dr_cr == DrCr.DEBIT else -amount,
            net_movement=amount,
        ))

def carry_forward_balances(
    session:          Session,
    closed_period_id: int,
    new_period_id:    int,
) -> int:
    closed_balances = (
        session.query(PeriodAccountBalanceModel)
        .filter_by(period_id=closed_period_id)
        .all()
    )
    count = 0
    for bal in closed_balances:
        session.add(PeriodAccountBalanceModel(
            period_id=new_period_id,
            account_name=bal.account_name,
            normal_balance=bal.normal_balance,
            opening_balance=bal.closing_balance,
            closing_balance=bal.closing_balance,
            net_movement=0.0,
        ))
        count += 1
    session.commit()
    return count
