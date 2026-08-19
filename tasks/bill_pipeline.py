from __future__ import annotations

from typing import Optional, Any, Dict

from app.celery import celery_app
from service import RunBill


_run_bill: Optional[RunBill] = None


def get_shared_run_bill() -> RunBill:
    global _run_bill
    if _run_bill is None:
        _run_bill = RunBill()
    return _run_bill


@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.process_bills"
)
def process_bill(self, bill_id: str) -> Dict[str, Any]:
    try:
        return get_shared_run_bill().process_bill(bill_id=bill_id)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="tasks.generate_gstr1"
)
def generate_gstr1(
    self,
    user_id: str,
    period_label: str,
    period_start: str,   # ISO "YYYY-MM-DD"
    period_end: str,     # ISO "YYYY-MM-DD"
) -> Dict[str, Any]:
    try:
        return get_shared_run_bill().generate_gstr1(
            user_id=user_id,
            period_label=period_label,
            period_start=period_start,
            period_end=period_end
        )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
    
