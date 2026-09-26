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

