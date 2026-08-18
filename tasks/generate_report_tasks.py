from __future__ import annotations

from typing import Optional, Dict, Any

from service import GenerateReports, ResultBankRec
from app.celery import celery_app


_generate_report: Optional[GenerateReports] = None
_run_result: Optional[ResultBankRec] = None

def get_generate_report_serivce():
    global _generate_report
    if _generate_report is None:
        _generate_report = GenerateReports()
    return _generate_report

def get_run_result_service():
    global _run_result
    if _run_result is None:
        _run_result = ResultBankRec()
    return _run_result



@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.get_data_from_db_for_bank_rec"
)
def get_data_from_db(
    self,
    run_id = str,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    
    run_result = get_run_result_service()
    
    try:
        return run_result.get_data_from_db(
            run_id=run_id,
            user_id=user_id
        )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
    
    
@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.generate_bank_rec_report"
)
def generate_report_bank_rec(
    self,
    run_id: str,
    user_id: Optional[str] =None
) -> Dict[str, Any]:
    reprot_service = get_generate_report_serivce()
    
    try:
        path, is_ok = reprot_service.export_bank_rec_report(
            run_id=run_id,
            user_id=user_id
        )
        if not is_ok:
            return {
                "status": "error", 
                "message": path
            }
        
        return {
            "status": "success",
            "run_id": run_id,
            "file": f"bank_rec_{run_id}",
            "path": path
        }
    
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
    