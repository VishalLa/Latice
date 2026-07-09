import os
import tempfile
from datetime import date, datetime

from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required
from werkzeug.utils import secure_filename

from database.session import get_session
from database.ledger_tax_models import TDSEntryModel
from service.rebuild_ledger_data import rebuild_tds_register
from service import write_tds_xlsx
from ._scoping import current_user, scope_owner_id

app = Blueprint("tds_api", __name__)

EXPORT_DIR = tempfile.gettempdir()

def _parse_date(val, fallback):
    if not val:
        return fallback
    try:
        return datetime.strptime(val, "%Y-%m-%d").date()
    except ValueError:
        return fallback

def _default_fy_bounds(today: date) -> tuple[date, date]:
    start_year = today.year if today.month >= 4 else today.year - 1
    return date(start_year, 4, 1), date(start_year + 1, 3, 31)

def _load_register(session, owner_id, period_start, period_end):
    q = session.query(TDSEntryModel).filter(
        TDSEntryModel.date >= period_start,
        TDSEntryModel.date <= period_end,
    )
    if owner_id is not None:
        q = q.filter(TDSEntryModel.user_id == owner_id)
    models = q.order_by(TDSEntryModel.date.asc()).all()
    return rebuild_tds_register(models, period_start, period_end)

@app.route("", methods=["GET"])
@jwt_required()
def get_tds_register():
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err

        default_start, default_end = _default_fy_bounds(date.today())
        period_start = _parse_date(request.args.get("period_start"), default_start)
        period_end = _parse_date(request.args.get("period_end"), default_end)

        reg = _load_register(session, owner_id, period_start, period_end)
        return jsonify(reg.to_dict()), 200

@app.route("/export", methods=["GET"])
@jwt_required()
def export_tds():
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err

        default_start, default_end = _default_fy_bounds(date.today())
        period_start = _parse_date(request.args.get("period_start"), default_start)
        period_end = _parse_date(request.args.get("period_end"), default_end)

        reg = _load_register(session, owner_id, period_start, period_end)
        if not reg.entries:
            return jsonify({"error": "No TDS entries found for this period"}), 404

        filename = secure_filename(f"tds_register_{period_start.isoformat()}_{period_end.isoformat()}.xlsx")
        path = os.path.join(EXPORT_DIR, filename)
        write_tds_xlsx(reg, path)

        return send_file(path, as_attachment=True, download_name=filename)
