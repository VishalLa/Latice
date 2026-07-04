import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from database.bank_renc_model import (
    AuditInvestigationItemModel,
    BankStatementModel,
    IgnoredMetadataRecordModel,
    LedgerFormatModel,
    LedgerSource,
    MatchResultModel,
    ReconciliationRunModel,
)


def _valid_columns(model_cls) -> set:
    """Column names that actually exist on the ORM model's table."""
    return {c.name for c in model_cls.__table__.columns}


def _already_populated(session: Session, model_cls, run_id: Optional[int]) -> bool:
    """
    True if rows already exist for this run_id on this model. Used to make
    the push_* methods idempotent per run — Celery/Redis can redeliver a
    task that already completed successfully (visibility_timeout / ack
    timing), and without this guard a redelivered `process_pre_data` or
    `process_post_data` call fails forever on a UNIQUE constraint instead
    of harmlessly no-op'ing.
    """
    if run_id is None or "run_id" not in _valid_columns(model_cls):
        return False
    return session.query(model_cls).filter(model_cls.run_id == run_id).first() is not None


def _coerce_date(value: Any) -> Optional[date]:
    """Accept date/datetime/ISO-string/None and always return a date or None."""
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_ledger_source(value: Any) -> LedgerSource:
    if value is None:
        return LedgerSource.MANUAL
    if isinstance(value, LedgerSource):
        return value
    if isinstance(value, str):
        try:
            return LedgerSource(value.strip().lower())
        except ValueError as exc:
            valid = [e.value for e in LedgerSource]
            raise ValueError(f"Invalid ledger source {value!r}; must be one of {valid}") from exc
    raise ValueError(f"Invalid type for ledger source: {type(value).__name__}")


_MATCH_FIELD_ALIASES = {
    "match_phase": "match_type",
    "amount": "matched_amount",
    "date": "matched_date",
    "confidence_numeric": "confidence_score",
}


def _normalize_match_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(data)

    for src, dest in _MATCH_FIELD_ALIASES.items():
        if src in payload and dest not in payload:
            payload[dest] = payload.pop(src)

    for key in ("bank_id", "ledger_id"):
        if payload.get(key) is not None and not isinstance(payload[key], str):
            payload[key] = str(payload[key])

    valid_cols = _valid_columns(MatchResultModel)
    extras = {k: v for k, v in payload.items() if k not in valid_cols}
    if extras:
        extras_str = json.dumps(extras, default=str)
        existing_details = payload.get("details")
        payload["details"] = f"{existing_details} | extra: {extras_str}" if existing_details else extras_str
        for k in extras:
            payload.pop(k, None)

    return payload


def _prepare_payload(
    model_cls,
    data: Dict[str, Any],
    *,
    run_id: Optional[int] = None,
    date_fields: Sequence[str] = (),
    numeric_fields: Sequence[str] = (),
    text_defaults: Optional[Dict[str, str]] = None,
    required_fields: Sequence[str] = (),
) -> Dict[str, Any]:
    """
    Normalize a raw dict from the loader/matcher into kwargs that are safe to
    pass to `model_cls(**payload)`:
      - drops any keys that aren't real columns on the model (prevents TypeError)
      - injects run_id when the model has that column
      - converts date-like fields to real `date` objects
      - converts numeric fields to float, replacing None/"" with a safe default
      - fills text fields that are NOT NULL but have no server-side default
      - fails fast with a clear ValueError if a required (NOT NULL, no default)
        column is missing/empty, instead of surfacing an opaque IntegrityError
    """
    valid_cols = _valid_columns(model_cls)

    payload = {k: v for k, v in data.items() if k in valid_cols}

    if run_id is not None and "run_id" in valid_cols:
        payload["run_id"] = run_id

    for field in date_fields:
        if field in payload:
            payload[field] = _coerce_date(payload[field])

    for field in numeric_fields:
        if field in payload:
            payload[field] = _coerce_float(payload[field])

    if text_defaults:
        for field, default in text_defaults.items():
            if field in valid_cols and payload.get(field) is None:
                payload[field] = default

    if "parse_warnings" in valid_cols:
        pw = payload.get("parse_warnings")
        if pw is None:
            payload["parse_warnings"] = []
        elif not isinstance(pw, list):
            payload["parse_warnings"] = [pw]

    missing = [f for f in required_fields if payload.get(f) in (None, "")]
    if missing:
        raise ValueError(
            f"{model_cls.__name__}: missing required field(s) {missing} in payload: {data}"
        )

    return payload


