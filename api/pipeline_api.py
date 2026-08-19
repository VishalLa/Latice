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


app = Blueprint("pipeline_api", __name__)


_run_bill: Optional[RunBill] = None
_allowed_extensions = {"png", "jpg", "jpeg", "pdf"}


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

