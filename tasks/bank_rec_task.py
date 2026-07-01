import os 
import dataclasses
from app.celery import app
from database.session import get_session
from service import PushEntryPointData
from matcher import reconcile
from entry_point.loader import load_bank_statement, load_ledger

from matcher.test import print_reconciliation_results


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
    This background task reads the files, triggers DB saving tasks,
    runs the matcher, triggers post-data DB saving, and returns the result.
    """
    print(f"DEBUG: Task received! Processing: {ledger_path}")

    def _serialize_for_celery(data_list):
        if not data_list:
            return []
        # If the items are dataclasses, convert them to dicts
        if dataclasses.is_dataclass(data_list[0]):
            return [dataclasses.asdict(item) for item in data_list]
        # If they have a custom to_dict method
        elif hasattr(data_list[0], 'to_dict'):
            return [item.to_dict() for item in data_list]
        # If they are already dicts, just return them
        return data_list

    def _deep_serialize(obj):
        """Recursively serialize objects to JSON-friendly structures.

        Handles dataclasses, objects with `to_dict`, lists, and dicts.
        Falls back to `__dict__` or `str(obj)` when necessary.
        """
        # Primitives
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj

        # Dataclass instances
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)

        # Objects that provide a to_dict helper
        if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
            try:
                return _deep_serialize(obj.to_dict())
            except Exception:
                pass

        # Dicts and lists
        if isinstance(obj, dict):
            return {k: _deep_serialize(v) for k, v in obj.items()}

        if isinstance(obj, (list, tuple, set)):
            return [_deep_serialize(i) for i in obj]

        # Fallback: try __dict__ then string
        try:
            data = getattr(obj, "__dict__", None)
            if isinstance(data, dict):
                return {k: _deep_serialize(v) for k, v in data.items() if not k.startswith("_")}
        except Exception:
            pass

        return str(obj)
    
    try:
        ledger_data = load_ledger(filepath=ledger_path, date_format="%d-%m-%Y")
        bank_data = load_bank_statement(filepath=bank_path)
        
        statements_list = _serialize_for_celery(bank_data.get("records", []))
        ledgers_list = _serialize_for_celery(ledger_data.get("records", []))
        
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
        
        matches_list = _serialize_for_celery(result.get("matches", []))
        ignored_list = _serialize_for_celery(result.get("ignored", []))
        audit_list = _serialize_for_celery(result.get("audit_items", []))

        print("\n\n" + "="*50)
        print("🎯 CELERY WORKER: MATCHING COMPLETE! PRINTING RESULTS:")
        print_reconciliation_results(results=result)
        print("="*50 + "\n\n")
        
        process_post_data.delay(
            matches_data=matches_list,
            ignored_data=ignored_list,
            audit_data=audit_list
        )

        if os.path.exists(ledger_path): os.remove(ledger_path)
        if os.path.exists(bank_path): os.remove(bank_path)

        safe_result = _deep_serialize(result)

        return {
            "status": "success",
            "summary": safe_result.get("summary", {}),
            "matches_found": len(matches_list),
            "reconciliation_data": safe_result,
        }

    except Exception as e:
        if os.path.exists(ledger_path): os.remove(ledger_path)
        if os.path.exists(bank_path): os.remove(bank_path)
        raise self.retry(exc=e, countdown=10)
    