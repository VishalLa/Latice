from __future__ import annotations

from datetime import date
from typing import Optional, Tuple

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from service import GenerateReports, RebuildServiceLedger, RunBill, _log_call


app = Blueprint("ledger_api", __name__)


_run_bill: Optional[RunBill] = None
_reports: Optional[GenerateReports] = None
_rebuild_ledger: Optional[RebuildServiceLedger] = None

def _services() -> Tuple[GenerateReports, RebuildServiceLedger]:
    global _run_bill, _reports, _rebuild_ledger
    if _run_bill is None:
        _run_bill = RunBill()
        
    if _reports is None:
        _reports = GenerateReports(db_manager=_run_bill.db_manager)
        
    if _rebuild_ledger is None:
        _rebuild_ledger = RebuildServiceLedger(db_manager=_run_bill.db_manager)
        
    return _reports, _rebuild_ledger


def _parse_date(value: Optional[str], field: str) -> date:
    if not value:
        raise ValueError(f"{field} is required")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD format") from exc


@app.route("/trial-balance", methods=["GET"])
@jwt_required()
@_log_call
def trial_balance():
    try:
        as_on = _parse_date(request.args.get("as_on"), "as_on")
    except ValueError as exc:
        return jsonify({
            "ok": False, 
            "error": str(exc)
        }), 400

    _, _rebuild_ledger = _services()
    
    return jsonify(_rebuild_ledger.get_trial_balance(
        user_id=get_jwt_identity(),
        as_on=as_on,
    )), 200


@app.route("/journal", methods=["GET"])
@jwt_required()
@_log_call
def list_journal_entries():
    today = date.today()
    fy_start = date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)
    
    try:
        date_from = date.fromisoformat(
            request.args.get("date_from", "")
        ) if request.args.get("date_from") else fy_start
        date_to = date.fromisoformat(
            request.args.get("date_to", "")
        ) if request.args.get("date_to") else today
        
    except ValueError:
        return jsonify({
            "ok": False, 
            "error": "date_from and date_to must use YYYY-MM-DD format"
        }), 400

    if date_from > date_to:
        return jsonify({
            "ok": False, 
            "error": "date_from must be on or before date_to"
        }), 400

    _, rebuild_ledger = _services()
    payload = rebuild_ledger.get_journal_entries(
        user_id=get_jwt_identity(),
        date_from=date_from,
        date_to=date_to,
    )

    return jsonify({
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        **payload,
    }), 200


@app.route("/tds", methods=["GET"])
@jwt_required()
@_log_call
def get_tds_register():
    today = date.today()
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    default_start = date(fy_start_year, 4, 1)
    default_end = date(fy_start_year + 1, 3, 31)

    try:
        period_start = date.fromisoformat(
            request.args.get("period_start", "")
        ) if request.args.get("period_start") else default_start
        period_end = date.fromisoformat(
            request.args.get("period_end", "")
        ) if request.args.get("period_end") else default_end
        
    except ValueError:
        return jsonify({
            "ok": False,
            "error": "period_start and period_end must use YYYY-MM-DD format",
        }), 400

    if period_start > period_end:
        return jsonify({
            "ok": False,
            "error": "period_start must be on or before period_end",
        }), 400

    _, rebuild_ledger = _services()
    payload = rebuild_ledger.get_tds_register(
        user_id=get_jwt_identity(),
        period_start=period_start,
        period_end=period_end,
    )
    return jsonify(payload), 200

