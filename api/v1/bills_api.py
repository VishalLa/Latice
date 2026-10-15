from __future__ import annotations

from typing import Optional

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from service import RunBill, _log_call


app = Blueprint("bills_api", __name__)

_run_bill: Optional[RunBill] = None


def _get_run_bill() -> RunBill:
    global _run_bill
    if _run_bill is None:
        _run_bill = RunBill()
    return _run_bill


@app.route("", methods=["GET"])
@jwt_required()
@_log_call
def list_bills():
    direction = request.args.get("direction")
    if direction and direction not in {"input", "output"}:
        return jsonify({
            "ok": False,
            "error": "direction must be input or output",
        }), 400

    return jsonify(_get_run_bill().list_bills(
        user_id=get_jwt_identity(),
        status=request.args.get("status"),
        direction=direction,
    )), 200
