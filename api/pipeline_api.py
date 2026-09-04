from __future__ import annotations

import json
import os
import uuid
from typing import Optional

from celery.result import AsyncResult
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from werkzeug.utils import secure_filename

from app.celery import celery_app
from service import RunBill, _log_call
from tasks.bill_pipeline import generate_gstr1, process_bill
from tasks.full_run import run_full_pipeline


app = Blueprint("pipeline_api", __name__)


_run_bill: Optional[RunBill] = None
_allowed_extensions = {"png", "jpg", "jpeg", "pdf"}
_bank_statement_extensions = {"csv", "xlsx"}


def _get_run_bill() -> RunBill:
    global _run_bill
    if _run_bill is None:
        _run_bill = RunBill()
    return _run_bill


def _task_status(task_id: str):
    task = AsyncResult(task_id, app=celery_app)
    payload = {
        "task_id": task_id, 
        "state": task.state
    }
    
    if task.state == "SUCCESS":
        payload["result"] = task.result
    elif task.state == "FAILURE":
        payload["error"] = str(task.result)
    
    return jsonify(payload), 200


@app.route("/bills", methods=["POST"])
@jwt_required()
@_log_call
def upload_bill():
    direction = request.form.get("direction", "input").lower()
    if direction not in {"input", "output"}:
        return jsonify({
            "ok": False, 
            "error": "direction must be input or output"
        }), 400

    source_path: Optional[str] = None
    raw_data = None
    
    uploaded_file = request.files.get("file")
    if uploaded_file is not None and uploaded_file.filename:
        filename = uploaded_file.filename
        
        if "." not in filename or filename.rsplit(".", 1)[1].lower() not in _allowed_extensions:
            return jsonify({
                "ok": False, 
                "error": "Unsupported bill file type"
            }), 400
        
        config = _get_run_bill().config
        source_path = os.path.join(
            config.STORAGE_DIR,
            f"{uuid.uuid4().hex[:12]}_{secure_filename(filename)}",
        )
        uploaded_file.save(source_path)
        
    elif request.form.get("raw_data"):
        try:
            raw_data = json.loads(request.form["raw_data"])
        except (TypeError, ValueError):
            return jsonify({
                "ok": False, 
                "error": "raw_data must be valid JSON"
            }), 400
    else:
        return jsonify({
            "ok": False, 
            "error": "file or raw_data is required"
        }), 400

    bill_id = _get_run_bill().create_bill(
        user_id=get_jwt_identity(),
        direction=direction,
        source_file=source_path,
        raw_extracted_data=raw_data,
    )
    
    task = process_bill.apply_async(args=[bill_id], task_id=bill_id)
    return jsonify({
        "ok": True,
        "bill_id": bill_id,
        "task_id": task.id,
        "status_url": f"/api/pipeline/bills/{bill_id}/status",
    }), 202
    

@app.route("/bills/<bill_id>/status", methods=["GET"])
@jwt_required()
@_log_call
def bill_status(bill_id: str):
    return _task_status(bill_id)


@app.route("/fullrun", methods=["POST"])
@jwt_required()
@_log_call
def fullrun():
    """Upload bills and a bank statement for the bill-to-reconciliation flow."""
    bank_file = request.files.get("bank_statement") or request.files.get("bank_file")
    bill_files = request.files.getlist("bill_files") or request.files.getlist("bills")

    if not bank_file or not bank_file.filename:
        return jsonify({
            "ok": False, 
            "error": "bank_statement is required"
        }), 400
    if not bill_files or not any(f.filename for f in bill_files):
        return jsonify({
            "ok": False, 
            "error": "at least one bill_files upload is required"
        }), 400

    def _has_extension(filename: str, extensions: set[str]) -> bool:
        return "." in filename and filename.rsplit(".", 1)[1].lower() in extensions

    if not _has_extension(bank_file.filename, _bank_statement_extensions):
        return jsonify({
            "ok": False, 
            "error": "bank_statement must be CSV or XLSX"
        }), 400
    
    invalid_bills = [
        f.filename 
        for f in bill_files 
        if f.filename and not _has_extension(f.filename, _allowed_extensions)
    ]
    
    if invalid_bills:
        return jsonify({
            "ok": False, 
            "error": "Bills must be PNG, JPG, JPEG, or PDF"
        }), 400

    config = _get_run_bill().config
    upload_token = uuid.uuid4().hex[:12]
    bank_path = os.path.join(
        config.STORAGE_DIR,
        f"{upload_token}_bank_{secure_filename(bank_file.filename)}",
    )
    bank_file.save(bank_path)

    image_paths = []
    try:
        for index, bill_file in enumerate(f for f in bill_files if f.filename):
            image_path = os.path.join(
                config.STORAGE_DIR,
                f"{upload_token}_bill_{index}_{secure_filename(bill_file.filename)}",
            )
            
            bill_file.save(image_path)
            image_paths.append(image_path)
            
    except Exception:
        for path in [bank_path, *image_paths]:
            if os.path.exists(path):
                os.remove(path)
        raise

    task = run_full_pipeline.apply_async(
        args=[get_jwt_identity(), image_paths, bank_path],
    )
    
    return jsonify({
        "ok": True,
        "run_id": task.id,
        "task_id": task.id,
        "status_url": f"/api/pipeline/tasks/{task.id}",
    }), 202


@app.route("/gstr1", methods=["POST"])
@jwt_required()
@_log_call
def generate_gstr1_report():
    data = request.get_json(silent=True) or {}
    required = ("period_label", "period_start", "period_end")
    missing = [
        key 
        for key in required 
        if not data.get(key)
    ]
    if missing:
        return jsonify({
            "ok": False, 
            "error": f"Missing fields: {', '.join(missing)}"
        }), 400

    task = generate_gstr1.delay(
        get_jwt_identity(),
        data["period_label"],
        data["period_start"],
        data["period_end"],
    )
    return jsonify({
        "ok": True,
        "status": "queued",
        "task_id": task.id,
        "status_url": f"/api/pipeline/tasks/{task.id}",
    }), 202


@app.route("/tasks/<task_id>", methods=["GET"])
@jwt_required()
@_log_call
def task_status(task_id: str):
    return _task_status(task_id)
