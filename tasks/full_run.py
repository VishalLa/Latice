from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.celery import celery_app
from service import FullRun


_full_run: Optional[FullRun] = None


def get_full_run() -> FullRun:
    global _full_run
    if _full_run is None:
        _full_run = FullRun()
    return _full_run


@celery_app.task(bind=True, name="tasks.run_full_pipeline")
def run_full_pipeline(
    self,
    user_id: str,
    image_paths: List[str],
    bank_path: str,
) -> Dict[str, Any]:
    """Run once: retries would create duplicate bills and journal entries."""
    return get_full_run().run_full_pipeline(
        user_id=user_id,
        image_paths=image_paths,
        bank_path=bank_path,
        run_id=self.request.id,
    )
