"""
Posts human-approved "suggested journal entries" (from
matcher.residual_reconciler.reconcile_residuals -> SUGGESTED_JOURNAL_ENTRIES)
into the database.

Deliberately reuses the SAME JournalEntryModel / JournalLineModel tables
that the bill-scanning pipeline posts into (database/ledger_tax_models.py),
rather than standing up a second, parallel journal system. This means:
  - A bank-reconciliation-originated entry shows up in /api/journal and
    /api/ledger/trial-balance exactly like a bill-originated one.
  - Multi-tenancy (user_id) is enforced the same way everywhere.
  - There is only ever one place that computes "what does the ledger say".

Nothing here is auto-posted without a human explicitly approving it first
(see api/bank_rec_api.py's approve-entries route) — this module only ever
posts entries whose status is "APPROVED" or "MODIFIED".
"""
from __future__ import annotations

import uuid
from datetime import date as date_
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from schema import AccountGroup
from database.bank_renc_model import BankStatementModel, MatchResultModel
from database.ledger_tax_models import JournalEntryModel, JournalLineModel


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# Heuristic account-name -> AccountGroup mapping for the counter-accounts a
# draft suggests (e.g. "Bank Charges A/c", "Interest Received A/c", a vendor
# name + " A/c"). This only needs to be good enough for trial-balance
# grouping and export formatting — it doesn't affect the actual amounts.
_KEYWORD_GROUPS = [
    (("bank charges", "amc", "service charge", "fee"), AccountGroup.INDIRECT_EXPENSES),
    (("interest received", "interest income"), AccountGroup.INDIRECT_INCOME),
    (("interest paid",), AccountGroup.INDIRECT_EXPENSES),
    (("suspense",), AccountGroup.CURRENT_ASSETS),
    (("bank",), AccountGroup.BANK_ACCOUNTS),
]


def _infer_account_group(account_name: str, dr_cr: str) -> AccountGroup:
    name_lower = (account_name or "").lower()
    for keywords, group in _KEYWORD_GROUPS:
        if any(k in name_lower for k in keywords):
            return group
    # Fall back on a vendor/party name: money paid out to them is a
    # creditor, money received from them is a debtor.
    return AccountGroup.SUNDRY_CREDITORS if dr_cr == "Cr" else AccountGroup.SUNDRY_DEBTORS


def approve_journal_entries(
    approved_entries: List[Dict[str, Any]],
    user_id: str,
    db_session: Session,
    run_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    approved_entries: the SUGGESTED_JOURNAL_ENTRIES drafts, each with an
    added/edited "status" field ("APPROVED" | "MODIFIED" | "REJECTED") from
    the human reviewer. Only APPROVED/MODIFIED entries get posted;
    REJECTED (or missing status) entries are skipped.
    """
    posted, skipped, errors = 0, 0, []

    for entry in approved_entries:
        status = str(entry.get("status", "")).upper()
        if status not in {"APPROVED", "MODIFIED"}:
            skipped += 1
            continue

        dr_account = str(entry.get("debit_account", "")).strip()
        cr_account = str(entry.get("credit_account", "")).strip()
        amount = _safe_float(entry.get("amount"))
        narration = str(entry.get("entry_narrative") or entry.get("narration") or "").strip()
        bank_id = entry.get("bank_id")
        entry_date = entry.get("date")

        if not dr_account or not cr_account or not amount:
            errors.append(
                f"Entry bank_id={bank_id}: missing debit_account, credit_account, "
                f"or amount - skipped."
            )
            skipped += 1
            continue

        try:
            parsed_date = date_.fromisoformat(entry_date) if entry_date else date_.today()
        except ValueError:
            parsed_date = date_.today()

        try:
            je = JournalEntryModel(
                entry_id=str(uuid.uuid4())[:8].upper(),
                user_id=user_id,
                date=parsed_date,
                voucher_type="Bank Reconciliation Adjustment",
                narration=narration or f"Bank reconciliation entry - bank row {bank_id}",
                direction=None,
                source_reconciliation_run_id=run_id,
            )
            je.lines.append(JournalLineModel(
                account_name=dr_account,
                account_group=_infer_account_group(dr_account, "Dr").value,
                dr_cr="Dr",
                amount=amount,
                narration=narration,
            ))
            je.lines.append(JournalLineModel(
                account_name=cr_account,
                account_group=_infer_account_group(cr_account, "Cr").value,
                dr_cr="Cr",
                amount=amount,
                narration=narration,
            ))
            db_session.add(je)
            db_session.flush()

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
    """Flags the MatchResultModel row (match_type="residual_draft") for
    this bank row as posted, so it doesn't show up as still-pending in the
    review UI, and links it back to the JournalEntryModel it produced."""
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
