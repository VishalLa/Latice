import os
import dataclasses
import tempfile
import uuid
from app.celery import app
from database.session import get_session
from service import PushEntryPointData
from matcher import reconcile
from entry_point.loader import load_bank_statement, load_ledger
from sqlalchemy.orm import Session

from matcher.test import print_reconciliation_results
from service import write_bank_recon_xlsx


def _report_filename(run_id: str) -> str:
    return f"bank-recon-{run_id}.xlsx"


def _report_path(run_id: str) -> str:
    return os.path.join(tempfile.gettempdir(), _report_filename(run_id))


def _collect_all_matches(result: dict) -> list:
    """reconcile() exposes matches split by phase; flatten them for the
    generic 'matches' payload the DB-push task expects."""
    return (
        result.get("EXACT_MATCHES", []) +
        result.get("FUZZY_MATCHES", []) +
        result.get("MEMORY_MATCHES", []) +
        result.get("AI_MATCHES", [])
    )


@app.task(bind=True, max_retries=3)
def process_pre_data(self, statements_data, ledgers_data):
    """Handles pushing loaded CSV data to the DB."""
    try:
        with get_session() as session:
            success = PushEntryPointData.push_all_data(
                session=session,
                statements_data=statements_data,
                ledgers_data=ledgers_data
            )
            return {"status": "success" if success else "failed"}
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)


@app.task(bind=True, max_retries=3)
def process_post_data(self, matches_data, ignored_data, audit_data):
    """Handles pushing matched and residual data to the DB."""
    try:
        with get_session() as session:
            success = PushEntryPointData.push_reconciliation_results(
                session=session,
                matches_data=matches_data,
                ignored_data=ignored_data,
                audit_data=audit_data
            )
            return {"status": "success" if success else "failed"}
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)


