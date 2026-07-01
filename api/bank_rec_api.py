import os
import uuid
from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
from tasks.bank_rec_task import run_reconciliation_pipeline 

app = Blueprint("file_handler", __name__)


ALLOWED_EXTENSIONS = {'csv', 'xlsx'}
UPLOAD_FOLDER = "/tmp"  
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

        print("📍 CHECKPOINT 3: Handing off to Celery pre_data_queue...")
        task = run_reconciliation_pipeline.delay(ledger_path, bank_path)

        print(f"📍 CHECKPOINT 4: Task {task.id} dispatched! Returning 202 to client.")
        return jsonify({
            "message": "Reconciliation pipeline started successfully!",
            "task_id": task.id
        }), 202

    except Exception as e:
        print(f"Error during file upload: {e}")
        return jsonify({"error": "Server Error processing files"}), 500
    