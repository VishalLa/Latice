import os
import uuid
import tempfile
from flask import Blueprint, request, jsonify, send_file
from werkzeug.utils import secure_filename
from celery.result import AsyncResult

from app.celery import app as celery_app
from tasks.bank_rec_task import (
    run_reconciliation_pipeline,
    generate_report_from_db,
    _report_filename,
    _report_path,
)

app = Blueprint("file_handler", __name__)


ALLOWED_EXTENSIONS = {'csv', 'xlsx'}
UPLOAD_FOLDER = tempfile.gettempdir()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_file(filename: str):
    """Check if the uploaded file has an allowed extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/run_reconciliation", methods=["POST"])
def upload_and_run():
    print("📍 CHECKPOINT 1: Endpoint hit!")
    if 'ledger_file' not in request.files or 'bank_file' not in request.files:
        return jsonify({"error": "Both ledger_file and bank_file are required"}), 400

    ledger_file = request.files['ledger_file']
    bank_file = request.files['bank_file']

    if ledger_file.filename == '' or bank_file.filename == '':
        return jsonify({"error": "Both files must be selected"}), 400

    if not (allowed_file(ledger_file.filename) and allowed_file(bank_file.filename)):
        return jsonify({"error": "Invalid file type. Only CSV and XLSX are allowed."}), 400

    try:
        unique_id = str(uuid.uuid4())[:8]
        ledger_name = f"{unique_id}_{secure_filename(ledger_file.filename)}"
        bank_name = f"{unique_id}_{secure_filename(bank_file.filename)}"

        ledger_path = os.path.join(UPLOAD_FOLDER, ledger_name)
        bank_path = os.path.join(UPLOAD_FOLDER, bank_name)

        print(f"📍 CHECKPOINT 2: Saving files to {UPLOAD_FOLDER} for Celery to read")
        ledger_file.save(ledger_path)
        bank_file.save(bank_path)

        # run_id is minted here and handed to Celery as the explicit task_id,
        # so run_id == task_id everywhere downstream (status / result /
        # download all key off the same value — no separate mapping to keep
        # in sync).
        run_id = str(uuid.uuid4())

        print(f"📍 CHECKPOINT 3: Handing off to Celery, run_id={run_id}...")
        run_reconciliation_pipeline.apply_async(
            args=[ledger_path, bank_path],
            task_id=run_id,
        )

        print(f"📍 CHECKPOINT 4: Task {run_id} dispatched! Returning 202 to client.")
        return jsonify({
            "message": "Reconciliation pipeline started successfully!",
            "run_id": run_id,
            "task_id": run_id,
            # Poll this until status == "SUCCESS", then either:
            #  - read `result` straight from this same payload (results window), or
            #  - hit download_url to fetch the file.
            "status_url": f"/run_status/{run_id}",
        }), 202

    except Exception as e:
        print(f"Error during file upload: {e}")
        return jsonify({"error": "Server Error processing files"}), 500


@app.route("/run_status/<run_id>", methods=["GET"])
def run_status(run_id: str):
    """
    Poll this from the frontend right after upload. Once state == SUCCESS,
    `result` contains everything needed to redirect to a results window
    (reconciliation_data / summary) and to download the report
    (download_url), without any further DB lookup — it's read straight off
    the Celery result backend, keyed by run_id (== task_id).
    """
    task = AsyncResult(run_id, app=celery_app)

    payload = {
        "run_id": run_id,
        "state": task.state,   # PENDING | STARTED | RETRY | SUCCESS | FAILURE
    }

    if task.state == "SUCCESS":
        payload["result"] = task.result
    elif task.state == "FAILURE":
        payload["error"] = str(task.result)

    return jsonify(payload), 200


@app.route("/run_result/<run_id>", methods=["GET"])
def run_result(run_id: str):
    """
    Dedicated endpoint for the frontend's "separate results window" —
    returns just the reconciliation JSON (summary + matches + unreconciled
    items) once the run has finished. Returns 202 while still running so
    the frontend can keep polling the same URL.
    """
    task = AsyncResult(run_id, app=celery_app)

    if task.state == "PENDING":
        return jsonify({"run_id": run_id, "state": task.state,
                         "message": "Unknown run_id or not started yet."}), 202
    if task.state in ("STARTED", "RETRY"):
        return jsonify({"run_id": run_id, "state": task.state,
                         "message": "Still processing."}), 202
    if task.state == "FAILURE":
        return jsonify({"run_id": run_id, "state": task.state,
                         "error": str(task.result)}), 500

    # SUCCESS
    result = task.result or {}
    return jsonify({
        "run_id": run_id,
        "state": task.state,
        "summary": result.get("summary", {}),
        "reconciliation_data": result.get("reconciliation_data", {}),
        "download_url": result.get("download_url"),
    }), 200


@app.route('/download_report/run/<run_id>', methods=['GET'])
def download_report_by_run(run_id: str):
    """
    Download the report for a specific run. Works immediately after the
    run finishes (file was written synchronously by
    run_reconciliation_pipeline), and later too as long as the report
    file is still on local disk. If it's gone (e.g. temp dir was cleared),
    falls back to regenerating it from the database.
    """
    safe_run_id = secure_filename(run_id)
    path = _report_path(safe_run_id)

    if os.path.exists(path):
        return send_file(path, as_attachment=True, download_name=_report_filename(safe_run_id))

    # Fall back: try to regenerate from the DB. Requires the caller to be
    # authorized for this run — plug in your real auth/user_id lookup here.
    user_id = request.args.get("user_id", type=int)
    if user_id is None:
        return jsonify({
            "error": "Report file not found on disk and no user_id supplied "
                     "to regenerate it from the database. Pass ?user_id=... "
                     "or re-run the reconciliation."
        }), 404

    task = generate_report_from_db.apply(args=[safe_run_id], kwargs={"user_id": user_id})
    outcome = task.get() if hasattr(task, "get") else task

    if not outcome or outcome.get("status") != "success":
        return jsonify({
            "error": "Report not found and could not be regenerated.",
            "detail": outcome,
        }), 404

    return send_file(
        outcome["path"], 
        as_attachment=True,
        download_name=_report_filename(safe_run_id)
    )


@app.route('/download_report/<filename>', methods=['GET'])
def download_report(filename: str):
    """Legacy filename-based download — kept for backward compatibility."""
    safe_name = secure_filename(filename)
    path = os.path.join(UPLOAD_FOLDER, safe_name)

    if not os.path.exists(path):
        return jsonify({"error": "Report not found"}), 404
    return send_file(path, as_attachment=True)


@app.route('/generate_report/run/<run_id>', methods=['POST'])
def generate_report_run(run_id: str):
    """Trigger (async) database-backed report regeneration for a run."""
    try:
        user_id = request.json.get('user_id') if request.is_json else None
        task = generate_report_from_db.delay(run_id, user_id=user_id)
        return jsonify({"message": "Report generation started", "task_id": task.id}), 202
    except Exception as e:
        return jsonify({"error": str(e)}), 500