class PushEntryPointData:

    @staticmethod
    def push_bank_statements(
        session: Session, statements_data: List[Dict[str, Any]], run_id: Optional[int] = None
    ) -> bool:
        """
        Takes a list of dictionaries (from the loader) and pushes them to the
        BankStatementModel.
        """
        try:
            if _already_populated(session, BankStatementModel, run_id):
                print(f"push_bank_statements: run_id={run_id} already has bank rows — skipping (idempotent no-op).")
                return True

            db_statements = []
            for data in statements_data:
                payload = _prepare_payload(
                    BankStatementModel,
                    data,
                    run_id=run_id,
                    date_fields=("date",),
                    numeric_fields=("debit", "credit", "balance"),
                    text_defaults={"narration": ""},
                    required_fields=("row_index", "bank_name", "template_version"),
                )
                db_statements.append(BankStatementModel(**payload))

            session.add_all(db_statements)
            session.commit()
            return True

        except (SQLAlchemyError, ValueError) as e:
            session.rollback()
            print(f"Database error while inserting bank statements: {e}")
            return False

    @staticmethod
    def push_ledgers(
        session: Session, ledgers_data: List[Dict[str, Any]], run_id: Optional[int] = None
    ) -> bool:
        """
        Takes a list of dictionaries (from the loader) and pushes them to the
        LedgerFormatModel.
        """
        try:
            if _already_populated(session, LedgerFormatModel, run_id):
                print(f"push_ledgers: run_id={run_id} already has ledger rows — skipping (idempotent no-op).")
                return True

            db_ledgers = []
            for data in ledgers_data:
                payload = _prepare_payload(
                    LedgerFormatModel,
                    data,
                    run_id=run_id,
                    date_fields=("transaction_date",),
                    numeric_fields=("debit_amount", "credit_amount"),
                    required_fields=("ledger_id", "account_name"),
                )
                payload["source"] = _coerce_ledger_source(payload.get("source"))
                db_ledgers.append(LedgerFormatModel(**payload))

            session.add_all(db_ledgers)
            session.commit()
            return True

        except (SQLAlchemyError, ValueError) as e:
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
        """
        Pushes both bank statements and ledgers in a single database
        transaction. If one fails, everything rolls back.
        """
        try:
            if _already_populated(session, BankStatementModel, run_id) or _already_populated(
                session, LedgerFormatModel, run_id
            ):
                print(f"push_all_data: run_id={run_id} already has data — skipping (idempotent no-op).")
                return True

            db_statements = []
            for data in statements_data:
                payload = _prepare_payload(
                    BankStatementModel,
                    data,
                    run_id=run_id,
                    date_fields=("date",),
                    numeric_fields=("debit", "credit", "balance"),
                    text_defaults={"narration": ""},
                    required_fields=("row_index", "bank_name", "template_version"),
                )
                db_statements.append(BankStatementModel(**payload))

            db_ledgers = []
            for data in ledgers_data:
                payload = _prepare_payload(
                    LedgerFormatModel,
                    data,
                    run_id=run_id,
                    date_fields=("transaction_date",),
                    numeric_fields=("debit_amount", "credit_amount"),
                    required_fields=("ledger_id", "account_name"),
                )
                payload["source"] = _coerce_ledger_source(payload.get("source"))
                db_ledgers.append(LedgerFormatModel(**payload))

            session.add_all(db_statements)
            session.add_all(db_ledgers)
            session.commit()
            return True

        except (SQLAlchemyError, ValueError) as e:
            session.rollback()
            print(f"Database error while inserting reconciliation data: {e}")
            return False


    @staticmethod
    def push_match_results(
        session: Session, matches_data: List[Dict[str, Any]], run_id: Optional[int] = None
    ) -> bool:
        """
        Pushes successfully matched records to the database.
        """
        try:
            if _already_populated(session, MatchResultModel, run_id):
                print(f"push_match_results: run_id={run_id} already has match results — skipping (idempotent no-op).")
                return True

            db_matches = []
            for data in matches_data:
                data = _normalize_match_payload(data)
                payload = _prepare_payload(
                    MatchResultModel,
                    data,
                    run_id=run_id,
                    date_fields=("matched_date",),
                    numeric_fields=("matched_amount",),
                    required_fields=("run_id", "match_type"),
                )
                if payload.get("confidence_score") is not None and not isinstance(
                    payload["confidence_score"], str
                ):
                    payload["confidence_score"] = str(payload["confidence_score"])
                db_matches.append(MatchResultModel(**payload))

            session.add_all(db_matches)
            session.commit()
            return True

        except (SQLAlchemyError, ValueError) as e:
            session.rollback()
            print(f"Database error while inserting match results: {e}")
            return False


    @staticmethod
    def push_ignored_records(session: Session, ignored_data: List[Dict[str, Any]]) -> bool:
        """
        Pushes zero-amount or metadata rows that were skipped during
        reconciliation.
        """
        try:
            db_ignored = []
            for data in ignored_data:
                payload = _prepare_payload(
                    IgnoredMetadataRecordModel,
                    data,
                    text_defaults={
                        "narration": "",
                        "reason": "Zero-amount metadata / header row — excluded from reconciliation.",
                    },
                    required_fields=("source", "row_ref"),
                )
                db_ignored.append(IgnoredMetadataRecordModel(**payload))

            session.add_all(db_ignored)
            session.commit()
            return True

        except (SQLAlchemyError, ValueError) as e:
            session.rollback()
            print(f"Database error while inserting ignored records: {e}")
            return False


    @staticmethod
    def push_audit_items(session: Session, audit_data: List[Dict[str, Any]]) -> bool:
        """
        Pushes flagged items (e.g., bank reversals) that require manual GL
        entry.
        """
        try:
            db_audit_items = []
            for data in audit_data:
                payload = _prepare_payload(
                    AuditInvestigationItemModel,
                    data,
                    numeric_fields=("amount",),
                    text_defaults={
                        "narration": "",
                        "action_required": "Bank Reversal detected; requires manual General Ledger journal entry.",
                    },
                    required_fields=("bank_row_index", "amount", "direction", "flag_reason"),
                )
                db_audit_items.append(AuditInvestigationItemModel(**payload))

            session.add_all(db_audit_items)
            session.commit()
            return True

        except (SQLAlchemyError, ValueError) as e:
            session.rollback()
            print(f"Database error while inserting audit items: {e}")
            return False


    @staticmethod
    def update_run_summary(session: Session, run_id: int, **counts: int) -> bool:
        """
        Updates the summary counters on a ReconciliationRunModel row once a
        run's matching phase has completed, e.g.:

            PushEntryPointData.update_run_summary(
                session, run_id,
                ledger_records=len(gl_records),
                bank_records=len(bank_records),
                exact_matches=len(result["EXACT_MATCHES"]),
                fuzzy_matches=len(result["FUZZY_MATCHES"]) + len(result["MEMORY_MATCHES"]),
                ai_matches=len(result["AI_MATCHES"]),
                unreconciled_ledger=len(result.get("UNRECONCILED_LEDGER", [])),
                unreconciled_bank=len(result.get("UNRECONCILED_BANK", [])),
            )

        Unknown kwargs (anything not a real column on ReconciliationRunModel)
        are silently ignored rather than raising, since callers may pass a
        broader summary dict than the model actually stores.
        """
        try:
            run = session.get(ReconciliationRunModel, run_id)
            if run is None:
                print(f"update_run_summary: no ReconciliationRunModel found for run_id={run_id}")
                return False

            valid_cols = _valid_columns(ReconciliationRunModel)
            for key, value in counts.items():
                if key in valid_cols:
                    setattr(run, key, value)

            session.commit()
            return True

        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while updating run summary: {e}")
            return False


    @staticmethod
    def push_reconciliation_results(
        session: Session,
        matches_data: List[Dict[str, Any]],
        ignored_data: List[Dict[str, Any]],
        audit_data: List[Dict[str, Any]],
        run_id: Optional[int] = None,
    ) -> bool:
        """
        Pushes all post-matching outputs (matches, ignored, audit) in a
        single transaction. Highly recommended to prevent partial saves if
        something goes wrong.
        """
        try:
            if _already_populated(session, MatchResultModel, run_id):
                print(
                    f"push_reconciliation_results: run_id={run_id} already has results — skipping (idempotent no-op)."
                )
                return True

            db_matches = []
            for data in matches_data:
                data = _normalize_match_payload(data)
                payload = _prepare_payload(
                    MatchResultModel,
                    data,
                    run_id=run_id,
                    date_fields=("matched_date",),
                    numeric_fields=("matched_amount",),
                    required_fields=("run_id", "match_type"),
                )
                if payload.get("confidence_score") is not None and not isinstance(
                    payload["confidence_score"], str
                ):
                    payload["confidence_score"] = str(payload["confidence_score"])
                db_matches.append(MatchResultModel(**payload))

            db_ignored = []
            for data in ignored_data:
                payload = _prepare_payload(
                    IgnoredMetadataRecordModel,
                    data,
                    text_defaults={
                        "narration": "",
                        "reason": "Zero-amount metadata / header row — excluded from reconciliation.",
                    },
                    required_fields=("source", "row_ref"),
                )
                db_ignored.append(IgnoredMetadataRecordModel(**payload))

            db_audit = []
            for data in audit_data:
                payload = _prepare_payload(
                    AuditInvestigationItemModel,
                    data,
                    numeric_fields=("amount",),
                    text_defaults={
                        "narration": "",
                        "action_required": "Bank Reversal detected; requires manual General Ledger journal entry.",
                    },
                    required_fields=("bank_row_index", "amount", "direction", "flag_reason"),
                )
                db_audit.append(AuditInvestigationItemModel(**payload))

            session.add_all(db_matches)
            session.add_all(db_ignored)
            session.add_all(db_audit)
            session.commit()
            return True

        except (SQLAlchemyError, ValueError) as e:
            session.rollback()
            print(f"Database error while inserting complete reconciliation results: {e}")
            return False
        