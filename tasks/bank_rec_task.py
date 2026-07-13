from typing import Optional
import os
import dataclasses
import uuid
from app.celery import app
from core.config import settings
from database.session import get_session
from service import PushBankRecData
from matcher import reconcile
from entry_point.loader import load_bank_statement, load_ledger
from sqlalchemy.orm import Session

try:
    from matcher.test import print_reconciliation_results
except ModuleNotFoundError:
    def print_reconciliation_results(results):
        print("(matcher.test.print_reconciliation_results not available — skipping detailed console dump)")
from service import write_bank_recon_xlsx


def _report_filename(run_id: str) -> str:
    return f"bank-recon-{run_id}.xlsx"


def _report_path(run_id: str) -> str:
    return os.path.join(settings.STORAGE_DIR, _report_filename(run_id))


def _collect_all_matches(result: dict) -> list:
    return (
        result.get("EXACT_MATCHES", []) +
        result.get("FUZZY_MATCHES", []) +
        result.get("MEMORY_MATCHES", []) +
        result.get("AI_MATCHES", [])
    )

@app.task(bind=True, max_retries=3)
def process_pre_data(self, statements_data, ledgers_data):
    try:
        with get_session() as session:
            success = PushBankRecData.push_all_data(
                session=session,
                statements_data=statements_data,
                ledgers_data=ledgers_data
            )
            return {"status": "success" if success else "failed"}
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)

@app.task(bind=True, max_retries=3)
def process_post_data(self, matches_data, ignored_data, audit_data):
    try:
        with get_session() as session:
            success = PushBankRecData.push_reconciliation_results(
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

    def _cleanup_input_files():
        if os.path.exists(ledger_path):
            os.remove(ledger_path)
        if os.path.exists(bank_path):
            os.remove(bank_path)

    try:
        ledger_data = load_ledger(filepath=ledger_path, date_format="%d-%m-%Y")
        bank_data = load_bank_statement(filepath=bank_path)

        gl_records = ledger_data.get("records", [])
        bank_records = bank_data.get("records", [])

        statements_list = _serialize_for_celery(bank_records)
        ledgers_list = _serialize_for_celery(gl_records)

        db_run_id = None
        try:
            with get_session() as session:
                run_row = PushBankRecData.create_run(
                    session,
                    celery_task_id=run_id,
                    bank_name=bank_data.get("bank_name"),
                    template_version=bank_data.get("template_version"),
                    ledger_source=ledger_data.get("source"),
                    bank_csv_path=bank_path,
                    ledger_csv_path=ledger_path,
                )
                if run_row is not None:
                    db_run_id = run_row.id
                    persisted = PushBankRecData.push_all_data(
                        session=session,
                        statements_data=statements_list,
                        ledgers_data=ledgers_list,
                        run_id=db_run_id,
                    )
                    if not persisted:
                        print(f"Ledger/bank persistence failed for run_id={run_id}; "
                              f"match-result FK linkage will be unresolved for this run.")
        except Exception as db_exc:
            print(f"DB run creation/persistence failed for run_id={run_id}: {db_exc}")

        all_warnings = ledger_data.get("warnings", []) + bank_data.get("warnings", [])
        result = reconcile(
            ledger_result=ledger_data,
            bank_result=bank_data,
            all_warnings=all_warnings
        )

        all_matches = _collect_all_matches(result)
        matches_list = _serialize_for_celery(all_matches)
        raw_ignored = _deep_serialize(result.get("IGNORED_METADATA", []))
        for item in raw_ignored:
            if not isinstance(item, dict):
                continue
            if "ledger_id" in item:
                item["row_ref"] = item.pop("ledger_id")
            elif "row_index" in item:
                item["row_ref"] = str(item.pop("row_index"))

        ignored_list = _serialize_for_celery(raw_ignored)
        audit_list = _serialize_for_celery(result.get("AUDIT_INVESTIGATION", []))

        print("\n\n" + "=" * 50)
        print(f"CELERY WORKER: MATCHING COMPLETE for run_id={run_id}! PRINTING RESULTS:")
        print_reconciliation_results(results=result)
        print("=" * 50 + "\n\n")

        if db_run_id is not None:
            try:
                with get_session() as session:
                    PushBankRecData.push_match_result_rows(
                        session,
                        run_id=db_run_id,
                        timing_matches=result.get("RESIDUAL_TIMING_MATCHES", []),
                        split_matches=result.get("RESIDUAL_SPLIT_MATCHES", []),
                        suggested_journal_entries=result.get("SUGGESTED_JOURNAL_ENTRIES", []),
                        other_matches=all_matches,
                    )
                    PushBankRecData.update_run_summary(session, db_run_id, result.get("summary", {}))
            except Exception as db_exc:
                print(f"Match-result persistence failed for run_id={run_id}: {db_exc}")

            if ignored_list:
                try:
                    with get_session() as session:
                        if not PushBankRecData.push_ignored_records(session, ignored_list):
                            print(f"Ignored-record persistence failed for run_id={run_id}.")
                except Exception as db_exc:
                    print(f"Ignored-record persistence failed for run_id={run_id}: {db_exc}")

            if audit_list:
                try:
                    with get_session() as session:
                        if not PushBankRecData.push_audit_items(session, audit_list):
                            print(f"Audit-item persistence failed for run_id={run_id}.")
                except Exception as db_exc:
                    print(f"Audit-item persistence failed for run_id={run_id}: {db_exc}")

        report_name = _report_filename(run_id)
        out_path = _report_path(run_id)
        
        try:
            write_bank_recon_xlsx(result, gl_records, bank_records, out_path)
            report_ready = True
        except Exception as report_exc:
            print(f"Report generation failed for run_id={run_id}: {report_exc}")
            report_name = None
            report_ready = False

        _cleanup_input_files()

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
        max_retries = self.max_retries if self.max_retries is not None else 3
        if self.request.retries >= max_retries:
            _cleanup_input_files()
        raise self.retry(exc=e, countdown=10)


def _fetch_run_data(session: Session, user_id: Optional[str], run_id: str):
    from database.bank_renc_model import (
        ReconciliationRunModel,
        LedgerFormatModel,
        BankStatementModel,
        MatchResultModel,
    )
    from schema.bank_renc_schema import LedgerFormat as SchemaLedger, BankStatement as SchemaBank

    run = session.query(ReconciliationRunModel).filter(
        ReconciliationRunModel.celery_task_id == str(run_id),
    )
    if user_id is not None:
        run = run.filter(ReconciliationRunModel.user_id == str(user_id))

    run = run.first()
    if run is None:
        return None

    matches = []
    for mr in run.match_results:
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
def get_data_from_db(self, user_id: Optional[str], run_id: str, filename: str = None):
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
def generate_report_from_db(self, run_id: str, user_id: Optional[str] = None, filename: str = None):
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
    
