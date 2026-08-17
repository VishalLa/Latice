from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from database import (
    DatabaseManager,
    
    ReconciliationRunModel,
    LedgerFormatModel,
    BankStatementModel
)
from schema import (
    LedgerFormat as SchemaLedger,
    BankStatement as SchemaBank
)

from .helper import _log_db_errors

class RunResult:
    
    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager
    
    
    @staticmethod
    def _parse_draft_details(details: Optional[str]) -> Dict[str, Optional[str]]:
        _DRAFT_DETAILS_RE = re.compile(
                r"^DRAFT \(([^,]+), confidence ([^,]+), source ([^)]+)\):\s*.*?\s-\s(.*)$",
                re.DOTALL,
            )
        if not details:
            return {
                "status": None,
                "confidence": None,
                "source": None,
                "entry_narrative": None
            }
            
        match = _DRAFT_DETAILS_RE.match(details.strip())
        if not match:
            return {
                "status": None, 
                "confidence": None, 
                "source": None, 
                "entry_narrative": details
            }
            
        status, confidence, source, narrative = match.groups()
        return {
            "status": status.strip(),
            "confidence": confidence.strip(),
            "source": source.strip(),
            "entry_narrative": narrative.strip(),
        }
        

    @staticmethod
    def _parse_dr_cr_accounts(adjustment_type: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        if not adjustment_type:
            return None, None

        parts =adjustment_type.split(" Dr / ")
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        
        return None, None

    
    @staticmethod
    def _split_match_ids(raw: Any) -> set[str]:
        if raw is None:
            return set()
        
        if isinstance(raw, list):
            values: set[str] = set()
            for item in raw:
                if isinstance(item, dict):
                    values.update(RunResult._split_match_ids(item.get("ledger_id") or item.get("bank_id")))
                else:
                    values.update(RunResult._split_match_ids(item))
            return values

        text = str(raw).strip()
        if not text or text == "—":
            return set()
        
        return {
            part.strip() 
            for part in text.split("&") 
            if part.strip()
        }
        
    
    @_log_db_errors("finding internal run record")
    def _find_run_internal(
        self, 
        session: Session, 
        run_id: str, 
        user_id: Optional[str] = None
    ) -> Optional[ReconciliationRunModel]:
        
        query = session.query(ReconciliationRunModel).filter(
            ReconciliationRunModel.celery_task_id == str(run_id)
        ).order_by(ReconciliationRunModel.id.desc())
        
        if user_id is not None:
            query = query.filter(ReconciliationRunModel.user_id == user_id)
            
        run = query.first()

        if run is None and user_id is not None:
            query = session.query(ReconciliationRunModel).filter(
                ReconciliationRunModel.celery_task_id == str(run_id),
                ReconciliationRunModel.user_id.is_(None)
            ).order_by(ReconciliationRunModel.id.desc())
            run = query.first()

        if run is None:
            try:
                pk = int(run_id)
            except (TypeError, ValueError):
                pk = None
                
            if pk is not None:
                fallback = session.query(ReconciliationRunModel).filter(
                    ReconciliationRunModel.id == pk
                )
                
                if user_id is not None:
                    fallback = fallback.filter(ReconciliationRunModel.user_id == user_id)
                
                run = fallback.first()
                if run is None and user_id is not None:
                    run = session.query(ReconciliationRunModel).filter(
                        ReconciliationRunModel.id == pk,
                        ReconciliationRunModel.user_id.is_(None)
                    ).first()

        return run
    
    
    @_log_db_errors("building match results")
    def _build_matches(
        self, 
        run: ReconciliationRunModel
    ) -> List[Dict[str, Any]]:
        matches: List[Dict[str, Any]] = []
        for mr in run.match_results:
            lid = mr.ledger_format.ledger_id if mr.ledger_format else None
            bid = str(mr.bank_statement.row_index) if mr.bank_statement else None
            matches.append({
                "id": mr.id,
                "ledger_id": lid,
                "bank_id": bid,
                "match_type": mr.match_type,
                "adjustment_type": mr.adjustment_type,
                "amount": mr.matched_amount,
                "date": mr.matched_date.isoformat() if mr.matched_date else None,
                "confidence_score": mr.confidence_score,
                "details": mr.details,
            })
        return matches
    
    
    @_log_db_errors("building ledger objects")
    def _build_ledger_objs(
        self, 
        session: Session, 
        run: ReconciliationRunModel
    ) -> List[SchemaLedger]:
        gl_objs: List[SchemaLedger] = []
        for lr in session.query(LedgerFormatModel).filter(LedgerFormatModel.run_id == run.id).all():
            gl_objs.append(SchemaLedger(
                account_name=lr.account_name or "",
                ledger_id=lr.ledger_id,
                account_number=lr.account_number,
                transaction_date=lr.transaction_date.isoformat() if lr.transaction_date else None,
                transaction_date_raw=lr.transaction_date_raw,
                debit_amount=lr.debit_amount,
                credit_amount=lr.credit_amount,
                reference_id=lr.reference_id,
                parse_warnings=lr.parse_warnings or [],
                source=lr.source.value if hasattr(lr.source, "value") else lr.source,
                journal_entry_id=lr.journal_entry_id,
                voucher_type=lr.voucher_type,
                vendor_name=lr.vendor_name,
                run_id=str(lr.run_id) if lr.run_id is not None else None,
            ))
        return gl_objs
    
    
    @_log_db_errors("building bank objects")
    def _build_bank_objs(
        self, 
        session: Session, 
        run: ReconciliationRunModel
    ) -> List[SchemaBank]:
        bank_objs: List[SchemaBank] = []
        for bs in session.query(BankStatementModel).filter(BankStatementModel.run_id == run.id).all():
            bank_objs.append(SchemaBank(
                row_index=bs.row_index,
                bank_name=bs.bank_name or "",
                template_version=bs.template_version or "",
                date=bs.date.isoformat() if bs.date else None,
                date_raw=bs.date_raw,
                narration=bs.narration or "",
                debit=bs.debit,
                credit=bs.credit,
                balance=bs.balance,
                txn_id=bs.txn_id,
                parse_warnings=bs.parse_warnings or [],
                run_id=str(bs.run_id) if bs.run_id is not None else None,
            ))
        return bank_objs
    
    
    @_log_db_errors("fetching internal run bundle")
    def _fetch_run_bundle_internal(
        self, 
        session: Session, 
        run_id: str, 
        user_id: Optional[str] = None
    ) -> Optional[Tuple[Dict[str, Any], List[SchemaLedger], List[SchemaBank]]]:
        
        run = self._find_run_internal(
            session=session, 
            run_id=run_id, 
            user_id=user_id
        )
        if run is None:
            return None

        matches = self._build_matches(run=run)
        gl_objs = self._build_ledger_objs(
            session=session, 
            run=run
        )
        bank_objs = self._build_bank_objs(
            session=session, 
            run=run
        )
        
        matched_ledger_ids = set()
        matched_bank_ids = set()
        
        for match in matches:
            matched_ledger_ids.update(
                self._split_match_ids(raw=match.get("ledger_id"))
            )
            matched_bank_ids.update(
                self._split_match_ids(raw=match.get("bank_id"))
            )

        unreconciled_gl_objs = [
            item 
            for item in gl_objs 
            if str(item.ledger_id) not in matched_ledger_ids
        ]
        unreconciled_bank_objs = [
            item 
            for item in bank_objs 
            if str(item.row_index) not in matched_bank_ids
        ]

        recon_result: Dict[str, Any] = {
            "summary": {
                "run_id": run.celery_task_id or str(run.id),
                "db_id": run.id,
                "status": run.status,
                "bank_name": run.bank_name,
                "template_version": run.template_version,
                "ledger_records": run.ledger_records,
                "bank_records": run.bank_records,
                "exact_matches": run.exact_matches,
                "fuzzy_matches": run.fuzzy_matches,
                "ai_matches": run.ai_matches,
                "unreconciled_ledger": run.unreconciled_ledger,
                "unreconciled_bank": run.unreconciled_bank,
                "run_at": run.run_at.isoformat() if run.run_at else None,
                "user_id": str(run.user_id) if run.user_id is not None else None,
                "error_message": run.error_message,
            },
            "EXACT_MATCHES": [m for m in matches if m.get("match_type") == "exact"],
            "FUZZY_MATCHES": [m for m in matches if m.get("match_type") == "fuzzy"],
            "MEMORY_MATCHES": [m for m in matches if m.get("match_type") == "memory"],
            "AI_MATCHES": [m for m in matches if m.get("match_type") in ("ai", "ai_queue")],
            "RESIDUAL_TIMING_MATCHES": [m for m in matches if m.get("match_type") == "residual_timing"],
            "RESIDUAL_SPLIT_MATCHES": [m for m in matches if m.get("match_type") == "residual_split"],
            "UNRECONCILED_ITEMS": {
                "ledger": unreconciled_gl_objs,
                "bank": unreconciled_bank_objs,
            },
        }
        return recon_result, gl_objs, bank_objs
    
    
    @_log_db_errors("finding run")
    def find_run(
        self, 
        run_id: str, 
        user_id: Optional[str] = None
    ) -> Optional[ReconciliationRunModel]:
        
        def _op(session: Session) -> Optional[ReconciliationRunModel]:
            return self._find_run_internal(
                session=session, 
                run_id=run_id, 
                user_id=user_id
            )
        
        return self.db_manager.run(_op)
    
    
    @_log_db_errors("marking run status")
    def mark_run_status(
        self, 
        run_id: str, 
        status: str, 
        error_message: Optional[str] = None
    ) -> bool:
        """Set status/error_message on a run row. Returns False if not found."""
        
        def _op(session: Session) -> bool:
            run = self._find_run_internal(
                session=session, 
                run_id=run_id
            )
            if run is None:
                return False
            
            run.status = status
            if error_message is not None:
                run.error_message = error_message

            return True
        
        return self.db_manager.run(_op)
    
    
    @_log_db_errors("fetching run bundle")
    def fetch_run_bundle(
        self, 
        run_id: str, 
        user_id: Optional[str] = None
    ) -> Optional[Tuple[Dict[str, Any], List[SchemaLedger], List[SchemaBank]]]:
        """
        Full reconstruction of a run's reconciliation payload — matches, ledger
        rows, bank rows. Returns None if the run isn't found.
        """
        def _op(session: Session) -> Optional[Tuple[Dict[str, Any], List[SchemaLedger], List[SchemaBank]]]:
            return self._fetch_run_bundle_internal(
                session=session, 
                run_id=run_id, 
                user_id=user_id
            )
        
        return self.db_manager.run(_op)
    
    
    @_log_db_errors("getting run result")
    def get_run_result(
        self, 
        run_id: str, 
        user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        GET /run_result/{run_id} — Shapes returned:
            {"run_id": ..., "state": "PENDING",    "message": ...}
            {"run_id": ..., "state": "STARTED",    "message": ...}
            {"run_id": ..., "state": "FAILURE",    "error": ...}
            {"run_id": ..., "state": "SUCCESS",    "summary": ..., "reconciliation_data": ..., "download_url": ...}
        """
        def _op(session: Session) -> Dict[str, Any]:
            run = self._find_run_internal(
                session=session, 
                run_id=run_id, 
                user_id=user_id
            )
            
            if run is None:
                return {
                    "run_id": run_id,
                    "state": "PENDING",
                    "message": "Unknown run_id or not started yet.",
                }

            status = (run.status or "processing").lower()
            public_run_id = run.celery_task_id or str(run.id)

            if status == "failed":
                return {
                    "run_id": public_run_id,
                    "state": "FAILURE",
                    "error": run.error_message or "Reconciliation run failed.",
                }

            if status != "success":
                return {
                    "run_id": public_run_id,
                    "state": "STARTED",
                    "message": "Still processing.",
                }

            bundle = self._fetch_run_bundle_internal(
                session=session, 
                run_id=run_id, 
                user_id=user_id
            )
            if bundle is None:
                # Row existed a moment ago, treat as not-found rather than raising
                return {
                    "run_id": public_run_id,
                    "state": "PENDING",
                    "message": "Unknown run_id or not started yet.",
                }

            recon_result, _gl_objs, _bank_objs = bundle
            
            return {
                "run_id": public_run_id,
                "state": "SUCCESS",
                "summary": recon_result.get("summary", {}),
                "reconciliation_data": recon_result,
                "download_url": f"/download_report/run/{public_run_id}",
            }
            
        return self.db_manager.run(_op)
    