@app.task(bind=True)
def run_reconciliation_pipeline(self, ledger_path, bank_path):
    """
    Reads the files, triggers DB saving tasks, runs the matcher, generates
    the XLSX report SYNCHRONOUSLY (so it's guaranteed to exist the moment
    this task reports SUCCESS), triggers post-data DB saving, and returns
    a payload the frontend can use to redirect to a results view and/or
    download the report immediately.

    IMPORTANT: this task must be dispatched with an explicit task_id equal
    to the run_id the API hands back to the client:

        run_reconciliation_pipeline.apply_async(
            args=[ledger_path, bank_path], task_id=run_id,
        )

    That makes run_id == Celery task_id == report filename key, so every
    downstream lookup (status / result / download) uses the same id.
    """
    run_id = self.request.id
    print(f"DEBUG: Task received! run_id={run_id} Processing: {ledger_path}")

    def _serialize_for_celery(data_list):
        if not data_list:
            return []
        if dataclasses.is_dataclass(data_list[0]):
            return [dataclasses.asdict(item) for item in data_list]
        elif hasattr(data_list[0], 'to_dict'):
            return [item.to_dict() for item in data_list]
        return data_list

    def _deep_serialize(obj):
        """Recursively serialize objects to JSON-friendly structures."""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj

        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)

        if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
            try:
                return _deep_serialize(obj.to_dict())
            except Exception:
                pass

        if isinstance(obj, dict):
            return {k: _deep_serialize(v) for k, v in obj.items()}

        if isinstance(obj, (list, tuple, set)):
            return [_deep_serialize(i) for i in obj]

        try:
            attrs = getattr(obj, "__dict__", None)
            if isinstance(attrs, dict):
                return {k: _deep_serialize(v) for k, v in attrs.items() if not k.startswith("_")}
        except Exception:
            pass

        return str(obj)

    try:
        ledger_data = load_ledger(filepath=ledger_path, date_format="%d-%m-%Y")
        bank_data = load_bank_statement(filepath=bank_path)

        gl_records = ledger_data.get("records", [])
        bank_records = bank_data.get("records", [])

        statements_list = _serialize_for_celery(bank_records)
        ledgers_list = _serialize_for_celery(gl_records)

        process_pre_data.delay(
            statements_data=statements_list,
            ledgers_data=ledgers_list
        )

        all_warnings = ledger_data.get("warnings", []) + bank_data.get("warnings", [])
        result = reconcile(
            ledger_result=ledger_data,
            bank_result=bank_data,
            all_warnings=all_warnings
        )

        all_matches = _collect_all_matches(result)
        matches_list = _serialize_for_celery(all_matches)
        raw_ignored = result.get("IGNORED_METADATA", [])
        for item in raw_ignored:
            if "ledger_id" in item:
                item["row_ref"] = item.pop("ledger_id")
            elif "row_index" in item:
                item["row_ref"] = str(item.pop("row_index"))

        ignored_list = _serialize_for_celery(raw_ignored)
        audit_list = _serialize_for_celery(result.get("AUDIT_INVESTIGATION", []))

        print("\n\n" + "=" * 50)
        print(f"🎯 CELERY WORKER: MATCHING COMPLETE for run_id={run_id}! PRINTING RESULTS:")
        print_reconciliation_results(results=result)
        print("=" * 50 + "\n\n")

        process_post_data.delay(
            matches_data=matches_list,
            ignored_data=ignored_list,
            audit_data=audit_list
        )
        report_name = _report_filename(run_id)
        out_path = _report_path(run_id)
        try:
            write_bank_recon_xlsx(result, gl_records, bank_records, out_path)
            report_ready = True
        except Exception as report_exc:
            print(f"⚠️  Report generation failed for run_id={run_id}: {report_exc}")
            report_name = None
            report_ready = False

        if os.path.exists(ledger_path):
            os.remove(ledger_path)
        if os.path.exists(bank_path):
            os.remove(bank_path)

        safe_result = _deep_serialize(result)

        return {
            "status": "success",
            "run_id": run_id,
            "summary": safe_result.get("summary", {}),
            "matches_found": len(matches_list),
            "reconciliation_data": safe_result,
            "report_ready": report_ready,
            "report_file": report_name,
            "result_url": f"/run_result/{run_id}",
            "download_url": f"/download_report/run/{run_id}" if report_ready else None,
        }

    except Exception as e:
        if os.path.exists(ledger_path):
            os.remove(ledger_path)
        if os.path.exists(bank_path):
            os.remove(bank_path)
        raise self.retry(exc=e, countdown=10)


