import os
import dataclasses
import tempfile

from celery import chord

from app.celery import app
from database.session import get_session
from database.bank_renc_model import ReconciliationRunModel, LedgerSource
from service import PushEntryPointData
from service.run_result_service import fetch_run_bundle, mark_run_status
from matcher import reconcile
from entry_point.loader import load_bank_statement, load_ledger
from schema import BankStatement, LedgerFormat, LedgerSource as SchemaLedgerSource
from sqlalchemy.exc import ProgrammingError

from matcher.test import print_reconciliation_results
from service import write_bank_recon_xlsx


QUEUE_DISPATCH = "queue_dispatch"       # run_reconciliation_pipeline (thin orchestrator)
QUEUE_PREPROCESS = "queue_preprocess"   # process_pre_data
QUEUE_RECONCILE = "queue_reconcile"     # run_matching
QUEUE_POSTPROCESS = "queue_postprocess" # finalize_reconciliation


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


def _serialize_loader_payload(data: dict) -> dict:

    safe = dict(data)
    if "records" in safe:
        safe["records"] = _serialize_for_celery(safe["records"])
    return safe


def _rehydrate_loader_payload(data: dict, record_cls) -> dict:
    safe = dict(data)
    records = []
    for item in safe.get("records", []):
        if isinstance(item, record_cls):
            records.append(item)
            continue
        if not isinstance(item, dict):
            records.append(item)
            continue

        payload = dict(item)
        if record_cls is LedgerFormat and isinstance(payload.get("source"), str):
            try:
                payload["source"] = SchemaLedgerSource(payload["source"])
            except ValueError:
                payload["source"] = SchemaLedgerSource.MANUAL

        valid_fields = getattr(record_cls, "__dataclass_fields__", {})
        payload = {k: v for k, v in payload.items() if k in valid_fields}
        records.append(record_cls(**payload))

    safe["records"] = records
    return safe


@app.task(bind=True, max_retries=3, queue=QUEUE_PREPROCESS)
def process_pre_data(self, statements_data, ledgers_data, run_id=None):
    """
    STAGE 1a (parallel) — bulk-inserts the raw loaded CSV rows into the DB.
    Runs on its own worker (queue_preprocess), concurrently with
    run_matching below.
    """
    try:
        with get_session() as session:
            success = PushEntryPointData.push_all_data(
                session=session,
                statements_data=statements_data,
                ledgers_data=ledgers_data,
                run_id=run_id,
            )
        if not success:
            raise RuntimeError(f"push_all_data failed for run_id={run_id}")
        return {"status": "success", "stage": "pre_data"}
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            try:
                with get_session() as session:
                    marked = mark_run_status(session, run_id, "failed", error_message=str(exc))
                if not marked:
                    print(f"⚠️  mark_run_status found no row for run_id={run_id} — status left stale.")
            except Exception as mark_exc:
                print(f"⚠️  Could not mark run_id={run_id} as failed: {mark_exc}")
        raise self.retry(exc=exc, countdown=5)


@app.task(bind=True, max_retries=3, queue=QUEUE_RECONCILE)
def run_matching(self, ledger_data, bank_data, all_warnings, run_db_id=None):
    """
    STAGE 1b (parallel) — runs the actual ledger<->bank matching. This only
    needs the in-memory loaded records (not anything process_pre_data
    writes), so it's safe to run at the same time as pre-processing, on its
    own worker (queue_reconcile).
    """
    try:
        ledger_data = _rehydrate_loader_payload(ledger_data, LedgerFormat)
        bank_data = _rehydrate_loader_payload(bank_data, BankStatement)

        result = reconcile(
            ledger_result=ledger_data,
            bank_result=bank_data,
            all_warnings=all_warnings,
        )

        print("\n\n" + "=" * 50)
        print(f"🎯 MATCHING COMPLETE for run_id={run_db_id}! PRINTING RESULTS:")
        print_reconciliation_results(results=result)
        print("=" * 50 + "\n\n")

        return {"status": "success", "stage": "matching", "result": _deep_serialize(result)}
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            try:
                with get_session() as session:
                    marked = mark_run_status(session, run_db_id, "failed", error_message=str(exc))
                if not marked:
                    print(f"⚠️  mark_run_status found no row for run_id={run_db_id} — status left stale.")
            except Exception as mark_exc:
                print(f"⚠️  Could not mark run_id={run_db_id} as failed: {mark_exc}")
        raise self.retry(exc=exc, countdown=5)


