from __future__ import annotations

import os
import re
import dataclasses

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from database import (
    DatabaseManager,
    
    ReconciliationRunModel,
    LedgerFormatModel,
    BankStatementModel,
    MatchResultModel
)
from schema import (
    LedgerFormat as SchemaLedger,
    BankStatement as SchemaBank
)
from ._base import Base
from .reports.bank_recon_xlsx import write_bank_recon_xlsx
from .helper import _log_db_errors

class ResultBankRec:
    
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        base = Base(db_manager)
        self.db_manager = base.get_manager
    
    
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
                    values.update(ResultBankRec._split_match_ids(item.get("ledger_id") or item.get("bank_id")))
                else:
                    values.update(ResultBankRec._split_match_ids(item))
            return values

        text = str(raw).strip()
        if not text or text == "—":
            return set()
        
        return {
            part.strip() 
            for part in text.split("&") 
            if part.strip()
        }
        
    
    @staticmethod
    def _report_filename(run_id: str) -> str:
        return f"bank-recon-{run_id}.xlsx"
    
    def _report_path(self, run_id: str) -> str:
        from core.config import Config
        return os.path.join(Config().from_env().STORAGE_DIR, self._report_filename(run_id))
    
        
    
    @staticmethod
    @_log_db_errors("finding internal run record")
    def _find_run_internal(
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
    
    
    @staticmethod
    @_log_db_errors("building match results")
    def _build_matches(
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
    
    
    @staticmethod
    @_log_db_errors("building ledger objects")
    def _build_ledger_objs(
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
    
    
    @staticmethod
    @_log_db_errors("building bank objects")
    def _build_bank_objs(
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
                debit_amount=bs.debit,
                credit_amount=bs.credit,
                balance=bs.balance,
                txn_id=bs.txn_id,
                parse_warnings=bs.parse_warnings or [],
                run_id=str(bs.run_id) if bs.run_id is not None else None,
            ))
        return bank_objs
    
    
    @staticmethod
    def _build_run_bundle(
        session: Session,
        run: ReconciliationRunModel
    ) -> Tuple[Dict[str, Any], List[SchemaLedger], List[SchemaBank]]:
        matches = ResultBankRec._build_matches(run=run)
        gl_objs = ResultBankRec._build_ledger_objs(session=session, run=run)
        bank_objs = ResultBankRec._build_bank_objs(session=session, run=run)
        
        matched_ledger_ids = set()
        matched_bank_ids = set()
        
        for match in matches:
            matched_ledger_ids.update(
                ResultBankRec._split_match_ids(raw=match.get("ledger_id"))
            )
            matched_bank_ids.update(
                ResultBankRec._split_match_ids(raw=match.get("bank_id"))
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
    
    
    @staticmethod
    @_log_db_errors("fetching internal run bundle")
    def _fetch_run_bundle_internal(
        session: Session, 
        run_id: str, 
        user_id: Optional[str] = None
    ) -> Optional[Tuple[Dict[str, Any], List[SchemaLedger], List[SchemaBank]]]:
        
        run = ResultBankRec._find_run_internal(
            session=session,
            run_id=run_id,
            user_id=user_id
        )
        if run is None:
            return None

        return ResultBankRec._build_run_bundle(session=session, run=run)

    
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
    
    
    @_log_db_errors("getting run result for celery")
    def get_data_from_db(
        self,
        run_id: str,
        user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        
        def _op(session: Session) -> Optional[Dict[str, Any]]:
            recon_result, _, _ = self._fetch_run_bundle_internal(
                session=session,
                run_id=run_id,
                user_id=user_id
            )
            return recon_result
        
        fetched = self.db_manager.run(_op)
        if fetched is None:
            return {
                "status": "error", 
                "message": "Reconciliation run not found or unauthorized."
            }
        return fetched
    
    
    @_log_db_errors("generating report for celery")
    def generate_report_from_db(
        self,
        run_id: str,
        user_id: Optional[str] = None
    ):
        def _op(session: Session) -> dict:
            return self._fetch_run_bundle_internal(
                session=session,
                run_id=run_id,
                user_id=user_id
            )
        
        fetched = self.db_manager.run(_op)
        if fetched is None:
            return {
                "status": "error", 
                "message": "Reconciliation run not found or unauthorized."
            }
            
        out_path = self._report_path(run_id)
        recon_result, gl_objs, bank_objs = fetched
        write_bank_recon_xlsx(recon_result, gl_objs, bank_objs, out_path)
        return {
            "status": "success",
            "run_id": run_id,
            "file": self._report_filename(run_id),
            "path": out_path,
        }

    
    @_log_db_errors("getting run result for api no celery")
    def get_run_result(
        self, 
        run_id: str, 
        user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        
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

            recon_result, _, _ = bundle
            
            return {
                "run_id": public_run_id,
                "state": "SUCCESS",
                "summary": recon_result.get("summary", {}),
                "reconciliation_data": recon_result,
                "download_url": f"/download_report/run/{public_run_id}",
            }
            
        return self.db_manager.run(_op)
    
    
    # per-user run history
    @staticmethod
    def _public_run_id(run: ReconciliationRunModel) -> str:
        return run.celery_task_id or str(run.id)
    
    
    @staticmethod
    def _state_from_status(status: Optional[str]) -> str:
        s = (status or "processing").lower()
        return {
            "success": "SUCCESS",
            "failed": "FAILURE"
        }.get(s, "STARTED")
        
    
    @staticmethod
    def _to_plain(obj: Any) -> Any:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        if hasattr(obj, "to_dict") and callable(obj.to_dict):
            return obj.to_dict()
        if isinstance(obj, dict):
            return {k: ResultBankRec._to_plain(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [ResultBankRec._to_plain(v) for v in obj]
        return obj

    
    @staticmethod
    def _find_owned_run_internal(
        session: Session,
        user_id: str,
        run_id: str
    ) -> Optional[ReconciliationRunModel]:
        return (
            session.query(ReconciliationRunModel).options(
                selectinload(ReconciliationRunModel.match_results).options(
                    joinedload(MatchResultModel.ledger_format),
                    joinedload(MatchResultModel.bank_statement)
                )
            ).filter(
                ReconciliationRunModel.user_id == user_id,
                or_(
                    ReconciliationRunModel.celery_task_id == str(run_id),
                    ReconciliationRunModel.id == str(run_id)
                )
            ).order_by(ReconciliationRunModel.run_at.desc()).first()
        )
        
    
    @staticmethod
    def _run_list_item(run: ReconciliationRunModel) -> Dict[str, Any]:
        public_id = ResultBankRec._public_run_id(run)
        return {
            "run_id": public_id,
            "name": run.name or f"Run {public_id[:8]}",
            "state": ResultBankRec._state_from_status(run.status),
            "status": run.status,
            "error_message": run.error_message,
            "bank_name": run.bank_name,
            "run_at": run.run_at.isoformat() if run.run_at else None,
            "ledger_records": run.ledger_records,
            "bank_records": run.bank_records,
            "exact_matches": run.exact_matches,
            "fuzzy_matches": run.fuzzy_matches,
            "ai_matches": run.ai_matches,
            "unreconciled_ledger": run.unreconciled_ledger,
            "unreconciled_bank": run.unreconciled_bank,
        }
        
        
    @_log_db_errors("listing runs for user")
    def list_runs_for_user(
        self,
        user_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))

        def _op(session: Session) -> List[Dict[str, Any]]:
            runs = session.query(ReconciliationRunModel).filter(
                ReconciliationRunModel.user_id == user_id
            ).order_by(
                ReconciliationRunModel.run_at.desc(),
                ReconciliationRunModel.id
            ).limit(limit).offset(offset).all()
            return [ResultBankRec._run_list_item(run) for run in runs]
    
        return self.db_manager.run(_op)
    
    
    @_log_db_errors("getting run for user")
    def get_run_for_user(
        self,
        user_id: str,
        run_id: str,
        include_records: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if not user_id:
            return None
        
        def _op(session: Session) -> Optional[Dict[str, Any]]:
            run = ResultBankRec._find_owned_run_internal(
                session=session,
                user_id=user_id,
                run_id=run_id
            )
            if run is None:
                return None
            
            recon_result, gl_objs, bank_objs = ResultBankRec._build_run_bundle(
                session=session,
                run=run,
            )
            public_id = ResultBankRec._public_run_id(run)
            state = ResultBankRec._state_from_status(run.status)
            payload: Dict[str, Any] = {
                "run_id": public_id,
                "name": run.name or f"Run {public_id[:8]}",
                "state": state,
                "status": run.status,
                "error_message": run.error_message,
                "summary": recon_result["summary"],
                "reconciliation_data": ResultBankRec._to_plain(recon_result),
                "download_url": (
                    f"/download_report/run/{public_id}" if state == "SUCCESS" else None
                ),
            }
        
            if include_records:
                payload["ledger_records"] = ResultBankRec._to_plain(gl_objs)
                payload["bank_records"] = ResultBankRec._to_plain(bank_objs)
                
            return payload
        
        return self.db_manager.run(_op)
