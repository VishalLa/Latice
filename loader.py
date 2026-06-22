"""
Standardized record schema (one dict per ledger row):
{
    "ledger_id"            : str,
    "account_name"         : str,
    "account_number"       : str | None,
    "transaction_date"     : str | None,   # ISO YYYY-MM-DD
    "transaction_date_raw" : str | None,
    "debit_amount"         : float,        # 0.0 if this is a credit row
    "credit_amount"        : float,        # 0.0 if this is a debit row
    "reference_id"         : str | None,
    "parse_warnings"       : list[str],
}
"""

from __future__ import annotations 

import csv 
import re 
from datetime import datetime 
from typing import List, Optional, Tuple, Dict

from schema import LedgerFormat, LedgerSource,BankStatement, BankTemplate
from matcher.bank_processor import detect_bank


_AMOUNT_CLEAN_RE = re.compile(r"[^\d.\-]")

def parse_amount(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            if value != value:
                return 0.0
        except Exception:
            pass
        return float(value)

    s = str(value).strip()
    if s == "" or s.lower() in ("nan", "none", "-", "na"):
        return 0.0

    negative = s.startswith("(") and s.endswith(")")
    s = _AMOUNT_CLEAN_RE.sub("", s)
    if s in ("", "-", "."):
        return 0.0

    try:
        amt = float(s)
    except ValueError:
        return 0.0

    return -abs(amt) if negative else amt


def parse_date(value, date_format: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (iso_date_string_or_None, warning_or_None).
    """
    if value is None:
        return None, "empty date"
    s = str(value).strip()
    if s == "" or s.lower() == "nan":
        return None, "empty date"
    try:
        dt = datetime.strptime(s, date_format)
        return dt.date().isoformat(), None
    except ValueError as e:
        return None, f"date '{s}' did not match format '{date_format}': {e}"
    

_LEDGER_ALIASES: Dict[str, set] = {
    "transaction_date": {
        "transaction_date", "date", "txn date", "txn_date",
        "voucher date", "entry date",
    },
    "account_name": {
        "account_name", "particulars", "description",
        "narration", "details", "ledger name",
    },
    "debit_amount": {
        "debit_amount", "debit", "dr", "withdrawal",
        "withdrawl", "paid", "debit amount",
    },
    "credit_amount": {
        "credit_amount", "credit", "cr", "deposit",
        "received", "credit amount",
    },
    "reference_id": {
        "reference_id", "voucher_no", "voucher no", "ref no",
        "ref_no", "chq no", "cheque no", "ref", "txn ref",
    },
    "ledger_id": {
        "ledger_id", "id", "sl no", "sr no",
    },
    "account_number": {
        "account_number", "account no", "acc no", "account_no",
    },
}

def _resolve_ledger_columns(
    fieldnames: List[str],
    column_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Optional[str]]:
    """
    Build a mapping  schema_field -> actual_csv_column_name.
    If column_map is provided it takes priority (format: {"csv_col": "schema_field"}).
    Otherwise, alias matching is used.
    Returns None for schema fields that couldn't be mapped.
    """
    resolved: Dict[str, Optional[str]] = {k: None for k in _LEDGER_ALIASES}

    if column_map:
        for csv_col, schema_field in column_map.items():
            if schema_field in resolved and csv_col in fieldnames:
                resolved[schema_field] = csv_col
        return resolved

    lower_to_original = {c.strip().lower(): c.strip() for c in fieldnames}
    for schema_field, aliases in _LEDGER_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lower_to_original:
                resolved[schema_field] = lower_to_original[alias.lower()]
                break

    return resolved


def load_ledger_csv(
    filepath: str,
    date_format: str = "%Y-%m-%d",
    encoding: str = "utf-8",
    column_map: Optional[Dict[str, str]] = None,
) -> dict:
    """
    Parse a user-uploaded ledger CSV into LedgerFormat records.

    All records are tagged with source=LedgerSource.MANUAL.

    Returns
    -------
    {
        "records":  List[LedgerFormat],
        "warnings": List[str]           ← file-level warnings
    }
    Row-level warnings are stored in each record's parse_warnings field.
    """
    file_warnings: List[str] = []
    records: List[LedgerFormat] = []

    try:
        with open(filepath, "r", encoding=encoding, newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return {
                    "records":  [],
                    "warnings": ["No header row found in ledger CSV."],
                }

            reader.fieldnames = [c.strip() for c in reader.fieldnames]
            col = _resolve_ledger_columns(reader.fieldnames, column_map)

            for required in ("transaction_date", "account_name", "debit_amount", "credit_amount"):
                if col[required] is None:
                    file_warnings.append(
                        f"Could not map required ledger field '{required}' to any "
                        f"column. CSV columns: {reader.fieldnames}. "
                        f"Pass column_map={{...}} to reconcile() to fix this."
                    )

            for i, row in enumerate(reader):
                row_warnings: List[str] = []

                # ledger_id: prefer explicit column, fall back to ref or auto-id
                raw_lid = row.get(col["ledger_id"] or "", "").strip()
                if not raw_lid and col["reference_id"]:
                    raw_lid = row.get(col["reference_id"], "").strip()
                ledger_id = raw_lid or None   # LedgerFormat.__post_init__ auto-assigns

                account_name   = (row.get(col["account_name"]   or "", "") or "").strip()
                account_number = (row.get(col["account_number"] or "", "") or "").strip() or None

                date_raw         = row.get(col["transaction_date"] or "", None)
                date_iso, d_warn = parse_date(date_raw, date_format)
                if d_warn:
                    row_warnings.append(d_warn)

                debit  = parse_amount(row.get(col["debit_amount"]  or "", None))
                credit = parse_amount(row.get(col["credit_amount"] or "", None))
                if debit > 0 and credit > 0:
                    row_warnings.append(
                        "Row has both debit and credit amounts — "
                        "expected exactly one; both will be used as-is."
                    )

                reference_id = (row.get(col["reference_id"] or "", "") or "").strip() or None

                records.append(LedgerFormat(
                    ledger_id            = ledger_id,
                    account_name         = account_name,
                    account_number       = account_number,
                    transaction_date     = date_iso,
                    transaction_date_raw = str(date_raw) if date_raw is not None else None,
                    debit_amount         = debit,
                    credit_amount        = credit,
                    reference_id         = reference_id,
                    parse_warnings       = row_warnings,
                    # ── Path B tag ──────────────────────────────────────────
                    source               = LedgerSource.MANUAL,
                    journal_entry_id     = None,
                    voucher_type         = None,
                ))

    except FileNotFoundError:
        return {"records": [], "warnings": [f"Ledger file not found: {filepath}"]}
    except Exception as e:
        return {"records": [], "warnings": [f"Failed to parse ledger CSV: {e}"]}

    return {"records": records, "warnings": file_warnings}


_DEBIT_TYPE_TOKENS = {"d", "dr", "debit", "withdrawal", "wd"}
_CREDIT_TYPE_TOKENS = {"c", "cr", "credit", "deposit", "dp"}


def classify_type_column(type_value) -> Optional[str]:
    if type_value is None:
        return None
    token = str(type_value).strip().lower()
    if token in _DEBIT_TYPE_TOKENS:
        return "debit"
    if token in _CREDIT_TYPE_TOKENS:
        return "credit"
    return None


def _is_blank_row(raw_row: dict) -> bool:
    for k, v in raw_row.items():
        if k is None:
            continue
        if v is not None and str(v).strip() != "":
            return False
    return True


def load_bank_statement(
    filepath: str,
    template: Optional[BankTemplate],
    header_row: Optional[int] = None,
) -> dict:
    """
    Parse a bank-statement CSV into BankStatement records.

    Returns
    -------
    {
        "bank_name":        str | None,
        "template_version": str | None,
        "records":          List[BankStatement],
        "warnings":         List[str],
    }
    """
    warnings: List[str] = []

    if template is None:
        template, header_row, debug_info = detect_bank(filepath=filepath)
        if template is None:
            return {
                "bank_name":        None,
                "template_version": None,
                "records":          [],
                "warnings":         [
                    f"Could not confidently detect bank template. debug={debug_info}"
                ],
            }
        if debug_info.get("match_type") == "fuzzy":
            warnings.append(
                f"Low-confidence bank match: {template.bank_name} "
                f"(confidence={debug_info.get('confidence')}). Please verify."
            )

    skip = header_row if header_row is not None else template.skip_rows

    try:
        with open(filepath, "r", encoding=template.encoding, newline="") as fh:
            for _ in range(skip):
                if fh.readline() == "":
                    break
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise ValueError("No header row found at the expected position.")
            reader.fieldnames = [c.strip() for c in reader.fieldnames]
            field_count = len(reader.fieldnames)
            raw_rows    = list(reader)

    except Exception as e:
        return {
            "bank_name":        template.bank_name,
            "template_version": template.version,
            "records":          [],
            "warnings":         [f"Failed to parse bank CSV: {e}"],
        }

    required_cols = [template.date_column, template.narration_column]
    missing = [c for c in required_cols if c not in reader.fieldnames]
    if missing:
        warnings.append(f"Missing expected column(s) after header detection: {missing}")

    records: List[BankStatement] = []
    out_idx = 0

    for raw_row in raw_rows:
        if _is_blank_row(raw_row):
            continue

        row_warnings: List[str] = []

        extra_fields = raw_row.get(None)
        if extra_fields:
            row_warnings.append(
                f"Row has {len(extra_fields)} more field(s) than the "
                f"{field_count}-column header — likely an unquoted comma. "
                f"Column alignment unreliable; row NOT parsed."
            )
            records.append(BankStatement(
                row_index       = out_idx,
                bank_name       = template.bank_name,
                template_version= template.version,
                date            = None,
                date_raw        = None,
                narration       = "",
                debit           = 0.0,
                credit          = 0.0,
                balance         = None,
                txn_id          = None,
                parse_warnings  = row_warnings,
            ))
            out_idx += 1
            continue

        if any(v is None for v in raw_row.values()):
            row_warnings.append(
                f"Row has fewer fields than the {field_count}-column header — "
                f"missing trailing cell(s) treated as blank."
            )

        date_raw         = raw_row.get(template.date_column)
        date_iso, d_warn = parse_date(date_raw, template.date_format)
        if d_warn:
            row_warnings.append(d_warn)

        narration_raw = raw_row.get(template.narration_column)
        narration     = str(narration_raw).strip() if narration_raw is not None else ""

        debit  = 0.0
        credit = 0.0

        if template.amount_column and template.type_column:
            amount   = parse_amount(raw_row.get(template.amount_column))
            txn_type = classify_type_column(raw_row.get(template.type_column))
            if txn_type == "debit":
                debit  = amount
            elif txn_type == "credit":
                credit = amount
            else:
                row_warnings.append(
                    f"Unrecognised type value "
                    f"'{raw_row.get(template.type_column)}' — "
                    f"could not classify as debit or credit."
                )

        else:
            if template.debit_column:
                debit  = parse_amount(raw_row.get(template.debit_column))
            if template.credit_column:
                credit = parse_amount(raw_row.get(template.credit_column))

        balance = None
        if template.balance_column:
            bal_raw = raw_row.get(template.balance_column)
            if bal_raw is not None and str(bal_raw).strip() != "":
                balance = parse_amount(bal_raw)

        txn_id = None
        if template.txn_id_column:
            tid_raw = raw_row.get(template.txn_id_column)
            if tid_raw is not None and str(tid_raw).strip() != "":
                txn_id = str(tid_raw).strip()

        records.append(BankStatement(
            row_index       = out_idx,
            bank_name       = template.bank_name,
            template_version= template.version,
            date            = date_iso,
            date_raw        = str(date_raw) if date_raw is not None else None,
            narration       = narration,
            debit           = debit,
            credit          = credit,
            balance         = balance,
            txn_id          = txn_id,
            parse_warnings  = row_warnings,
        ))
        out_idx += 1

    return {
        "bank_name":        template.bank_name,
        "template_version": template.version,
        "records":          records,
        "warnings":         warnings,
    }
