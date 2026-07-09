import os
import tempfile
from datetime import date, datetime

from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required
from werkzeug.utils import secure_filename

from database.session import get_session
from database.ledger_tax_models import JournalEntryModel
from service.rebuild_ledger_data import rebuild_journal_entries
from service import write_ledger_xlsx
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
