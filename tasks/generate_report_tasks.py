from __future__ import annotations

from datetime import date as date_
from typing import Optional, Dict, Any

from service import GenerateReports, ResultBankRec
from app.celery import celery_app


_generate_report: Optional[GenerateReports] = None
_run_result: Optional[ResultBankRec] = None

def get_generate_report_service():
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
    try:
        return get_run_result_service().get_data_from_db(
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
    try:
        path, is_ok = get_generate_report_service().export_bank_rec_report(
            run_id=run_id,
            user_id=user_id
        )
        if not is_ok:
            return {
                "status": "error", 
                "message": path
            }
        
        from werkzeug.utils import secure_filename
        return {
            "status": "success",
            "run_id": run_id,
            "file": secure_filename(f"bank_rec_{run_id}.xlsx"),
            "path": path
        }
    
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
    
    
@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.generate_gstr1_reprot"
)
def generate_report_gstr1(
    self,
    period_label: str,
    user_id: Optional[str] = None
) -> Dict[str, str]:
    try:
        path, is_ok = get_generate_report_service().export_gstr1_report(
            period_label=period_label,
            owner_id=user_id
        )
        
        if not is_ok:
            return {
                "status": "error",
                "message": path
            }
            
        return {
            "status": "success",
            "file_path": path
        }
        
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.generate_journal_report"
)
def generate_report_journal(
    self,
    date_from: date_,
    date_to: date_,
    user_id: Optional[str] = None
) -> Dict[str, str]:
    try:
        path, is_ok = get_generate_report_service().export_journal_report(
            date_from=date_.fromisoformat(date_from),
            date_to=date_.fromisoformat(date_to),
            owner_id=user_id
        )
        
        if not is_ok:
            return {
                "status": "error",
                "message": path
            }
        
        return {
            "status": "success",
            "file_path": path
        }
        
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
    
    
@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.generate_ledger_report"
)
def generate_ledger_report(
    self,
    as_on: date_,
    user_id: Optional[str] = None
) -> Dict[str, str]:
    try:
        path, is_ok = get_generate_report_service().export_ledger_report(
            as_on=date_.fromisoformat(as_on),
            owner_id=user_id
        )
        
        if not is_ok:
            return {
                "status": "error",
                "message": path
            }
            
        return {
            "status": "success",
            "file_path": path
        }
        
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.generate_tds_report"
)
def generate_tds_reprot(
    self,
    period_start: date_, 
    period_end: date_,
    user_id: Optional[str] = None
) -> Dict[str, str]:
    try:
        path, is_ok = get_generate_report_service().export_tds_report(
            period_start=date_.fromisoformat(period_start),
            period_end=date_.fromisoformat(period_end),
            owner_id=user_id
        )
        
        if not is_ok:
            return {
                "status": "error",
                "message": path
            }
            
        return {
            "status": "success",
            "file_path": path
        }
        
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