def _fetch_run_data(session: Session, user_id: int, run_id: str):
    """Query DB and reconstruct the run payload used by report writers.

    Returns (recon_result_dict, ledger_schema_list, bank_schema_list)
    or None when not found / unauthorized.
    """
    from database.bank_renc_model import (
        ReconciliationRunModel,
        LedgerFormatModel,
        BankStatementModel,
        MatchResultModel,
    )
    from schema.bank_renc_schema import LedgerFormat as SchemaLedger, BankStatement as SchemaBank

    run = session.query(ReconciliationRunModel).filter(
        ReconciliationRunModel.id == int(run_id),
    )
    # If user_id provided, restrict
    if user_id is not None:
        run = run.filter(ReconciliationRunModel.user_id == int(user_id))

    run = run.first()
    if run is None:
        return None

    # Build matches payload
    matches = []
    for mr in run.match_results:
        # ledger_format and bank_statement relationships may be None
        lid = mr.ledger_format.ledger_id if mr.ledger_format else None
        bid = mr.bank_statement.row_index if mr.bank_statement else None
        matches.append({
            "id": mr.id,
            "ledger_id": lid,
            "bank_id": str(bid) if bid is not None else None,
            "match_type": mr.match_type,
            "adjustment_type": mr.adjustment_type,
            "amount": mr.matched_amount,
            "date": mr.matched_date.isoformat() if mr.matched_date else None,
            "confidence_score": mr.confidence_score,
            "details": mr.details,
        })

    # Ledger rows attached to the run
    gl_objs = []
    ledger_q = session.query(LedgerFormatModel).filter(LedgerFormatModel.run_id == run.id)
    for lr in ledger_q.all():
        gl_objs.append(SchemaLedger(
            account_name=lr.account_name or "",
            ledger_id=lr.ledger_id,
            account_number=lr.account_number,
            transaction_date=lr.transaction_date.isoformat() if lr.transaction_date else None,
            transaction_date_raw=lr.transaction_date_raw,
            debit_amount=lr.debit_amount,
            credit_amount=lr.credit_amount,
            reference_id=lr.reference_id,
            parse_warnings=lr.parse_warnings or [],
            source=lr.source.value if hasattr(lr.source, 'value') else lr.source,
            journal_entry_id=lr.journal_entry_id,
            voucher_type=lr.voucher_type,
            vendor_name=lr.vendor_name,
            run_id=str(lr.run_id) if lr.run_id is not None else None,
        ))

    # Bank rows attached to the run
    bank_objs = []
    bank_q = session.query(BankStatementModel).filter(BankStatementModel.run_id == run.id)
    for bs in bank_q.all():
        bank_objs.append(SchemaBank(
            row_index=bs.row_index,
            bank_name=bs.bank_name or "",
            template_version=bs.template_version or "",
            date=bs.date.isoformat() if bs.date else None,
            date_raw=bs.date_raw,
            narration=bs.narration or "",
            debit=bs.debit,
            credit=bs.credit,
            balance=bs.balance,
            txn_id=bs.txn_id,
            parse_warnings=bs.parse_warnings or [],
            run_id=str(bs.run_id) if bs.run_id is not None else None,
        ))

    # Assemble result with counts and summary
    recon_result = {
        "summary": {
            "run_id": str(run.id),
            "bank_name": run.bank_name,
            "template_version": run.template_version,
            "ledger_records": run.ledger_records,
            "bank_records": run.bank_records,
            "exact_matches": run.exact_matches,
            "fuzzy_matches": run.fuzzy_matches,
            "ai_matches": run.ai_matches,
            "unreconciled_ledger": run.unreconciled_ledger,
            "unreconciled_bank": run.unreconciled_bank,
            "run_at": run.run_at.isoformat() if run.run_at else None,
            "user_id": str(run.user_id) if run.user_id is not None else None,
        },
        "EXACT_MATCHES": [m for m in matches if m.get("match_type") == "exact"],
        "FUZZY_MATCHES": [m for m in matches if m.get("match_type") == "fuzzy"],
        "MEMORY_MATCHES": [m for m in matches if m.get("match_type") == "memory"],
        "AI_MATCHES": [m for m in matches if m.get("match_type") in ("ai", "ai_queue")],
        "UNRECONCILED_ITEMS": {
            "ledger": gl_objs,
            "bank": bank_objs,
        },
    }

    return recon_result, gl_objs, bank_objs


@app.task(bind=True)
def get_data_from_db(self, user_id: int, run_id: str, filename: str = None):
    try:
        with get_session() as session:
            fetched = _fetch_run_data(session, user_id, run_id)
            if fetched is None:
                return {"status": "error", "message": "Reconciliation run not found or unauthorized."}
            recon_result, _gl_objs, _bank_objs = fetched
            return recon_result
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)


@app.task(bind=True)
def generate_report_from_db(self, run_id: str, user_id: int = None, filename: str = None):
    """
    On-demand regeneration of the XLSX report from the database, for a
    specific run_id. This is the fallback path used when the run's report
    file is no longer on local disk (e.g. temp dir was cleared) — it
    rebuilds the same file at the same predictable path so
    "/download_report/run/<run_id>" keeps working transparently.
    """
    try:
        with get_session() as session:
            fetched = _fetch_run_data(session, user_id, run_id)
            if fetched is None:
                return {"status": "error", "message": "Reconciliation run not found or unauthorized."}
            recon_result, gl_objs, bank_objs = fetched

        out_path = _report_path(run_id)
        write_bank_recon_xlsx(recon_result, gl_objs, bank_objs, out_path)
        return {
            "status": "success",
            "run_id": run_id,
            "file": _report_filename(run_id),
            "path": out_path,
        }
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
    