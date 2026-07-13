import os
import tempfile
from datetime import date, datetime

from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required
from werkzeug.utils import secure_filename

from database.session import get_session
from database.ledger_tax_models import JournalEntryModel
from database.period_model import FiscalPeriod
from service.rebuild_ledger_data import rebuild_journal_entries
from service import close_period, PeriodAlreadyClosedError, write_ledger_xlsx
from api._scoping import current_user, scope_owner_id

from ledger import build_ledger
from ledger.ledger import trial_balance

app = Blueprint("ledger_api", __name__)

EXPORT_DIR = tempfile.gettempdir()


def _parse_date(val, fallback):
    if not val:
        return fallback
    try:
        return datetime.strptime(val, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def _load_general_ledger(session, owner_id, as_on):
    """Every JournalEntry up to `as_on`, scoped to owner_id (None = all users, admin-only)."""
    q = session.query(JournalEntryModel).filter(JournalEntryModel.date <= as_on)
    if owner_id is not None:
        q = q.filter(JournalEntryModel.user_id == owner_id)
    models = q.order_by(JournalEntryModel.date.asc()).all()
    entries = rebuild_journal_entries(models)
    gl, all_entries, _closing = build_ledger([], _prebuilt_entries=entries)
    return gl, entries


@app.route("/trial-balance", methods=["GET"])
@jwt_required()
def get_trial_balance():
    """GET /api/ledger/trial-balance?as_on=YYYY-MM-DD[&user_id=...]"""
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err

        as_on = _parse_date(request.args.get("as_on"), date.today())
        gl, entries = _load_general_ledger(session, owner_id, as_on)

        if not entries:
            return jsonify({"as_on": as_on.isoformat(), "is_balanced": True, "accounts": []}), 200

        tb = trial_balance(gl, as_on=as_on)
        payload = tb.to_dict()
        if owner_id is None:
            payload["scope"] = "all_users"
        return jsonify(payload), 200


@app.route("/export", methods=["GET"])
@jwt_required()
def export_ledger():
    """GET /api/ledger/export?as_on=YYYY-MM-DD[&user_id=...] -> .xlsx download
    (Trial Balance + Ledger Accounts + Cash Book sheets)"""
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err

        as_on = _parse_date(request.args.get("as_on"), date.today())
        gl, entries = _load_general_ledger(session, owner_id, as_on)

        if not entries:
            return jsonify({"error": "No ledger entries found up to this date"}), 404

        filename = secure_filename(f"ledger_{as_on.isoformat()}.xlsx")
        path = os.path.join(EXPORT_DIR, filename)
        write_ledger_xlsx(gl, path, as_on=as_on)

        return send_file(path, as_attachment=True, download_name=filename)

@app.route("/close-period", methods=["POST"])
@jwt_required()
def close_period_endpoint():
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err
        if owner_id is None:
            return jsonify({"error": "user_id is required to close a period"}), 400

        body = request.get_json(silent=True) or {}
        period_end = _parse_date(body.get("period_end"), None)
        period_label = (body.get("period_label") or "").strip()
        financial_year = (body.get("financial_year") or "").strip()
        period_type = (body.get("period_type") or "month").strip().lower()
        period_start = _parse_date(body.get("period_start"), None) if body.get("period_start") else None

        if period_end is None or not period_label or not financial_year:
            return jsonify({
                "error": "period_end, period_label, and financial_year are all required"
            }), 400
        if period_type not in {"month", "quarter", "year"}:
            return jsonify({"error": "period_type must be one of: month, quarter, year"}), 400

        try:
            result = close_period(
                session=session,
                user_id=owner_id,
                period_end=period_end,
                period_label=period_label,
                financial_year=financial_year,
                period_type=period_type,
                period_start=period_start,
                closed_by=user.id if user else None,
                notes=body.get("notes"),
            )
        except PeriodAlreadyClosedError as exc:
            return jsonify({"error": str(exc)}), 409

        return jsonify(result), 200


@app.route("/periods", methods=["GET"])
@jwt_required()
def list_periods():
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err
        if owner_id is None:
            return jsonify({"error": "user_id is required to list periods"}), 400

        rows = (
            session.query(FiscalPeriod)
            .order_by(FiscalPeriod.financial_year.desc(), FiscalPeriod.sequence_number.desc())
            .all()
        )
        return jsonify([
            {
                "id": p.id,
                "financial_year": p.financial_year,
                "period_type": p.period_type,
                "period_label": p.period_label,
                "period_start": p.period_start.isoformat(),
                "period_end": p.period_end.isoformat(),
                "is_closed": p.is_closed,
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
                "net_profit": p.net_profit,
                "books_closed": p.books_closed,
            }
            for p in rows
        ]), 200
