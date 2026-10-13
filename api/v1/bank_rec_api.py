from __future__ import annotations

import os
import uuid
from typing import Optional, Tuple

from celery.result import AsyncResult
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from werkzeug.utils import secure_filename

from app.celery import celery_app
from core.config import Config
from service import ResultBankRec, RunBankRec, RecJournalPosting, _log_call
from tasks.bank_rec import run_reconciliation_pipeline
from tasks.generate_report_tasks import generate_report_bank_rec


app = Blueprint("bank_rec_api", __name__)

_run_bank_rec: Optional[RunBankRec] = None
_result_service: Optional[ResultBankRec] = None
_journal_posting: Optional[RecJournalPosting] = None


def _services() -> Tuple[RunBankRec, ResultBankRec, RecJournalPosting]:
    global _run_bank_rec, _result_service, _journal_posting
    if _run_bank_rec is None:
        _run_bank_rec = RunBankRec()
        
    if _result_service is None:
        _result_service = ResultBankRec(db_manager=_run_bank_rec.db_manager)
        
    if _journal_posting is None:
        _journal_posting = RecJournalPosting(db_manager=_run_bank_rec.db_manager)
        
    return _run_bank_rec, _result_service, _journal_posting


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in {"csv", "xlsx"}


@app.route("/reconciliation", methods=["POST"])
@jwt_required()
@_log_call
def start_reconciliation():
    ledger_file = request.files.get("ledger_file")
    bank_file = request.files.get("bank_file")
    run_name = (request.form.get("run_name") or "").strip() or None
    
    if not ledger_file or not bank_file or not ledger_file.filename or not bank_file.filename:
        return jsonify({
            "ok": False, 
            "error": "ledger_file and bank_file are required"
        }), 400
        
    if not _allowed(ledger_file.filename) or not _allowed(bank_file.filename):
        return jsonify({
            "ok": False, 
            "error": "Only CSV and XLSX files are supported"
        }), 400

    config = Config.from_env()
    run_token = uuid.uuid4().hex[:12]
    
    ledger_path = os.path.join(
        config.STORAGE_DIR, 
        f"{run_token}_{secure_filename(ledger_file.filename)}"
    )
    bank_path = os.path.join(
        config.STORAGE_DIR, 
        f"{run_token}_{secure_filename(bank_file.filename)}"
    )
    
    ledger_file.save(ledger_path)
    bank_file.save(bank_path)

    task = run_reconciliation_pipeline.apply_async(
        args=[ledger_path, bank_path, run_name, str(get_jwt_identity())]
    )
    return jsonify({
        "ok": True,
        "run_id": task.id,
        "task_id": task.id,
        "run_name": run_name,
        "status_url": f"/api/bank-rec/reconciliation/{task.id}/status",
    }), 202


@app.route("/reconciliation/<run_id>/status", methods=["GET"])
@jwt_required()
@_log_call
def run_status(run_id: str):
    task = AsyncResult(run_id, app=celery_app)
    payload = {"run_id": run_id, "state": task.state}
    
    if task.state == "SUCCESS":
        payload["result"] = task.result
    elif task.state == "FAILURE":
        payload["error"] = str(task.result)
    
    return jsonify(payload), 200


@app.route("/reconciliation/<run_id>", methods=["GET"])
@jwt_required()
@_log_call
def run_result(run_id: str):
    _, result_service, _ = _services()
    result = result_service.get_run_result(
        run_id=run_id, 
        user_id=get_jwt_identity()
    )
    state = result.get("state")
    return jsonify(result), 200 if state == "SUCCESS" else 202 if state in {"PENDING", "STARTED"} else 500


@app.route("/reconciliation/<run_id>/report", methods=["POST"])
@jwt_required()
@_log_call
def generate_report(run_id: str):
    task = generate_report_bank_rec.delay(
        run_id=run_id, 
        user_id=get_jwt_identity()
    )
    
    return jsonify({
        "ok": True, 
        "task_id": task.id, 
        "status": "queued"
    }), 202


@app.route("/reconciliation/<run_id>/journal-entries/approve", methods=["POST"])
@jwt_required()
@_log_call
def approve_journal_entries(run_id: str):
    entries = (request.get_json(silent=True) or {}).get("entries")
    if not isinstance(entries, list) or not entries:
        return jsonify({
            "ok": False, 
            "error": "entries must be a non-empty list"
        }), 400

    _, result_service, posting = _services()
    
    run = result_service.find_run(
        run_id=run_id, 
        user_id=get_jwt_identity()
    )
    
    if run is None:
        return jsonify({
            "ok": False, 
            "error": "Run not found"
        }), 404
        
    return jsonify(posting.approve_journal_entries(
        approved_entries=entries,
        user_id=get_jwt_identity(),
        run_id=run.id,
    )), 200


@app.route("/previous/runs/<user_id>", methods=["GET"])
@jwt_required()
@_log_call
def get_previous_runs(user_id: str):
    authenticated_user_id = str(get_jwt_identity())
    if user_id != authenticated_user_id:
        return jsonify({
            "ok": False,
            "error": "Not authorized to access these runs",
        }), 403

    try:
        limit = int(request.args.get("limit", "50"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        return jsonify({
            "ok": False,
            "error": "limit and offset must be integers",
        }), 400

    if not 1 <= limit <= 200 or offset < 0:
        return jsonify({
            "ok": False,
            "error": "limit must be between 1 and 200 and offset must be non-negative",
        }), 400

    _, result_service, _ = _services()
    runs = result_service.list_runs_for_user(
        user_id=authenticated_user_id,
        limit=limit,
        offset=offset,
    )
    return jsonify({
        "runs": runs,
        "limit": limit,
        "offset": offset,
    }), 200


@app.route("/previous/runs/<user_id>/<run_id>", methods=["GET"])
@jwt_required()
@_log_call
def get_previous_run(user_id: str, run_id: str):
    authenticated_user_id = str(get_jwt_identity())
    if user_id != authenticated_user_id:
        return jsonify({
            "ok": False,
            "error": "Not authorized to access this run",
        }), 403

    include_records_raw = request.args.get("include_records", "true").lower()
    if include_records_raw not in {"true", "false"}:
        return jsonify({
            "ok": False,
            "error": "include_records must be true or false",
        }), 400

    _, result_service, _ = _services()
    run = result_service.get_run_for_user(
        user_id=authenticated_user_id,
        run_id=run_id,
        include_records=include_records_raw == "true",
    )
    if run is None:
        return jsonify({
            "ok": False,
            "error": "Run not found",
        }), 404

    return jsonify(run), 200
