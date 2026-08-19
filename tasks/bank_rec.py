from __future__ import annotations

from typing import Optional, Dict, List, Any

from app.celery import celery_app
from service import RunBankRec

_run_bank_rec: Optional[RunBankRec] = None

def get_run_bank_rec():
    global _run_bank_rec
    if _run_bank_rec is None:
        _run_bank_rec = RunBankRec()
    return _run_bank_rec


@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.process_pre_data_for_bank_rec"
)
def process_pre_data(
    self,
    statements_data: List[Dict[str, Any]],
    ledgers_data: List[Dict[str, Any]],
) -> Dict[str, str]:
    try:
        return get_run_bank_rec().process_pre_data(
            statements_data=statements_data,
            ledgers_data=ledgers_data
        )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
    
    
@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.process_post_data_for_bank_rec"
)
def process_post_data(
    self,
    matches_data: List[Dict[str, Any]], 
    ignored_data: List[Dict[str, Any]], 
    audit_data: List[Dict[str, Any]]
) -> Dict[str, str]:
    try:
        return get_run_bank_rec().process_post_data(
            matches_data=matches_data,
            ignored_data=ignored_data,
            audit_data=audit_data
        )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.run_bank_rec"
)
def run_reconciliation_pipeline(
    self,
    ledger_path: str,
    bank_path: str
) -> Dict[str, Any]:
    run_bank_rec = get_run_bank_rec()
    
    try:
        return run_bank_rec.run_reconciliation_pipeline(
            run_id=self.request.id,
            ledger_path=ledger_path,
            bank_path=bank_path
        )
    except Exception as exc:
        max_retries = self.max_retries if self.max_retries is not None else 3
        if self.request.retries >= max_retries:
            run_bank_rec._cleanup_input_files()
        raise self.retry(exc=exc, countdown=10)

