from __future__ import annotations

import os
import dataclasses
from datetime import date as date_
from typing import Optional, Any, List, Dict

from sqlalchemy.orm import Session

from matcher import reconcile
from ledger import TDSEngine, GSTR1Builder, JournalBuilder
from database import BillModel

from entry_point.loader import load_bank_statement, load_ledger
from entry_point.data_extractor import classify_direction, detect_type, parse_invoice
from entry_point.ocr import Block, get_ocr

from .store_bank_rec_data import PushBankRecData
from .store_ledger_data import PushLedgerData
from .reports.bank_recon_xlsx import write_bank_recon_xlsx
from .helper import _safe_float, fy_label_for_date
from ._base import Base

class RunBankRec:
    
    def __init__(self) -> None:
        base = Base()
        self.db_manager = base.get_manager
        self.config = base.config
        self.bank_rec_service = PushBankRecData(db_manager=self.db_manager)

    
    @staticmethod
    def _report_filename(run_id: str) -> str:
        return f"bank-recon-{run_id}.xlsx"
    
    
    @staticmethod
    def _collect_all_matches(result: dict) -> list:
        return (
            result.get("EXACT_MATCHES", []) +
            result.get("FUZZY_MATCHES", []) +
            result.get("MEMORY_MATCHES", []) +
            result.get("AI_MATCHES", [])
        )
    
    
    def _report_path(self, run_id: str) -> str:
        return os.path.join(self.config.STORAGE_DIR, self._report_filename(run_id))
    
    
    @staticmethod
    def _serialize_for_celery(data_list):
        if not data_list:
            return []
        if dataclasses.is_dataclass(data_list[0]):
            return [dataclasses.asdict(item) for item in data_list]
        elif hasattr(data_list[0], "to_dict"):
            return [item.to_dict() for item in data_list]
        return data_list
    
    
    @staticmethod
    def _deep_serialize(obj: Any) -> Any:
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj

        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)

        if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
            try:
                return RunBankRec._deep_serialize(obj.to_dict())
            except Exception:
                pass

        if isinstance(obj, dict):
            return {
                k: RunBankRec._deep_serialize(v) 
                for k, v in obj.items()
            }

        if isinstance(obj, (list, tuple, set)):
            return [
                RunBankRec._deep_serialize(i) 
                for i in obj
            ]

        try:
            attrs = getattr(obj, "__dict__", None)
            if isinstance(attrs, dict):
                return {
                    k: RunBankRec._deep_serialize(v) 
                    for k, v in attrs.items() 
                    if not k.startswith("_")
                }
        except Exception:
            pass

        return str(obj)
    
    
    @staticmethod
    def _cleanup_input_files(
        ledger_path: str, 
        bank_path: str
    ) -> None:
        if os.path.exists(ledger_path):
            os.remove(ledger_path)
        if os.path.exists(bank_path):
            os.remove(bank_path)
    
    
    def process_pre_data(
        self,
        statements_data: List[Dict[str, Any]],
        ledgers_data: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        success = self.bank_rec_service.push_all_data(
            statements_data=statements_data,
            ledgers_data=ledgers_data,
        )
        return {"status": "success" if success else "failed"}
    
    
    def process_post_data(
        self, 
        matches_data: List[Dict[str, Any]], 
        ignored_data: List[Dict[str, Any]], 
        audit_data: List[Dict[str, Any]]
    ) -> Dict[str, str]:
        success = self.bank_rec_service.push_reconciliation_results(
            matches_data=matches_data,
            ignored_data=ignored_data,
            audit_data=audit_data
        )
        return {"status": "success" if success else "failed"}
        
        
    def run_reconciliation_pipeline(
        self,
        run_id: str,
        ledger_path: str,
        bank_path: str,
        date_format: str = "%d-%m-%Y"
    ) -> Dict[str, Any]:
        """
        Full pipeline: load files → reconcile → persist → generate report.
        
        Deliberately raises on failure rather than swallowing into a celery
        retry — retry semantics belong to the @app.task wrapper (which has
        access to `self.retry`/`self.request.retries`), not to this service.
        The wrapper is responsible for catching exceptions from this method
        and deciding whether/how to retry; see the module docstring at the
        bottom of this file for the expected wrapper shape.
        """
        print(f"DEBUG: Task received! run_id={run_id} Processing: {ledger_path}")
        
        ledger_data = load_ledger(filepath=ledger_path, date_format=date_format)
        bank_data = load_bank_statement(filepath=bank_path)
        
        gl_records = ledger_data.get("records", [])
        bank_records = bank_data.get("records", [])
        
        statements_list = self._serialize_for_celery(bank_records)
        ledgers_list = self._serialize_for_celery(gl_records)
        
        db_run_id = None
        try:
            run_row = self.bank_rec_service.create_run(
                celery_task_id=run_id,
                bank_name=bank_data.get("bank_name"),
                template_version=bank_data.get("template_version"),
                ledger_source=ledger_data.get("source"),
                bank_csv_path=bank_path,
                ledger_csv_path=ledger_path,
            )
            
            if run_row is not None:
                db_run_id = run_row.id
                
                persisted = self.bank_rec_service.push_all_data(
                    statements_data=statements_list,
                    ledgers_data=ledgers_list,
                    run_id=db_run_id
                )
                
                if not persisted:
                    print(f"Ledger/bank persistence failed for run_id={run_id}; "
                    f"match-result FK linkage will be unresolved for this run.")
            
        except Exception as e:
            print(f"DB run creation/persistence failed for run_id={run_id}: {e}")
                    
        
        all_warnings = ledger_data.get("war")
        all_warnings = ledger_data.get("warnings", []) + bank_data.get("warnings", [])
        result = reconcile(
            config=self.config,
            ledger_result=ledger_data,
            bank_result=bank_data,
            all_warnings=all_warnings,
        )
        
        all_matches = self._collect_all_matches(result)
        matches_list = self._serialize_for_celery(all_matches)
        raw_ignored = self._deep_serialize(result.get("IGNORED_METADATA", []))
        
        for item in raw_ignored:
            if not isinstance(item, dict):
                continue
            
            if "ledger_id" in item:
                item["row_ref"] = item.pop("ledger_id")
            elif "row_index" in item:
                item["row_ref"] = str(item.pop("row_index"))
                
        ignored_list = self._serialize_for_celery(raw_ignored)
        audit_list = self._serialize_for_celery(result.get("AUDIT_INVESTIGATION", []))
        
        # print("\n\n" + "=" * 50)
        # print(f"CELERY WORKER: MATCHING COMPLETE for run_id={run_id}! PRINTING RESULTS:")
        # print_reconciliation_results(results=result)
        # print("=" * 50 + "\n\n")
        
        if db_run_id is not None:
            try:
                self.bank_rec_service.push_match_result_rows(
                    run_id=db_run_id,
                    timing_matches=result.get("RESIDUAL_TIMING_MATCHES", []),
                    split_matches=result.get("RESIDUAL_SPLIT_MATCHES", []),
                    suggested_journal_entries=result.get("SUGGESTED_JOURNAL_ENTRIES", []),
                    other_matches=all_matches,
                )
                self.bank_rec_service.update_run_summary(
                    run_id=db_run_id,
                    summary=result.get("summary", {})
                )
                
            except Exception as e:
                print(f"Match-result persistence failed for run_id={run_id}: {e}")
                
            if ignored_list:
                try:
                    if not self.bank_rec_service.push_ignored_records(ignored_data=ignored_list):
                        print(f"Ignored-record persistence failed for run_id={run_id}.")
                except Exception as e:
                    print(f"Ignored-record persistence failed for run_id={run_id}: {e}")
                    
            if audit_list:
                try:
                    if not self.bank_rec_service.push_audit_items(audit_data=audit_list):
                        print(f"Audit-item persistence failed for run_id={run_id}.")
                except Exception as e:
                    print(f"Audit-item persistence failed for run_id={run_id}: {e}")
                    
        
        report_name = self._report_filename(run_id)
        out_path = self._report_path(run_id)
        
        try:
            write_bank_recon_xlsx(result, gl_records, bank_records, out_path)
            report_ready = True
        except Exception as report_exc:
            print(f"Report generation failed for run_id={run_id}: {report_exc}")
            report_name = None
            report_ready = False
        
        
        self._cleanup_input_files(
            ledger_path=ledger_path,
            bank_path=bank_path
        )
        
        safe_result = self._deep_serialize(result)
        
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


class RunBill:
    def __init__(self) -> None:
        base = Base()
        self.db_manager = base.get_manager
        self.config = base.config
        self.ledger_service = PushLedgerData(db_manager=self.db_manager)
        
    
    @staticmethod
    def _normalize_bill_dict(
        bill_dict: dict,
        blocks: List[Block]=None,
        source_file: Optional[str] = None,
        fallback_direction: Optional[str] = None,        
    ) -> dict:
        if "direction" in bill_dict or "_direction" in bill_dict:
            direction = bill_dict.get("_direction") or bill_dict.get("direction")
            
        elif blocks is not None:
            inv_type = detect_type(blocks)
            direction = classify_direction(bill_dict, inv_type)
        else:
            direction = fallback_direction or "input"
            
        bill_dict["direction"] = direction
        bill_dict["_direction"] = direction
        
        if "_status" not in bill_dict:
            bill_dict["_status"] = "ok" if _safe_float(bill_dict.get("grand_total")) > 0 else "failed"
        
        if source_file and "_source_file" not in bill_dict:
            bill_dict["_source_file"] = source_file
        
        return bill_dict
    
    
    @staticmethod
    def _run_ocr_and_extract(image_path: str) -> dict:
        ocr = get_ocr()
        blocks = ocr.ocr_image(image_path)
        bill_dict = parse_invoice(blocks)
        return RunBill._normalize_bill_dict(bill_dict, blocks=blocks, source_file=image_path)


    def create_bill(
        self,
        user_id: str,
        direction: str,
        source_file: Optional[str] = None,
        raw_extracted_data: Optional[dict] = None,
    ) -> str:
        def _op(session: Session) -> str:
            bill = BillModel(
                user_id=user_id,
                direction=direction,
                source_file=source_file,
                raw_extracted_data=raw_extracted_data,
                status="pending",
            )
            session.add(bill)
            session.flush()
            return bill.id

        return self.db_manager.run(_op)
    
    
    def process_bill(self, bill_id: str) -> Dict[str, Any]:

        def _op(session: Session) -> Dict[str, Any]:
            bill = session.query(BillModel).filter(
                BillModel.id == bill_id
            ).first()
            if bill is None:
                return {
                    "status": "error",
                    "message": f"Bill {bill_id} not found"
                }

            try:
                bill_dict = bill.raw_extracted_data
                if not bill_dict:
                    if not bill.source_file:
                        raise ValueError("Bill has neither raw_extracted_data nor source_file")

                    bill_dict = self._run_ocr_and_extract(bill.source_file)
                    bill.raw_extracted_data = bill_dict
                    session.commit()
                
                else:
                    bill_dict = self._normalize_bill_dict(
                        bill_dict=bill_dict,
                        source_file=bill.source_file,
                        fallback_direction=bill.direction
                    )
                    bill.raw_extracted_data = bill_dict

                bill.invoice_number = bill.invoice_number or bill_dict.get("invoice_number")
                bill.vendor_name = bill.vendor_name or bill_dict.get("vendor_name")
                bill.direction = bill_dict.get("_direction", bill.direction or "input")

                journal_entry = JournalBuilder(bill=bill_dict).to_journal_entry()
                if journal_entry is None:
                    bill.status = "failed"
                    bill.error_message = "Could not build a journal entry from this bill (bad/incomplete extraction)."
                    session.commit()
                    return {
                        "status": "failed",
                        "bill_id": bill_id,
                        "reason": bill.error_message
                    }

                tds_engine = TDSEngine(financial_year=fy_label_for_date(journal_entry.date))

                self.ledger_service.prime_tds_engine_aggregates(
                    session=session,
                    user_id=bill.user_id,
                    deductee_name=bill_dict.get("vendor_name") or bill_dict.get("buyer_name") or "Unknown Vendor",
                    financial_year=tds_engine.financial_year,
                    tds_engine=tds_engine
                )

                tds_result = tds_engine.process_bill(
                    bill=bill_dict,
                    journal_entry=journal_entry
                )
                
                self.ledger_service.persist_tds_engine_aggregates(
                    session=session,
                    user_id=bill.user_id,
                    financial_year=tds_engine.financial_year,
                    tds_engine=tds_engine,
                )

                journal_model = self.ledger_service.push_journal_entry(
                    session=session,
                    user_id=bill.user_id,
                    journal_entry=tds_result.journal_entry,
                    bill=bill
                )
                if journal_model is None:
                    raise RuntimeError("Failed to persist journal entry")

                tds_model = None
                if tds_result.tds_applied and tds_result.tds_entry is not None:
                    tds_model = self.ledger_service.push_tds_entry(
                        session=session,
                        user_id=bill.user_id,
                        tds_entry=tds_result.tds_entry,
                        journal_entry_model=journal_model,
                    )

                bill.status = "processed"
                bill.error_message = None
                session.commit()

                return {
                    "status": "success",
                    "bill_id": bill_id,
                    "journal_entry_id": journal_model.entry_id,
                    "tds_applied": bool(tds_model),
                    "tds_entry_id": tds_model.entry_id if tds_model else None,
                    "warnings": tds_result.warnings,
                }

            except Exception as e:
                bill.status = "failed"
                bill.error_message = str(e)
                session.commit()
                raise
            
        return self.db_manager.run(_op)

        
        
    def generate_gstr1(
        self,
        user_id: str,
        period_label: str,
        period_start: str,   # ISO "YYYY-MM-DD"
        period_end: str,     # ISO "YYYY-MM-DD"
    ) -> Dict[str, Any]:

        def _op(session: Session) -> Dict[str, Any]:
            start = date_.fromisoformat(period_start)
            end = date_.fromisoformat(period_end)

            bills = session.query(BillModel).filter(
                BillModel.user_id == user_id,
                BillModel.direction == "output",
                BillModel.status == "processed",
                BillModel.bill_date >= start,
                BillModel.bill_date <= end
            ).all()

            bill_dicts = [
                self._normalize_bill_dict(
                    bill_dict=dict(b.raw_extracted_data),
                    source_file=b.source_file,
                    fallback_direction=b.direction
                ) for b in bills if b.raw_extracted_data
            ]

            gstr1 = GSTR1Builder(bill_dicts).build(period_label=period_label)

            ok = self.ledger_service.replace_gstr1_period(
                session=session,
                user_id=user_id,
                period_label=period_label,
                gstr1=gstr1
            )

            return {
                "status": "success" if ok else "failed",
                "period_label": period_label,
                "bills_considered": len(bill_dicts),
                "totals": gstr1.get("totals", {}),
                "warnings": gstr1.get("warnings", []),
            }

        return self.db_manager.run(_op)
        