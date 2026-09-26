from __future__ import annotations

from datetime import date
import os

from celery.result import AsyncResult
from flask import Blueprint, jsonify, request, send_file
from flask_jwt_extended import get_jwt_identity, jwt_required

from tasks.generate_report_tasks import (
    generate_ledger_report as generate_ledger_report_task,
    generate_report_bank_rec as generate_bank_reconciliation_task,
    generate_report_gstr1 as generate_gstr1_task,
    generate_report_journal as generate_journal_task,
    generate_tds_reprot as generate_tds_task,
)
from app.celery import celery_app
from service import _log_call


app = Blueprint("reports", __name__)


def _enqueue(task, **kwargs):
    result = task.delay(**kwargs)
    return jsonify({
        "ok": True,
        "status": "queued",
        "task_id": result.id,
        "download_url": f"/api/reports/tasks/{result.id}/download",
    }), 202


@app.route("/reports/tasks/<task_id>", methods=["GET"])
@jwt_required()
@_log_call
def report_task_status(task_id: str):
    task = AsyncResult(task_id, app=celery_app)
    payload = {
        "ok": task.state not in {"FAILURE", "REVOKED"},
        "task_id": task_id,
        "state": task.state,
        "download_url": f"/api/reports/tasks/{task_id}/download",
    }
    
    if task.state == "SUCCESS":
        payload["result"] = task.result
    elif task.state in {"FAILURE", "REVOKED"}:
        payload["error"] = str(task.result)

    return jsonify(payload), 200


@app.route("/reports/tasks/<task_id>/download", methods=["GET"])
@jwt_required()
@_log_call
def download_report_task(task_id: str):
    task = AsyncResult(task_id, app=celery_app)
    if task.state in {"PENDING", "STARTED", "RETRY"}:
        return jsonify({
            "ok": True,
            "task_id": task_id,
            "state": task.state,
            "message": "Report is still being generated",
        }), 202

    if task.state != "SUCCESS":
        return jsonify({
            "ok": False,
            "task_id": task_id,
            "state": task.state,
            "error": str(task.result),
        }), 500

    result = task.result or {}
    if result.get("status") != "success":
        return jsonify({
            "ok": False,
            "task_id": task_id,
            "error": result.get("message", "Report generation failed"),
        }), 404

    path = result.get("path") or result.get("file_path")
    if not path or not os.path.isfile(path):
        return jsonify({
            "ok": False,
            "task_id": task_id,
            "error": "Generated report file is no longer available",
        }), 404

    filename = result.get("file") or os.path.basename(path)
    return send_file(path, as_attachment=True, download_name=filename)


@app.route("/reports/bank-reconciliation", methods=["POST"])
@jwt_required()
@_log_call
def generate_bank_reconciliation_report():
    data = request.get_json(silent=True) or {}
    run_id = data.get("run_id")
    if not run_id:
        return jsonify({
            "ok": False, 
            "error": "run_id is required"
        }), 400

    return _enqueue(
        generate_bank_reconciliation_task,
        run_id=run_id,
        user_id=get_jwt_identity(),
    )


@app.route("/reports/gstr1", methods=["POST"])
@jwt_required()
@_log_call
def generate_gstr1_report():
    data = request.get_json(silent=True) or {}
    period_label = data.get("period_label")
    if not period_label:
        return jsonify({
            "ok": False, 
            "error": "period_label is required"
        }), 400

    return _enqueue(
        generate_gstr1_task,
        period_label=period_label,
        user_id=get_jwt_identity(),
    )


@app.route("/reports/journal", methods=["POST"])
@jwt_required()
@_log_call
def generate_journal_report():
    data = request.get_json(silent=True) or {}
    try:
        date_from = date.fromisoformat(data["date_from"])
        date_to = date.fromisoformat(data["date_to"])
    except (KeyError, TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "date_from and date_to must use YYYY-MM-DD format"
        }), 400

    if date_from > date_to:
        return jsonify({
            "ok": False,
            "error": "date_from cannot be after date_to"
        }), 400

    return _enqueue(
        generate_journal_task,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        user_id=get_jwt_identity(),
    )


@app.route("/reports/ledger", methods=["POST"])
@jwt_required()
@_log_call
def generate_ledger_report():
    data = request.get_json(silent=True) or {}
    try:
        as_on = date.fromisoformat(data["as_on"])
    except (KeyError, TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "as_on must use YYYY-MM-DD format"
        }), 400

    return _enqueue(
        generate_ledger_report_task,
        as_on=as_on.isoformat(),
        user_id=get_jwt_identity(),
    )


@app.route("/reports/tds", methods=["POST"])
@jwt_required()
@_log_call
def generate_tds_report():
    data = request.get_json(silent=True) or {}
    try:
        period_start = date.fromisoformat(data["period_start"])
        period_end = date.fromisoformat(data["period_end"])
    except (KeyError, TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "period_start and period_end must use YYYY-MM-DD format"
        }), 400

    if period_start > period_end:
        return jsonify({
            "ok": False,
            "error": "period_start cannot be after period_end"
        }), 400

    return _enqueue(
        generate_tds_task,
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        user_id=get_jwt_identity(),
    )
