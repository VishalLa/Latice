from typing import List, Dict, Any, Optional
from datetime import date, datetime
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from database.bank_renc_model import (
    LedgerSource,
    LedgerFormatModel,
    BankStatementModel,
    IgnoredMetadataRecordModel,
    AuditInvestigationItemModel,
    MatchPatternModel,
    MatchResultModel,
    ReconciliationRunModel,
)

def _coerce_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None

def _coerce_row_dates(data: Dict[str, Any], date_fields: List[str]) -> Dict[str, Any]:
    out = dict(data)
    for f in date_fields:
        if f in out:
            out[f] = _coerce_date(out[f])
    return out

class PushBankRecData:

    @staticmethod
    def create_run(
        session: Session,
        celery_task_id: Optional[str] = None,
        bank_name: Optional[str] = None,
        template_version: Optional[str] = None,
        ledger_source: Optional[str] = None,
        bank_csv_path: Optional[str] = None,
        ledger_csv_path: Optional[str] = None,
    ) -> Optional[ReconciliationRunModel]:
        try:
            run = ReconciliationRunModel(
                celery_task_id=celery_task_id,
                bank_name=bank_name,
                template_version=template_version,
                ledger_source=ledger_source,
                bank_csv_path=bank_csv_path,
                ledger_csv_path=ledger_csv_path,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return run
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while creating reconciliation run: {e}")
            return None

    @staticmethod
    def update_run_summary(session: Session, run_id: int, summary: Dict[str, Any]) -> bool:
        try:
            run = session.query(ReconciliationRunModel).filter_by(id=run_id).first()
            if run is None:
                print(f"update_run_summary: no run found for run_id={run_id}")
                return False
            field_map = {
                "ledger_records": "ledger_record_count",  # summary dict key -> model column
                "bank_records": "bank_records",
                "exact_matches": "exact_matches",
                "fuzzy_matches": "fuzzy_matches",
                "ai_matches": "ai_matches",
                "unreconciled_ledger": "unreconciled_ledger",
                "unreconciled_bank": "unreconciled_bank",
            }
            for summary_key, column_name in field_map.items():
                if summary_key in summary and summary[summary_key] is not None:
                    setattr(run, column_name, summary[summary_key])
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while updating run summary: {e}")
            return False

    @staticmethod 
    def push_bank_statements(session: Session, statements_data: List[Dict[str, Any]], run_id: Optional[int] = None) -> bool:
        try:
            db_statements = [
                BankStatementModel(**{**_coerce_row_dates(data, ["date"]), "run_id": run_id})
                for data in statements_data
            ]
            
            session.add_all(db_statements)
            session.commit()
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting bank statements: {e}")
            return False

    @staticmethod 
    def push_ledgers(session: Session, ledgers_data: List[Dict[str, Any]], run_id: Optional[int] = None) -> bool:
        try:
            db_ledgers = []
            for data in ledgers_data:
                if 'source' in data and isinstance(data['source'], str):
                    data['source'] = LedgerSource(data['source'])
                data = _coerce_row_dates(data, ["transaction_date"])
                db_ledgers.append(LedgerFormatModel(**{**data, "run_id": run_id}))
                
            session.add_all(db_ledgers)
            session.commit()
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting ledgers: {e}")
            return False

    @staticmethod
    def push_all_data(
        session: Session,
        statements_data: List[Dict[str, Any]],
        ledgers_data: List[Dict[str, Any]],
        run_id: Optional[int] = None,
    ) -> bool:
        try:
            db_statements = [
                BankStatementModel(**{**_coerce_row_dates(data, ["date"]), "run_id": run_id})
                for data in statements_data
            ]
            
            db_ledgers = []
            for data in ledgers_data:
                if 'source' in data and isinstance(data['source'], str):
                    data['source'] = LedgerSource(data['source'])
                data = _coerce_row_dates(data, ["transaction_date"])
                db_ledgers.append(LedgerFormatModel(**{**data, "run_id": run_id}))
            
            session.add_all(db_statements)
            session.add_all(db_ledgers)
            session.commit()
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting reconciliation data: {e}")
            return False
        
    @staticmethod
    def push_match_results(session: Session, matches_data: List[Dict[str, Any]]) -> bool:
        try:
            db_matches = [MatchPatternModel(**data) for data in matches_data]
            session.add_all(db_matches)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting match results: {e}")
            return False

    @staticmethod
    def push_match_result_rows(
        session: Session,
        run_id: int,
        timing_matches: Optional[List[Dict[str, Any]]] = None,
        split_matches: Optional[List[Dict[str, Any]]] = None,
        suggested_journal_entries: Optional[List[Dict[str, Any]]] = None,
        other_matches: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        timing_matches = timing_matches or []
        split_matches = split_matches or []
        suggested_journal_entries = suggested_journal_entries or []
        other_matches = other_matches or []
        if not (timing_matches or split_matches or suggested_journal_entries or other_matches):
            return True

        try:
            ledger_lookup: Dict[str, int] = {
                ledger_id: pk for pk, ledger_id in
                session.query(LedgerFormatModel.id, LedgerFormatModel.ledger_id)
                       .filter(LedgerFormatModel.run_id == run_id).all()
            }
            bank_lookup: Dict[int, int] = {
                row_index: pk for pk, row_index in
                session.query(BankStatementModel.id, BankStatementModel.row_index)
                       .filter(BankStatementModel.run_id == run_id).all()
            }

            unresolved: List[str] = []

            def resolve_ledger(business_key: Optional[str]) -> Optional[int]:
                if business_key is None:
                    return None
                fk = ledger_lookup.get(business_key)
                if fk is None:
                    unresolved.append(f"ledger_id={business_key!r}")
                return fk

            def resolve_bank(business_key: Any) -> Optional[int]:
                if business_key is None:
                    return None
                try:
                    business_key = int(business_key)
                except (TypeError, ValueError):
                    unresolved.append(f"bank_id={business_key!r} (not an int)")
                    return None
                fk = bank_lookup.get(business_key)
                if fk is None:
                    unresolved.append(f"bank_id={business_key!r}")
                return fk

            db_rows: List[MatchResultModel] = []

            def base_row(m: Dict[str, Any], match_type: str, ledger_fk, bank_fk, amount, details) -> MatchResultModel:
                return MatchResultModel(
                    run_id=run_id,
                    ledger_format_id=ledger_fk,
                    bank_statement_id=bank_fk,
                    match_type=match_type,
                    adjustment_type=m.get("adjustment_type"),
                    confidence_score=str(m.get("confidence_score")) if m.get("confidence_score") is not None else None,
                    matched_amount=amount,
                    matched_date=_coerce_date(m.get("date")),
                    details=details,
                )

            def expand_and_add(m: Dict[str, Any], default_match_type: str) -> None:
                match_type = m.get("match_phase", default_match_type)
                details = m.get("details")
                raw_ledger_id = m.get("ledger_id")
                raw_bank_id = m.get("bank_id")

                if "ledger_components" in m:
                    bank_fk = resolve_bank(raw_bank_id)
                    for comp in m["ledger_components"]:
                        db_rows.append(base_row(
                            m, match_type, resolve_ledger(comp.get("ledger_id")), bank_fk,
                            comp.get("amount"), details,
                        ))
                    return

                if "bank_components" in m:
                    ledger_fk = resolve_ledger(raw_ledger_id)
                    for comp in m["bank_components"]:
                        db_rows.append(base_row(
                            m, match_type, ledger_fk, resolve_bank(comp.get("bank_id")),
                            comp.get("amount"), details,
                        ))
                    return

                ledger_parts = [p.strip() for p in str(raw_ledger_id).split(" & ")] if raw_ledger_id else [None]
                bank_parts = [p.strip() for p in str(raw_bank_id).split(" & ")] if raw_bank_id is not None else [None]
                if len(ledger_parts) == 1 and len(bank_parts) == 1:
                    db_rows.append(base_row(
                        m, match_type, resolve_ledger(ledger_parts[0]), resolve_bank(bank_parts[0]),
                        m.get("amount"), details,
                    ))
                    return

                joined_note = (
                    (details + " " if details else "") +
                    "(expanded from a joined multi-id match; per-row amount is the "
                    "combined total, not this row's individual share.)"
                )
                if len(bank_parts) == 1:
                    bank_fk = resolve_bank(bank_parts[0])
                    for lp in ledger_parts:
                        db_rows.append(base_row(m, match_type, resolve_ledger(lp), bank_fk, m.get("amount"), joined_note))
                else:
                    ledger_fk = resolve_ledger(ledger_parts[0])
                    for bp in bank_parts:
                        db_rows.append(base_row(m, match_type, ledger_fk, resolve_bank(bp), m.get("amount"), joined_note))

            for m in timing_matches:
                expand_and_add(m, "residual_timing")
            for m in split_matches:
                expand_and_add(m, "residual_split")
            for m in other_matches:
                expand_and_add(m, "match")

            for d in suggested_journal_entries:
                narrative = (
                    f"DRAFT ({d.get('status', 'pending_review')}, "
                    f"confidence {d.get('confidence')}, source {d.get('source')}): "
                    f"{d.get('debit_account')} Dr / {d.get('credit_account')} Cr - "
                    f"{d.get('entry_narrative')}"
                )
                db_rows.append(MatchResultModel(
                    run_id=run_id,
                    ledger_format_id=None,
                    bank_statement_id=resolve_bank(d.get("bank_id")),
                    match_type="residual_draft",
                    adjustment_type=f"{d.get('debit_account')} Dr / {d.get('credit_account')} Cr",
                    confidence_score=str(d.get("confidence")) if d.get("confidence") is not None else None,
                    matched_amount=d.get("amount"),
                    details=narrative,
                ))

            session.add_all(db_rows)
            session.commit()

            if unresolved:
                print(
                    f"push_match_result_rows: wrote {len(db_rows)} row(s) for run_id={run_id}, "
                    f"but {len(unresolved)} business key(s) could not be resolved to a DB row "
                    f"(row written with that side's FK left NULL): {unresolved[:10]}"
                    + (" ...(truncated)" if len(unresolved) > 10 else "")
                )
            return True

        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting match result rows: {e}")
            return False

    @staticmethod
    def push_ignored_records(session: Session, ignored_data: List[Dict[str, Any]]) -> bool:
        try:
            db_ignored = [IgnoredMetadataRecordModel(**data) for data in ignored_data]
            session.add_all(db_ignored)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting ignored records: {e}")
            return False

    @staticmethod
    def push_audit_items(session: Session, audit_data: List[Dict[str, Any]]) -> bool:
        try:
            db_audit_items = [AuditInvestigationItemModel(**data) for data in audit_data]
            session.add_all(db_audit_items)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting audit items: {e}")
            return False

    @staticmethod
    def push_reconciliation_results(
        session: Session, 
        matches_data: List[Dict[str, Any]], 
        ignored_data: List[Dict[str, Any]], 
        audit_data: List[Dict[str, Any]]
    ) -> bool:
        try:
            db_matches = [MatchPatternModel(**data) for data in matches_data]            

            db_ignored = [IgnoredMetadataRecordModel(**data) for data in ignored_data]
            
            db_audit = [AuditInvestigationItemModel(**data) for data in audit_data]
            
            session.add_all(db_matches)
            session.add_all(db_ignored)
            session.add_all(db_audit)
            
            session.commit()
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting complete reconciliation results: {e}")
            return False
        
