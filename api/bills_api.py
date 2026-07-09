import os
import tempfile
import uuid

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required
from werkzeug.utils import secure_filename
from celery.result import AsyncResult

from database.session import get_session
from database.ledger_tax_models import BillModel
from ._scoping import current_user, scope_owner_id
from app.celery import app as celery_app
from tasks.bill_pipeline_task import process_bill_task

app = Blueprint("bills_api", __name__)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf"}
UPLOAD_FOLDER = tempfile.gettempdir()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/upload", methods=["POST"])
@jwt_required()
def upload_bill():
    """
    POST /api/bills/upload
    multipart/form-data:
      - file: bill image/PDF (optional if raw_data is supplied)
      - direction: "input" | "output" (default "input")
      - raw_data: optional JSON string of an already-extracted bill dict
                  (skips OCR entirely — useful for testing / non-image sources)

    Creates a Bill row and kicks off process_bill_task asynchronously.
    """
    with get_session() as session:
        user = current_user(session)
        if user is None:
            return jsonify({"error": "User not found"}), 404

        direction = request.form.get("direction", "input")
        if direction not in ("input", "output"):
            return jsonify({"error": "direction must be 'input' or 'output'"}), 400

        source_path = None
        raw_data = None

        if "file" in request.files and request.files["file"].filename:
            f = request.files["file"]
            if not _allowed_file(f.filename):
                return jsonify({"error": "Invalid file type. Allowed: png, jpg, jpeg, pdf"}), 400
            unique_name = f"{uuid.uuid4().hex[:8]}_{secure_filename(f.filename)}"
            source_path = os.path.join(UPLOAD_FOLDER, unique_name)
            f.save(source_path)
        elif request.form.get("raw_data"):
            import json
            try:
                raw_data = json.loads(request.form["raw_data"])
            except (ValueError, TypeError):
                return jsonify({"error": "raw_data must be valid JSON"}), 400
        else:
            return jsonify({"error": "Either 'file' or 'raw_data' is required"}), 400

        bill = BillModel(
            user_id=user.id,
            direction=direction,
            source_file=source_path,
            raw_extracted_data=raw_data,
            status="pending",
        )
        session.add(bill)
        session.commit()
        bill_id = bill.id

        process_bill_task.apply_async(args=[bill_id], task_id=bill_id)

        return jsonify({
            "message": "Bill uploaded, processing started",
            "bill_id": bill_id,
            "status_url": f"/api/bills/{bill_id}/status",
        }), 202


@app.route("/<bill_id>/status", methods=["GET"])
@jwt_required()
def bill_status(bill_id: str):
    """Poll this after upload — combines the Celery task state with the
    persisted Bill row (whichever finishes updating first)."""
    with get_session() as session:
        user = current_user(session)
        bill = session.query(BillModel).filter(BillModel.id == bill_id).first()
        if bill is None:
            return jsonify({"error": "Bill not found"}), 404
        if not user.is_admin and bill.user_id != user.id:
            return jsonify({"error": "Not authorized to view this bill"}), 403

        task = AsyncResult(bill_id, app=celery_app)
        journal_entry = bill.journal_entries[0] if bill.journal_entries else None

        return jsonify({
            "bill_id": bill.id,
            "task_state": task.state,
            "status": bill.status,
            "error_message": bill.error_message,
            "vendor_name": bill.vendor_name,
            "invoice_number": bill.invoice_number,
            "direction": bill.direction,
            "journal_entry_id": journal_entry.entry_id if journal_entry else None,
            "tds_applied": bool(journal_entry.tds_entries) if journal_entry else False,
        }), 200


@app.route("", methods=["GET"])
@jwt_required()
def list_bills():
    """GET /api/bills?status=processed&direction=input[&user_id=...] (admin only for user_id)"""
    with get_session() as session:
        user = current_user(session)
        owner_id, err = scope_owner_id(user, request.args.get("user_id"))
        if err:
            return err

        q = session.query(BillModel)
        if owner_id is not None:
            q = q.filter(BillModel.user_id == owner_id)
        if request.args.get("status"):
            q = q.filter(BillModel.status == request.args["status"])
        if request.args.get("direction"):
            q = q.filter(BillModel.direction == request.args["direction"])

        bills = q.order_by(BillModel.created_at.desc()).limit(200).all()
        return jsonify({
            "count": len(bills),
            "bills": [
                {
                    "id": b.id,
                    "vendor_name": b.vendor_name,
                    "invoice_number": b.invoice_number,
                    "direction": b.direction,
                    "bill_date": b.bill_date.isoformat() if b.bill_date else None,
                    "status": b.status,
                    "error_message": b.error_message,
                    "created_at": b.created_at.isoformat(),
                    **({"user_id": b.user_id, "owner_username": b.user.username} if user.is_admin else {}),
                }
                for b in bills
            ],
        }), 200
