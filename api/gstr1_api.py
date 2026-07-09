import os
import tempfile
from collections import defaultdict

from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required
from werkzeug.utils import secure_filename

from database.session import get_session
from database.ledger_tax_models import GSTR1RecordModel
from service import write_gstr1_xlsx
from api._scoping import current_user, scope_owner_id
from tasks.bill_pipeline_task import generate_gstr1_task

app = Blueprint("gstr1_api", __name__)

EXPORT_DIR = tempfile.gettempdir()


def _rebuild_gstr1_dict(session, owner_id, period_label) -> dict | None:
    q = session.query(GSTR1RecordModel).filter(GSTR1RecordModel.period_label == period_label)
    if owner_id is not None:
        q = q.filter(GSTR1RecordModel.user_id == owner_id)
    rows = q.all()
    if not rows:
        return None

    grouped: dict = defaultdict(list)
    for r in rows:
        grouped[r.table_type].append(r.raw or r.to_dict())

    totals = {
        "b2b_invoice_count": len(grouped.get("b2b", [])),
        "total_taxable": round(sum(r.taxable_value for r in rows), 2),
        "total_tax": round(sum(r.igst + r.cgst + r.sgst + r.cess for r in rows), 2),
    }

    return {
        "period_label": period_label,
        "b2b": grouped.get("b2b", []),
        "b2c_large": grouped.get("b2c_large", []),
        "nil_rated": (grouped.get("nil_rated") or [{}])[0],
        "hsn_summary": grouped.get("hsn_summary", []),
        "totals": totals,
        "warnings": [],
    }


@app.route("/<period_label>/generate", methods=["POST"])
@jwt_required()
def generate_gstr1(period_label: str):
    """
    POST /api/gstr1/<period_label>/generate
    Body: {"period_start": "YYYY-MM-DD", "period_end": "YYYY-MM-DD", "user_id": "..." (admin only)}

    Rebuilds GSTR-1 from every processed, output-direction bill in that
    date range. Async — poll Celery via the returned task_id, same
    pattern as /api/run_status/<run_id> for bank reconciliation.
    """
    with get_session() as session:
        user = current_user(session)
        body = request.get_json(silent=True) or {}
        target_user_id, err = scope_owner_id(user, body.get("user_id"))
        if err:
            return err
        if target_user_id is None:
            return jsonify({"error": "Admins must supply user_id to generate GSTR-1 for a specific user"}), 400

        period_start = body.get("period_start")
        period_end = body.get("period_end")
        if not period_start or not period_end:
            return jsonify({"error": "period_start and period_end (YYYY-MM-DD) are required"}), 400

        task = generate_gstr1_task.delay(target_user_id, period_label, period_start, period_end)
        return jsonify({
            "message": "GSTR-1 generation started",
            "task_id": task.id,
            "status_url": f"/api/gstr1/task_status/{task.id}",
        }), 202


@app.route("/task_status/<task_id>", methods=["GET"])
@jwt_required()
def gstr1_task_status(task_id: str):
    from celery.result import AsyncResult
    from app.celery import app as celery_app

    task = AsyncResult(task_id, app=celery_app)
    payload = {"task_id": task_id, "state": task.state}
    if task.state == "SUCCESS":
        payload["result"] = task.result
    elif task.state == "FAILURE":
        payload["error"] = str(task.result)
    return jsonify(payload), 200


@app.route("/<period_label>", methods=["GET"])
@jwt_required()
def get_gstr1(period_label: str):
    """GET /api/gstr1/<period_label>[?user_id=...]"""
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err

        result = _rebuild_gstr1_dict(session, owner_id, period_label)
        if result is None:
            return jsonify({"error": f"No GSTR-1 data found for period {period_label!r}. Generate it first."}), 404
        return jsonify(result), 200


@app.route("/<period_label>/export", methods=["GET"])
@jwt_required()
def export_gstr1(period_label: str):
    """GET /api/gstr1/<period_label>/export[?user_id=...] -> .xlsx download"""
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err

        result = _rebuild_gstr1_dict(session, owner_id, period_label)
        if result is None:
            return jsonify({"error": f"No GSTR-1 data found for period {period_label!r}. Generate it first."}), 404

        filename = secure_filename(f"gstr1_{period_label}.xlsx")
        path = os.path.join(EXPORT_DIR, filename)
        write_gstr1_xlsx(result, path)

        return send_file(path, as_attachment=True, download_name=filename)
