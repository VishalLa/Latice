import os
import tempfile
from datetime import date, datetime

from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required
from werkzeug.utils import secure_filename

from database.session import get_session
from database.ledger_tax_models import JournalEntryModel
from service.rebuild_ledger_data import rebuild_journal_entries
from service import write_journal_xlsx
from ._scoping import current_user, scope_owner_id

app = Blueprint("journal_api", __name__)

EXPORT_DIR = tempfile.gettempdir()

def _parse_date(val: str | None, fallback: date) -> date:
    if not val:
        return fallback
    try:
        return datetime.strptime(val, "%Y-%m-%d").date()
    except ValueError:
        return fallback

def _query_journal_entries(session, owner_id, date_from, date_to):
    q = session.query(JournalEntryModel).filter(
        JournalEntryModel.date >= date_from,
        JournalEntryModel.date <= date_to,
    )
    if owner_id is not None:
        q = q.filter(JournalEntryModel.user_id == owner_id)
    return q.order_by(JournalEntryModel.date.asc()).all()

@app.route("", methods=["GET"])
@jwt_required()
def list_journal_entries():
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err

        today = date.today()
        date_from = _parse_date(request.args.get("date_from"), date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1))
        date_to = _parse_date(request.args.get("date_to"), today)

        models = _query_journal_entries(session, owner_id, date_from, date_to)
        entries = rebuild_journal_entries(models)

        return jsonify({
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "count": len(entries),
            "total_debit": round(sum(e.total_amount for e in entries), 2),
            "entries": [
                {
                    "entry_id": m.entry_id,
                    "date": m.date.isoformat(),
                    "voucher_type": m.voucher_type,
                    "narration": m.narration,
                    "vendor_name": m.vendor_name,
                    "invoice_number": m.invoice_number,
                    "direction": m.direction,
                    "user_id": m.user_id,
                    **({"owner_username": m.user.username} if user.is_admin else {}),
                    "total_amount": e.total_amount,
                    "lines": m.to_dict()["lines"],
                }
                for m, e in zip(models, entries)
            ],
        }), 200

@app.route("/export", methods=["GET"])
@jwt_required()
def export_journal():
    """GET /api/journal/export?date_from=...&date_to=...[&user_id=...] -> .xlsx download"""
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err

        today = date.today()
        date_from = _parse_date(request.args.get("date_from"), date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1))
        date_to = _parse_date(request.args.get("date_to"), today)

        models = _query_journal_entries(session, owner_id, date_from, date_to)
        if not models:
            return jsonify({"error": "No journal entries found for this period"}), 404

        entries = rebuild_journal_entries(models)

        filename = secure_filename(f"journal_{date_from.isoformat()}_{date_to.isoformat()}.xlsx")
        path = os.path.join(EXPORT_DIR, filename)
        write_journal_xlsx(entries, path)

        return send_file(path, as_attachment=True, download_name=filename)