@app.task(bind=True, max_retries=3, queue=QUEUE_POSTPROCESS)
def finalize_reconciliation(
    self,
    parallel_results,
    run_db_id=None,
    run_id=None,
    ledger_path=None,
    bank_path=None,
    gl_count=0,
    bank_count=0,
):
    """
    STAGE 2 — the chord callback. Celery only invokes this once BOTH
    process_pre_data and run_matching (the chord's header) have completed;
    `parallel_results` is their return values, in header order:
    [process_pre_data_result, run_matching_result].

    This is the dedicated 3rd worker (queue_postprocess): pushes match
    results, updates summary counters, generates the report, and is the
    ONLY place that marks a run "success" — so if this never runs, or
    fails, the run correctly stays out of "success" state instead of the
    frontend being told everything's fine.
    """
    pre_result, match_result = parallel_results

    try:
        if pre_result.get("status") != "success":
            raise RuntimeError(f"process_pre_data did not succeed for run_id={run_db_id}: {pre_result}")
        if match_result.get("status") != "success":
            raise RuntimeError(f"run_matching did not succeed for run_id={run_db_id}: {match_result}")

        result = match_result["result"]

        all_matches = _collect_all_matches(result)
        matches_list = _serialize_for_celery(all_matches)

        ignored_list = _serialize_for_celery(result.get("IGNORED_METADATA", []))
        for item in ignored_list:
            if "ledger_id" in item:
                item["row_ref"] = item.pop("ledger_id")
            elif "row_index" in item:
                item["row_ref"] = str(item.pop("row_index"))

        audit_list = _serialize_for_celery(result.get("AUDIT_INVESTIGATION", []))

        with get_session() as session:
            success = PushEntryPointData.push_reconciliation_results(
                session=session,
                matches_data=matches_list,
                ignored_data=ignored_list,
                audit_data=audit_list,
                run_id=run_db_id,
            )
            if not success:
                raise RuntimeError(f"push_reconciliation_results failed for run_id={run_db_id}")

            PushEntryPointData.update_run_summary(
                session,
                run_db_id,
                ledger_records=gl_count,
                bank_records=bank_count,
                exact_matches=len(result.get("EXACT_MATCHES", [])),
                fuzzy_matches=len(result.get("FUZZY_MATCHES", [])) + len(result.get("MEMORY_MATCHES", [])),
                ai_matches=len(result.get("AI_MATCHES", [])),
                unreconciled_ledger=len(result.get("UNRECONCILED_ITEMS", {}).get("ledger", [])),
                unreconciled_bank=len(result.get("UNRECONCILED_ITEMS", {}).get("bank", [])),
            )

            marked = mark_run_status(session, run_db_id, "success")
            if not marked:
                raise RuntimeError(f"mark_run_status: no row found for run_id={run_db_id}")

        report_name = _report_filename(run_id)
        out_path = _report_path(run_id)
        report_ready = False
        try:
            with get_session() as session:
                bundle = fetch_run_bundle(session, run_id)
            if bundle is not None:
                recon_result, gl_objs, bank_objs = bundle
                write_bank_recon_xlsx(recon_result, gl_objs, bank_objs, out_path)
                report_ready = True
            else:
                report_name = None
        except Exception as report_exc:
            print(f"⚠️  Report generation failed for run_id={run_id}: {report_exc}")
            report_name = None
            report_ready = False

        if ledger_path and os.path.exists(ledger_path):
            os.remove(ledger_path)
        if bank_path and os.path.exists(bank_path):
            os.remove(bank_path)

        return {
            "status": "success",
            "run_id": run_id,
            "matches_found": len(matches_list),
            "report_ready": report_ready,
            "report_file": report_name,
            "result_url": f"/run_result/{run_id}",
            "download_url": f"/download_report/run/{run_id}" if report_ready else None,
        }

    except Exception as exc:
        if self.request.retries >= self.max_retries:
            try:
                with get_session() as session:
                    marked = mark_run_status(session, run_db_id, "failed", error_message=str(exc))
                if not marked:
                    print(f"⚠️  mark_run_status found no row for run_id={run_db_id} — status left stale.")
            except Exception as mark_exc:
                print(f"⚠️  Could not mark run_id={run_db_id} as failed: {mark_exc}")
            if ledger_path and os.path.exists(ledger_path):
                os.remove(ledger_path)
            if bank_path and os.path.exists(bank_path):
                os.remove(bank_path)
        raise self.retry(exc=exc, countdown=5)


