from typing import List, Dict, Any, Optional
import calendar
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
from database.journal_model import (
    FiscalPeriodModel,
    JournalEntryModel,
    EntryLineModel,
    DrCr,
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


def _safe_user_id(user_id: Optional[str], max_len: int = 36) -> Optional[str]:
    """
    reconciliation_run.user_id is varchar(36) (sized for a UUID FK). A
    caller-supplied value that's too long previously blew up an unguarded
    commit downstream with StringDataRightTruncation, after the run row
    had *already* been committed — leaving an orphaned row that made every
    retry hit the celery_task_id unique constraint. Validate here instead
    of letting Postgres reject it mid-transaction.
    """
    if user_id is None:
        return None
    user_id = str(user_id).strip()
    if not user_id:
        return None
    if len(user_id) > max_len:
        print(
            f"⚠️  user_id {user_id!r} is {len(user_id)} chars, exceeds the "
            f"{max_len}-char column limit — storing run without a user_id "
            f"instead of failing the whole run."
        )
        return None
    return user_id

def _coerce_row_dates(data: Dict[str, Any], date_fields: List[str]) -> Dict[str, Any]:
    out = dict(data)
    for f in date_fields:
        if f in out:
            out[f] = _coerce_date(out[f])
    return out


def _get_or_create_fiscal_period(session: Session, entry_date: date) -> FiscalPeriodModel:
    """
    JournalEntryModel.period_id is NOT NULL, so posting a journal entry
    always needs a FiscalPeriodModel row for that entry's month. Get the
    existing one (label = "YYYY-MM") or create it — flush (not commit) so
    the caller's transaction stays in charge of when this becomes durable.
    """
    label = entry_date.strftime("%Y-%m")
    period = session.query(FiscalPeriodModel).filter_by(label=label).first()
    if period is not None:
        return period

    start = entry_date.replace(day=1)
    last_day = calendar.monthrange(entry_date.year, entry_date.month)[1]
    end = entry_date.replace(day=last_day)

    period = FiscalPeriodModel(label=label, start_date=start, end_date=end, is_closed=False)
    session.add(period)
    session.flush()  # need period.id below without ending the transaction
    return period


def _build_fk_lookups(session: Session, run_id: Optional[int]) -> "tuple[Dict[str, int], Dict[int, int]]":
    """
    Build business-key -> internal-PK lookup maps for resolving match rows
    to LedgerFormatModel / BankStatementModel foreign keys.

    If run_id is given, lookups are scoped to that run (correct — business
    keys like ledger_id/row_index are only unique within a run). If run_id
    is None, lookups fall back to a global (unscoped) query; this is only
    safe when business keys are unique across the whole table, so callers
    should pass run_id whenever they have it.
    """
    ledger_query = session.query(LedgerFormatModel.id, LedgerFormatModel.ledger_id)
    bank_query = session.query(BankStatementModel.id, BankStatementModel.row_index)
    if run_id is not None:
        ledger_query = ledger_query.filter(LedgerFormatModel.run_id == run_id)
        bank_query = bank_query.filter(BankStatementModel.run_id == run_id)

    ledger_lookup: Dict[str, int] = {ledger_id: pk for pk, ledger_id in ledger_query.all()}
    bank_lookup: Dict[int, int] = {row_index: pk for pk, row_index in bank_query.all()}
    return ledger_lookup, bank_lookup


def _expand_matches_to_rows(
    session: Session,
    run_id: Optional[int],
    timing_matches: Optional[List[Dict[str, Any]]] = None,
    split_matches: Optional[List[Dict[str, Any]]] = None,
    suggested_journal_entries: Optional[List[Dict[str, Any]]] = None,
    other_matches: Optional[List[Dict[str, Any]]] = None,
) -> "tuple[List[MatchResultModel], List[str]]":
    """
    Shared expansion/FK-resolution logic for turning raw match dicts
    (exact/fuzzy/ai/memory/residual matches, plus suggested journal
    entries) into MatchResultModel rows. Used by both push_match_result_rows
    and push_match_results so there's one correct implementation instead of
    two divergent ones.

    Returns (db_rows, unresolved) — does NOT commit; caller owns the
    transaction so it can decide how to batch/commit alongside other work.
    """
    timing_matches = timing_matches or []
    split_matches = split_matches or []
    suggested_journal_entries = suggested_journal_entries or []
    other_matches = other_matches or []

    db_rows: List[MatchResultModel] = []
    unresolved: List[str] = []

    if not (timing_matches or split_matches or suggested_journal_entries or other_matches):
        return db_rows, unresolved

    ledger_lookup, bank_lookup = _build_fk_lookups(session, run_id)

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

    return db_rows, unresolved


class PushEntryPointData:

    @staticmethod
    def create_run(
        session: Session,
        celery_task_id: Optional[str] = None,
        bank_name: Optional[str] = None,
        template_version: Optional[str] = None,
        ledger_source: Optional[str] = None,
        bank_csv_path: Optional[str] = None,
        ledger_csv_path: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[ReconciliationRunModel]:
        safe_user_id = _safe_user_id(user_id)

        if celery_task_id is not None:
            try:
                existing = session.query(ReconciliationRunModel).filter_by(
                    celery_task_id=celery_task_id
                ).first()
            except SQLAlchemyError as e:
                session.rollback()
                print(f"Database error while looking up existing reconciliation run: {e}")
                existing = None

            if existing is not None:
                changed = False
                for field, value in (
                    ("bank_name", bank_name),
                    ("template_version", template_version),
                    ("ledger_source", ledger_source),
                    ("bank_csv_path", bank_csv_path),
                    ("ledger_csv_path", ledger_csv_path),
                ):
                    if value is not None and getattr(existing, field) != value:
                        setattr(existing, field, value)
                        changed = True
                if safe_user_id is not None and existing.user_id != safe_user_id:
                    existing.user_id = safe_user_id
                    changed = True
                if changed:
                    try:
                        session.commit()
                        session.refresh(existing)
                    except SQLAlchemyError as e:
                        session.rollback()
                        print(f"Database error while updating existing reconciliation run: {e}")
                return existing

        try:
            run = ReconciliationRunModel(
                celery_task_id=celery_task_id,
                bank_name=bank_name,
                template_version=template_version,
                ledger_source=ledger_source,
                bank_csv_path=bank_csv_path,
                ledger_csv_path=ledger_csv_path,
                user_id=safe_user_id,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return run
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while creating reconciliation run: {e}")
            if celery_task_id is not None:
                try:
                    return session.query(ReconciliationRunModel).filter_by(
                        celery_task_id=celery_task_id
                    ).first()
                except SQLAlchemyError as lookup_exc:
                    session.rollback()
                    print(f"Database error while recovering existing reconciliation run: {lookup_exc}")
            return None

    @staticmethod
    def update_run_summary(session: Session, run_id: int, summary: Dict[str, Any]) -> bool:
        try:
            run = session.query(ReconciliationRunModel).filter_by(id=run_id).first()
            if run is None:
                print(f"update_run_summary: no run found for run_id={run_id}")
                return False
            for field in (
                "ledger_records", "bank_records", "exact_matches",
                "fuzzy_matches", "ai_matches", "unreconciled_ledger", "unreconciled_bank",
            ):
                if field in summary and summary[field] is not None:
                    setattr(run, field, summary[field])
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
    def push_match_results(session: Session, matches_data: List[Dict[str, Any]], run_id: Optional[int] = None) -> bool:
        try:
            db_rows, unresolved = _expand_matches_to_rows(
                session, run_id, other_matches=matches_data,
            )
            if not db_rows:
                return True

            session.add_all(db_rows)
            session.commit()

            if unresolved:
                print(
                    f"push_match_results: wrote {len(db_rows)} row(s) (run_id={run_id}), "
                    f"but {len(unresolved)} business key(s) could not be resolved to a DB row "
                    f"(row written with that side's FK left NULL): {unresolved[:10]}"
                    + (" ...(truncated)" if len(unresolved) > 10 else "")
                )
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
        try:
            db_rows, unresolved = _expand_matches_to_rows(
                session, run_id,
                timing_matches=timing_matches,
                split_matches=split_matches,
                suggested_journal_entries=suggested_journal_entries,
                other_matches=other_matches,
            )
            if not db_rows:
                return True

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
    def push_journal_entries(
        session: Session,
        entries: List[Dict[str, Any]],
        run_id: Optional[int] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Post approved SUGGESTED_JOURNAL_ENTRIES drafts as real, double-entry
        JournalEntryModel + EntryLineModel rows — the "approve, then post"
        step the report's Suggested Journal Entries sheet asks for.

        Each item in `entries` is expected to have the shape produced by
        run_result_service._build_suggested_journal_entries(): bank_id,
        date, amount, debit_account, credit_account, entry_narrative,
        confidence, source, status. Only entries whose status is
        "approved" or "modified" (case-insensitive) are posted — anything
        still "pending_review" (the default) is skipped, not silently
        dropped: it's reported back so the caller knows what didn't post
        and why.

        Idempotent per run: an entry already posted for a given bank_id
        (traced back through its match_result row) is not posted again,
        so calling this twice with the same reviewed sheet is safe.

        Returns {"posted": int, "skipped": [str, ...], "journal_entry_ids": [...]}
        (or {"posted": 0, "skipped": [...], "error": str} on a DB failure).
        """
        skipped: List[str] = []
        posted: List[JournalEntryModel] = []

        try:
            bank_to_match_id: Dict[str, int] = {}
            if run_id is not None:
                rows = (
                    session.query(MatchResultModel.id, BankStatementModel.row_index)
                    .join(BankStatementModel, MatchResultModel.bank_statement_id == BankStatementModel.id)
                    .filter(MatchResultModel.run_id == run_id)
                    .all()
                )
                bank_to_match_id = {str(row_index): mr_id for mr_id, row_index in rows}

            already_posted_match_ids: set = set()
            if run_id is not None:
                already_posted_match_ids = {
                    match_id for (match_id,) in session.query(JournalEntryModel.source_match_result_id)
                    .filter(
                        JournalEntryModel.run_id == run_id,
                        JournalEntryModel.source_match_result_id.isnot(None),
                    ).all()
                }

            for e in entries:
                bank_id = e.get("bank_id")
                status = str(e.get("status", "")).strip().lower()

                if status not in ("approved", "modified"):
                    skipped.append(f"bank_id={bank_id!r}: status is {status or 'pending_review'!r}, not approved/modified")
                    continue

                debit_account  = e.get("debit_account")
                credit_account = e.get("credit_account")
                amount         = e.get("amount")
                if not debit_account or not credit_account or amount is None:
                    skipped.append(f"bank_id={bank_id!r}: missing debit_account/credit_account/amount")
                    continue

                match_id = bank_to_match_id.get(str(bank_id)) if bank_id is not None else None
                if match_id is not None and match_id in already_posted_match_ids:
                    skipped.append(f"bank_id={bank_id!r}: already posted for this run")
                    continue

                entry_date = _coerce_date(e.get("date")) or datetime.utcnow().date()
                period = _get_or_create_fiscal_period(session, entry_date)
                narrative = e.get("entry_narrative") or ""

                je = JournalEntryModel(
                    period_id=period.id,
                    entry_date=entry_date,
                    voucher_type="Bank Reconciliation Adjustment",
                    narration=narrative,
                    is_reconciliation_entry=True,
                    source_match_result_id=match_id,
                    run_id=run_id,
                    user_id=user_id,
                )
                je.lines = [
                    EntryLineModel(account_name=debit_account, dr_cr=DrCr.DEBIT,
                                   amount=amount, narration=narrative),
                    EntryLineModel(account_name=credit_account, dr_cr=DrCr.CREDIT,
                                   amount=amount, narration=narrative),
                ]
                session.add(je)
                posted.append(je)
                if match_id is not None:
                    already_posted_match_ids.add(match_id)

            if not posted:
                return {"posted": 0, "skipped": skipped, "journal_entry_ids": []}

            session.commit()
            for je in posted:
                session.refresh(je)

            return {
                "posted": len(posted),
                "skipped": skipped,
                "journal_entry_ids": [je.id for je in posted],
            }

        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while posting journal entries: {e}")
            return {"posted": 0, "skipped": skipped, "error": str(e)}

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
        audit_data: List[Dict[str, Any]],
        run_id: Optional[int] = None,
    ) -> bool:
        try:
            db_matches, unresolved = _expand_matches_to_rows(
                session, run_id, other_matches=matches_data,
            )

            db_ignored = [IgnoredMetadataRecordModel(**data) for data in ignored_data]
            
            db_audit = [AuditInvestigationItemModel(**data) for data in audit_data]
            
            session.add_all(db_matches)
            session.add_all(db_ignored)
            session.add_all(db_audit)
            
            session.commit()

            if unresolved:
                print(
                    f"push_reconciliation_results: wrote {len(db_matches)} match row(s) "
                    f"(run_id={run_id}), but {len(unresolved)} business key(s) could not be "
                    f"resolved to a DB row (row written with that side's FK left NULL): "
                    f"{unresolved[:10]}" + (" ...(truncated)" if len(unresolved) > 10 else "")
                )
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting complete reconciliation results: {e}")
            return False
        