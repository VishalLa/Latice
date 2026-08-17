from __future__ import annotations

import uuid
from datetime import date as date_
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from schema import AccountGroup
from database import (
    DatabaseManager,
    
    BankStatementModel,
    MatchResultModel,
    JournalEntryModel,
    JournalLineModel
)

from .helper import _log_db_errors, _safe_float


class RecJournalPosting:
    
    _KEYWORD_GROUPS = [
        (("bank charges", "amc", "service charge", "fee"), AccountGroup.INDIRECT_EXPENSES),
        (("interest received", "interest income"),         AccountGroup.INDIRECT_INCOME),
        (("interest paid",),                               AccountGroup.INDIRECT_EXPENSES),
        (("suspense",),                                    AccountGroup.CURRENT_ASSETS),
        (("bank",),                                        AccountGroup.BANK_ACCOUNTS),
    ]

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager
    
    
    def _infer_account_group(
        self,
        account_name: str,
        dr_cr: str
    ) -> AccountGroup:
        name_lower = (account_name or "").lower()
        for keywords, group in self._KEYWORD_GROUPS:
            if any(k in name_lower for k in keywords):
                return group
            
        return AccountGroup.SUNDRY_CREDITORS if dr_cr == "Cr" else AccountGroup.SUNDRY_DEBTORS
    
    
    @_log_db_errors("approving and posting journal entries")
    def approve_journal_entries(
        self,
        approved_entries: List[Dict[str, Any]],
        user_id: str,
        run_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        approved_entries: the SUGGESTED_JOURNAL_ENTRIES drafts, each with an
        added/edited "status" field ("APPROVED" | "MODIFIED" | "REJECTED") from
        the human reviewer. Only APPROVED/MODIFIED entries get posted;
        REJECTED (or missing status) entries are skipped.
        """
        
        def _op(session: Session) -> Dict[str, Any]:
            posted, skipped, errors = 0, 0, []
            
            for entry in approved_entries:
                status = str(entry.get("status", "")).upper()
                if status not in {"APPROVED", "MODIFIED"}:
                    skipped += 1
                    continue
                
                dr_account = str(entry.get("debit_account", "")).strip()
                cr_account = str(entry.get("credit_account", "")).strip()
                amount =     _safe_float(entry.get("amount"))
                narration =  str(entry.get("entry_narrative") or entry.get("narration") or "").strip()
                bank_id =    entry.get("bank_id")
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
                    with session.begin_nested():
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
                            account_group=self._infer_account_group(dr_account, "Dr").value,
                            dr_cr="Dr",
                            amount=amount,
                            narration=narration,
                        ))
                        
                        je.lines.append(JournalLineModel(
                            account_name=cr_account,
                            account_group=self._infer_account_group(cr_account, "Cr").value,
                            dr_cr="Cr",
                            amount=amount,
                            narration=narration,
                        ))
                        
                        session.add(je)
                        session.flush()

                        if run_id is not None:
                            self._mark_draft_posted_internal(session, run_id, bank_id, je.id)

                    posted += 1

                except SQLAlchemyError as exc:
                    errors.append(f"Entry bank_id={bank_id}: {exc}")
                    skipped += 1
                    continue

            return {"posted": posted, "skipped": skipped, "errors": errors}

        return self.db_manager.run(_op)


    @_log_db_errors("marking reconciliation draft as posted")
    def _mark_draft_posted_internal(
        self,
        session: Session,
        run_id: int,
        bank_id: Any,
        journal_entry_id: int
    ) -> None:
        """
        Flags the MatchResultModel row (match_type="residual_draft") for
        this bank row as posted, so it doesn't show up as still-pending in the
        review UI, and links it back to the JournalEntryModel it produced.
        """
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