@app.task(bind=True, max_retries=3, queue=QUEUE_DISPATCH)
def run_reconciliation_pipeline(self, ledger_path, bank_path, user_id=None):
    """
    Thin dispatcher only, now. Loads the files, creates/reuses the run row,
    then fans out pre-processing and matching to run CONCURRENTLY on two
    separate workers via a Celery chord — finalize_reconciliation (a 3rd,
    separate worker) fires only once both are done.
    """
    run_id = self.request.id
    print(f"DEBUG: Task received! run_id={run_id} Processing: {ledger_path}")

    try:
        ledger_data = load_ledger(filepath=ledger_path, date_format="%d-%m-%Y")
        bank_data = load_bank_statement(filepath=bank_path)

        gl_records = ledger_data.get("records", [])
        bank_records = bank_data.get("records", [])

        statements_list = _serialize_for_celery(bank_records)
        ledgers_list = _serialize_for_celery(gl_records)

        with get_session() as session:
            existing = session.query(ReconciliationRunModel).filter(
                ReconciliationRunModel.task_id == run_id
            ).first()
            if existing is not None:
                db_run = existing
                run_db_id = db_run.id
            else:
                try:
                    db_run = ReconciliationRunModel(
                        task_id=run_id,
                        bank_name=bank_data.get("bank_name"),
                        template_version=bank_data.get("template_version"),
                        bank_csv_path=bank_path,
                        ledger_csv_path=ledger_path,
                        ledger_source=LedgerSource.MANUAL.value,
                        user_id=str(user_id) if user_id else None,
                    )
                    session.add(db_run)
                    session.commit()
                    run_db_id = db_run.id
                except ProgrammingError as exc:
                    if "task_id" in str(exc).lower() and "reconciliation_run" in str(exc).lower():
                        session.rollback()
                        db_run = ReconciliationRunModel(
                            bank_name=bank_data.get("bank_name"),
                            template_version=bank_data.get("template_version"),
                            bank_csv_path=bank_path,
                            ledger_csv_path=ledger_path,
                            ledger_source=LedgerSource.MANUAL.value,
                            user_id=str(user_id) if user_id else None,
                        )
                        session.add(db_run)
                        session.commit()
                        run_db_id = db_run.id
                    else:
                        raise

        all_warnings = ledger_data.get("warnings", []) + bank_data.get("warnings", [])

        ledger_payload = _serialize_loader_payload(ledger_data)
        bank_payload = _serialize_loader_payload(bank_data)

        header = [
            process_pre_data.s(
                statements_data=statements_list,
                ledgers_data=ledgers_list,
                run_id=run_db_id,
            ),
            run_matching.s(
                ledger_data=ledger_payload,
                bank_data=bank_payload,
                all_warnings=all_warnings,
                run_db_id=run_db_id,
            ),
        ]
        callback = finalize_reconciliation.s(
            run_db_id=run_db_id,
            run_id=run_id,
            ledger_path=ledger_path,
            bank_path=bank_path,
            gl_count=len(gl_records),
            bank_count=len(bank_records),
        )
        chord(header)(callback)

        return {
            "status": "dispatched",
            "run_id": run_id,
            "result_url": f"/run_result/{run_id}",
            "download_url": f"/download_report/run/{run_id}",
        }

    except Exception as e:
        if self.request.retries >= self.max_retries:
            try:
                with get_session() as session:
                    mark_run_status(session, run_id, "failed", error_message=str(e))
            except Exception as mark_exc:
                print(f"⚠️  Could not mark run_id={run_id} as failed: {mark_exc}")
            if os.path.exists(ledger_path):
                os.remove(ledger_path)
            if os.path.exists(bank_path):
                os.remove(bank_path)
        raise self.retry(exc=e, countdown=10)


@app.task(bind=True)
def get_data_from_db(self, user_id: int, run_id: str, filename: str = None):
    try:
        with get_session() as session:
            bundle = fetch_run_bundle(session, run_id, user_id)
            if bundle is None:
                return {"status": "error", "message": "Reconciliation run not found or unauthorized."}
            recon_result, _gl_objs, _bank_objs = bundle
            return recon_result
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)


@app.task(bind=True)
def generate_report_from_db(self, run_id: str, user_id: int = None, filename: str = None):

    try:
        with get_session() as session:
            bundle = fetch_run_bundle(session, run_id, user_id)
            if bundle is None:
                return {"status": "error", "message": "Reconciliation run not found or unauthorized."}
            recon_result, gl_objs, bank_objs = bundle

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
    
