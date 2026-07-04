import os
import uuid
import tempfile
from typing import Optional

from fastapi import APIRouter, File, UploadFile, HTTPException, Query, Body, Form
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from werkzeug.utils import secure_filename

from database.session import get_session
from service.run_result_service import get_run_result
from tasks.bank_rec_task import (
    run_reconciliation_pipeline,
    generate_report_from_db,
    _report_filename,
    _report_path,
)

router = APIRouter()


ALLOWED_EXTENSIONS = {"csv", "xlsx"}
UPLOAD_FOLDER = tempfile.gettempdir()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_file(filename: str) -> bool:
    """Check if the uploaded file has an allowed extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@router.post("/run_reconciliation", status_code=202)
async def upload_and_run(
    ledger_file: UploadFile = File(...),
    bank_file: UploadFile = File(...),
    user_id: Optional[str] = Form(None),
):
    print("📍 CHECKPOINT 1: Endpoint hit!")

    if not ledger_file.filename or not bank_file.filename:
        raise HTTPException(status_code=400, detail="Both files must be selected")

    if not (allowed_file(ledger_file.filename) and allowed_file(bank_file.filename)):
        raise HTTPException(
            status_code=400, detail="Invalid file type. Only CSV and XLSX are allowed."
        )

    try:
        unique_id = str(uuid.uuid4())[:8]
        ledger_name = f"{unique_id}_{secure_filename(ledger_file.filename)}"
        bank_name = f"{unique_id}_{secure_filename(bank_file.filename)}"

        ledger_path = os.path.join(UPLOAD_FOLDER, ledger_name)
        bank_path = os.path.join(UPLOAD_FOLDER, bank_name)

        print(f"📍 CHECKPOINT 2: Saving files to {UPLOAD_FOLDER} for Celery to read")
        ledger_bytes = await ledger_file.read()
        bank_bytes = await bank_file.read()
        with open(ledger_path, "wb") as f:
            f.write(ledger_bytes)
        with open(bank_path, "wb") as f:
            f.write(bank_bytes)

        run_id = str(uuid.uuid4())

        print(f"📍 CHECKPOINT 3: Handing off to Celery, run_id={run_id}...")
        run_reconciliation_pipeline.apply_async(
            args=[ledger_path, bank_path, user_id],
            task_id=run_id,
        )

        print(f"📍 CHECKPOINT 4: Task {run_id} dispatched! Returning 202 to client.")
        return JSONResponse(
            status_code=202,
            content={
                "message": "Reconciliation pipeline started successfully!",
                "run_id": run_id,
                "task_id": run_id,
                "status_url": f"/run_status/{run_id}",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error during file upload: {e}")
        raise HTTPException(status_code=500, detail="Server Error processing files")


def _get_run_result_sync(run_id: str, user_id: Optional[str]) -> dict:
    """Blocking DB read — no Celery anywhere in this call. Executed in a
    threadpool since the underlying SQLAlchemy session is synchronous."""
    with get_session() as session:
        return get_run_result(session, run_id, user_id)


@router.get("/run_status/{run_id}")
async def run_status(run_id: str, user_id: Optional[str] = Query(None)):

    data = await run_in_threadpool(_get_run_result_sync, run_id, user_id)

    payload = {"run_id": data.get("run_id", run_id), "state": data["state"]}
    if data["state"] == "FAILURE":
        payload["error"] = data.get("error")
    elif data["state"] == "SUCCESS":
        payload["result"] = {
            "summary": data.get("summary", {}),
            "download_url": data.get("download_url"),
        }
    return payload


@router.get("/run_result/{run_id}")
async def run_result(run_id: str, user_id: Optional[str] = Query(None)):

    data = await run_in_threadpool(_get_run_result_sync, run_id, user_id)

    if data["state"] == "FAILURE":
        return JSONResponse(status_code=500, content=data)
    if data["state"] != "SUCCESS":
        return JSONResponse(status_code=202, content=data)
    return data


@router.get("/download_report/run/{run_id}")
async def download_report_by_run(run_id: str, user_id: Optional[str] = Query(None)):

    safe_run_id = secure_filename(run_id)
    path = _report_path(safe_run_id)

    if os.path.exists(path):
        return FileResponse(
            path,
            filename=_report_filename(safe_run_id),
            media_type="application/octet-stream",
        )

    if user_id is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Report file not found on disk and no user_id supplied "
                "to regenerate it from the database. Pass ?user_id=... "
                "or re-run the reconciliation."
            ),
        )

    task = await run_in_threadpool(
        lambda: generate_report_from_db.apply(args=[safe_run_id], kwargs={"user_id": user_id})
    )
    outcome = await run_in_threadpool(task.get) if hasattr(task, "get") else task

    if not outcome or outcome.get("status") != "success":
        raise HTTPException(
            status_code=404,
            detail={"error": "Report not found and could not be regenerated.", "detail": outcome},
        )

    return FileResponse(
        outcome["path"],
        filename=_report_filename(safe_run_id),
        media_type="application/octet-stream",
    )


@router.get("/download_report/{filename}")
async def download_report(filename: str):
    """Legacy filename-based download — kept for backward compatibility."""
    safe_name = secure_filename(filename)
    path = os.path.join(UPLOAD_FOLDER, safe_name)

    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(path, filename=safe_name)


@router.post("/generate_report/run/{run_id}", status_code=202)
async def generate_report_run(run_id: str, body: Optional[dict] = Body(None)):
    """Trigger (async) database-backed report regeneration for a run."""
    try:
        user_id = body.get("user_id") if body else None
        task = generate_report_from_db.delay(run_id, user_id=user_id)
        return {"message": "Report generation started", "task_id": task.id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
