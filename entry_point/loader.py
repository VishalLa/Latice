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

    # Excel-only extra fields (populated when source is .xlsx)
    "opening_balance"      : float | None,
    "closing_balance"      : float | None,
    "ledger_type"          : str | None,   # e.g. "Assets", "Liabilities", "Income"
}
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from schema import LedgerFormat, LedgerSource, BankStatement, BankTemplate
from .bank_processor import detect_bank


_AMOUNT_CLEAN_RE = re.compile(r"[^\d.\-]")


def parse_amount(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            if value != value:          # NaN check
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


def parse_date(value: Any, date_format: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns (iso_date_string_or_None, warning_or_None)."""
    if value is None:
        return None, "empty date"

    # pandas Timestamp / Python datetime — already parsed
    if hasattr(value, "date"):
        try:
            return value.date().isoformat(), None
        except Exception:
            pass

    s = str(value).strip()
    if s == "" or s.lower() == "nan":
        return None, "empty date"
    try:
        dt = datetime.strptime(s, date_format)
        return dt.date().isoformat(), None
    except ValueError as e:
        return None, f"date '{s}' did not match format '{date_format}': {e}"


def _file_extension(filepath: str) -> str:
    return Path(filepath).suffix.lower()


# ---------------------------------------------------------------------------
# Ledger column aliases
# ---------------------------------------------------------------------------

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
    # Excel-only extras (ignored gracefully if absent in CSV)
    "opening_balance": {
        "opening_balance", "opening balance", "op balance",
        "ob", "open bal",
    },
    "closing_balance": {
        "closing_balance", "closing balance", "cl balance",
        "cb", "close bal",
    },
    "ledger_type": {
        "ledger_type", "ledger type", "account type",
        "type", "group", "category",
    },
}


def _resolve_ledger_columns(
    fieldnames: List[str],
    column_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Optional[str]]:
    """
    Build a mapping  schema_field -> actual_column_name.
    column_map (if given) takes priority: {"csv_col": "schema_field"}.
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


# ---------------------------------------------------------------------------
# Internal: build LedgerFormat from a resolved row dict
# ---------------------------------------------------------------------------

def _build_ledger_record(
    row: Dict[str, Any],
    col: Dict[str, Optional[str]],
    date_format: str,
) -> LedgerFormat:
    """Convert a flat row dict (CSV or Excel) into a LedgerFormat object."""
    row_warnings: List[str] = []

    raw_lid = str(row.get(col["ledger_id"] or "", "") or "").strip()
    if not raw_lid and col["reference_id"]:
        raw_lid = str(row.get(col["reference_id"], "") or "").strip()
    ledger_id = raw_lid or None

    account_name   = str(row.get(col["account_name"]   or "", "") or "").strip()
    account_number = str(row.get(col["account_number"] or "", "") or "").strip() or None

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

    reference_id = str(row.get(col["reference_id"] or "", "") or "").strip() or None

    # Excel-only extras stored as parse_warnings metadata if schema doesn't have fields
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    ledger_type: Optional[str] = None

    if col.get("opening_balance") and col["opening_balance"] in row:
        opening_balance = parse_amount(row[col["opening_balance"]])
    if col.get("closing_balance") and col["closing_balance"] in row:
        closing_balance = parse_amount(row[col["closing_balance"]])
    if col.get("ledger_type") and col["ledger_type"] in row:
        raw_lt = row.get(col["ledger_type"])
        ledger_type = str(raw_lt).strip() if raw_lt is not None else None

    record = LedgerFormat(
        ledger_id            = ledger_id,
        account_name         = account_name,
        account_number       = account_number,
        transaction_date     = date_iso,
        transaction_date_raw = str(date_raw) if date_raw is not None else None,
        debit_amount         = debit,
        credit_amount        = credit,
        reference_id         = reference_id,
        parse_warnings       = row_warnings,
        source               = LedgerSource.MANUAL,
        journal_entry_id     = None,
        voucher_type         = None,
    )

    # Attach Excel extras as side-channel attributes (no schema change required)
    record.__dict__["opening_balance"] = opening_balance
    record.__dict__["closing_balance"] = closing_balance
    record.__dict__["ledger_type"]     = ledger_type

    return record


# ---------------------------------------------------------------------------
# Excel → in-memory CSV rows helper
# ---------------------------------------------------------------------------

def _read_excel_as_rows(
    filepath: str,
    sheet_name: Optional[str | int] = 0,
    header_row: int = 0,
) -> Tuple[List[str], List[Dict[str, Any]], List[str]]:
    """
    Read an Excel file and return (fieldnames, rows, warnings).

    Tries every sheet if the first sheet yields no usable columns.
    Handles merged cells, multi-row headers (takes the first non-empty
    header row within the first 25 rows), and skips purely empty rows.
    """
    warnings: List[str] = []

    try:
        xl = pd.ExcelFile(filepath, engine="openpyxl")
    except Exception as e:
        return [], [], [f"Could not open Excel file: {e}"]

    sheet_names = xl.sheet_names
    sheets_to_try: List[str | int] = (
        [sheet_name] if sheet_name is not None else sheet_names
    )

    for sname in sheets_to_try:
        try:
            # Read raw without header to scan for the actual header row
            raw: pd.DataFrame = xl.parse(
                sname, header=None, dtype=str, keep_default_na=False
            )
        except Exception as e:
            warnings.append(f"Sheet '{sname}': could not parse — {e}")
            continue

        if raw.empty:
            warnings.append(f"Sheet '{sname}' is empty, skipping.")
            continue

        # Detect header row: first row with >= 3 non-blank cells in first 25 rows
        detected_header = header_row
        for idx in range(min(25, len(raw))):
            non_blank = sum(1 for v in raw.iloc[idx] if str(v).strip() != "")
            if non_blank >= 3:
                detected_header = idx
                break

        df: pd.DataFrame = xl.parse(
            sname, header=detected_header, dtype=str, keep_default_na=False
        )

        # Drop fully-empty rows and columns
        df.dropna(how="all", inplace=True)
        df.dropna(axis=1, how="all", inplace=True)
        df.columns = [str(c).strip() for c in df.columns]

        if df.empty or len(df.columns) < 2:
            warnings.append(f"Sheet '{sname}' has no usable data after cleanup, skipping.")
            continue

        fieldnames = list(df.columns)
        rows = df.where(pd.notna(df), None).to_dict(orient="records")

        if sname != (sheet_name or sheet_names[0]):
            warnings.append(f"Using sheet '{sname}' (first usable sheet found).")

        return fieldnames, rows, warnings

    return [], [], warnings + ["No usable sheet found in the Excel file."]


# ---------------------------------------------------------------------------
# load_ledger  (CSV + XLSX)
# ---------------------------------------------------------------------------

def load_ledger(
    filepath: str,
    date_format: str = "%Y-%m-%d",
    encoding: str = "utf-8",
    column_map: Optional[Dict[str, str]] = None,
    sheet_name: Optional[str | int] = 0,
) -> dict:
    """
    Parse a user-uploaded ledger file (CSV **or** Excel .xlsx/.xls) into
    LedgerFormat records.

    Extra fields extracted from Excel files
    ----------------------------------------
    opening_balance  – opening balance at the start of the ledger period
    closing_balance  – closing balance at the end of the ledger period
    ledger_type      – account type / group (Assets, Liabilities, Income …)

    These are attached directly to the returned LedgerFormat objects as
    ``record.opening_balance``, ``record.closing_balance``,
    ``record.ledger_type`` for downstream use.

    Parameters
    ----------
    filepath    : path to .csv, .xlsx, or .xls file
    date_format : strptime format string for the date column
    encoding    : text encoding (CSV only; Excel auto-detects)
    column_map  : override alias detection {"csv_col": "schema_field"}
    sheet_name  : Excel sheet to read (name or 0-based index); None = auto

    Returns
    -------
    {
        "records":  List[LedgerFormat],
        "warnings": List[str]
    }
    """
    ext = _file_extension(filepath)
    file_warnings: List[str] = []
    records: List[LedgerFormat] = []

    # ── Branch: Excel ──────────────────────────────────────────────────────
    if ext in (".xlsx", ".xls", ".xlsm"):
        fieldnames, rows, read_warnings = _read_excel_as_rows(
            filepath, sheet_name=sheet_name
        )
        file_warnings.extend(read_warnings)

        if not fieldnames:
            return {"records": [], "warnings": file_warnings}

        col = _resolve_ledger_columns(fieldnames, column_map)

        for required in ("transaction_date", "account_name", "debit_amount", "credit_amount"):
            if col[required] is None:
                file_warnings.append(
                    f"Could not map required ledger field '{required}' to any "
                    f"Excel column. Columns found: {fieldnames}. "
                    f"Pass column_map={{...}} to reconcile() to fix this."
                )

        for row in rows:
            # Skip summary/blank rows (all watched columns are empty)
            watched = [col["account_name"], col["transaction_date"],
                       col["debit_amount"], col["credit_amount"]]
            if all(
                row.get(c) is None or str(row.get(c, "")).strip() == ""
                for c in watched if c
            ):
                continue

            records.append(_build_ledger_record(row, col, date_format))

        return {"records": records, "warnings": file_warnings}

    # ── Branch: CSV ────────────────────────────────────────────────────────
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

            for row in reader:
                records.append(_build_ledger_record(row, col, date_format))

    except FileNotFoundError:
        return {"records": [], "warnings": [f"Ledger file not found: {filepath}"]}
    except Exception as e:
        return {"records": [], "warnings": [f"Failed to parse ledger file: {e}"]}

    return {"records": records, "warnings": file_warnings}


# ---------------------------------------------------------------------------
# Debit / Credit type-column classification
# ---------------------------------------------------------------------------

_DEBIT_TYPE_TOKENS  = {"d", "dr", "debit", "withdrawal", "wd"}
_CREDIT_TYPE_TOKENS = {"c", "cr", "credit", "deposit", "dp"}


def classify_type_column(type_value: Any) -> Optional[str]:
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


# ---------------------------------------------------------------------------
# Internal: build BankStatement from a resolved row dict
# ---------------------------------------------------------------------------

def _build_bank_record(
    row: Dict[str, Any],
    out_idx: int,
    template: BankTemplate,
    field_count: int,
) -> BankStatement:
    row_warnings: List[str] = []

    # Extra-field guard (only relevant for CSV; Excel won't have None keys)
    extra_fields = row.get(None)
    if extra_fields:
        row_warnings.append(
            f"Row has {len(extra_fields)} more field(s) than the "
            f"{field_count}-column header — likely an unquoted comma. "
            f"Column alignment unreliable; row NOT parsed."
        )
        return BankStatement(
            row_index        = out_idx,
            bank_name        = template.bank_name,
            template_version = template.version,
            date             = None,
            date_raw         = None,
            narration        = "",
            debit            = 0.0,
            credit           = 0.0,
            balance          = None,
            txn_id           = None,
            parse_warnings   = row_warnings,
        )

    if any(v is None for k, v in row.items() if k is not None):
        row_warnings.append(
            f"Row has fewer fields than the {field_count}-column header — "
            f"missing trailing cell(s) treated as blank."
        )

    date_raw         = row.get(template.date_column)
    date_iso, d_warn = parse_date(date_raw, template.date_format)
    if d_warn:
        row_warnings.append(d_warn)

    narration_raw = row.get(template.narration_column)
    narration     = str(narration_raw).strip() if narration_raw is not None else ""

    debit  = 0.0
    credit = 0.0

    if template.amount_column and template.type_column:
        amount   = parse_amount(row.get(template.amount_column))
        txn_type = classify_type_column(row.get(template.type_column))
        if txn_type == "debit":
            debit = amount
        elif txn_type == "credit":
            credit = amount
        else:
            row_warnings.append(
                f"Unrecognised type value "
                f"'{row.get(template.type_column)}' — "
                f"could not classify as debit or credit."
            )
    else:
        if template.debit_column:
            debit  = parse_amount(row.get(template.debit_column))
        if template.credit_column:
            credit = parse_amount(row.get(template.credit_column))

    balance = None
    if template.balance_column:
        bal_raw = row.get(template.balance_column)
        if bal_raw is not None and str(bal_raw).strip() != "":
            balance = parse_amount(bal_raw)

    txn_id = None
    if template.txn_id_column:
        tid_raw = row.get(template.txn_id_column)
        if tid_raw is not None and str(tid_raw).strip() != "":
            txn_id = str(tid_raw).strip()

    return BankStatement(
        row_index        = out_idx,
        bank_name        = template.bank_name,
        template_version = template.version,
        date             = date_iso,
        date_raw         = str(date_raw) if date_raw is not None else None,
        narration        = narration,
        debit            = debit,
        credit           = credit,
        balance          = balance,
        txn_id           = txn_id,
        parse_warnings   = row_warnings,
    )


# ---------------------------------------------------------------------------
# Excel bank-statement reader
# ---------------------------------------------------------------------------

def _load_bank_excel(
    filepath: str,
    template: BankTemplate,
    sheet_name: Optional[str | int] = 0,
) -> dict:
    """
    Parse an Excel bank statement using the supplied BankTemplate.

    The template's fingerprint columns are used to locate the header row
    (first row in the first 30 that contains a superset of those columns).
    Falls back to the first row with >= 3 non-blank cells.
    """
    warnings: List[str] = []

    try:
        xl = pd.ExcelFile(filepath, engine="openpyxl")
    except Exception as e:
        return {
            "bank_name":        template.bank_name,
            "template_version": template.version,
            "records":          [],
            "warnings":         [f"Could not open Excel file: {e}"],
        }

    sname = sheet_name if sheet_name is not None else xl.sheet_names[0]

    try:
        raw: pd.DataFrame = xl.parse(sname, header=None, dtype=str, keep_default_na=False)
    except Exception as e:
        return {
            "bank_name":        template.bank_name,
            "template_version": template.version,
            "records":          [],
            "warnings":         [f"Could not parse sheet '{sname}': {e}"],
        }

    # Locate header row
    detected_header = 0
    for idx in range(min(30, len(raw))):
        cols_here = {str(v).strip() for v in raw.iloc[idx] if str(v).strip() != ""}
        if template.fingerprint and template.fingerprint.issubset(cols_here):
            detected_header = idx
            break
        if len(cols_here) >= 3 and detected_header == 0:
            detected_header = idx   # tentative fallback

    df: pd.DataFrame = xl.parse(sname, header=detected_header, dtype=str, keep_default_na=False)
    df.dropna(how="all", inplace=True)
    df.dropna(axis=1, how="all", inplace=True)
    df.columns = [str(c).strip() for c in df.columns]

    fieldnames  = list(df.columns)
    field_count = len(fieldnames)

    required_cols = [template.date_column, template.narration_column]
    missing = [c for c in required_cols if c not in fieldnames]
    if missing:
        warnings.append(f"Missing expected column(s) in Excel sheet: {missing}")

    records: List[BankStatement] = []
    for out_idx, (_, series) in enumerate(df.iterrows()):
        row = series.where(pd.notna(series), None).to_dict()

        # Skip blank rows
        if all(v is None or str(v).strip() == "" for v in row.values()):
            continue

        records.append(_build_bank_record(row, out_idx, template, field_count))

    return {
        "bank_name":        template.bank_name,
        "template_version": template.version,
        "records":          records,
        "warnings":         warnings,
    }


# ---------------------------------------------------------------------------
# detect_bank extension for Excel files
# ---------------------------------------------------------------------------

def _detect_bank_excel(
    filepath: str,
    sheet_name: Optional[str | int] = 0,
) -> Tuple[Optional[BankTemplate], Optional[int], dict]:
    """
    Attempt to detect the bank template from an Excel file by scanning
    column headers across sheets against every registered fingerprint.
    """
    from schema.template import get_all_templates

    templates = get_all_templates()
    debug_info: dict = {}

    try:
        xl = pd.ExcelFile(filepath, engine="openpyxl")
    except Exception as e:
        return None, None, {"error": str(e), "match_type": "none"}

    sheets_to_scan = xl.sheet_names if sheet_name is None else [sheet_name or xl.sheet_names[0]]
    exact_matches = []
    best_fuzzy    = None

    for sname in sheets_to_scan:
        try:
            raw = xl.parse(sname, header=None, dtype=str, keep_default_na=False)
        except Exception:
            continue

        for idx in range(min(30, len(raw))):
            cols_here = {str(v).strip() for v in raw.iloc[idx] if str(v).strip() != ""}
            if len(cols_here) < 2:
                continue
            for tmpl in templates:
                if not tmpl.fingerprint:
                    continue
                if tmpl.fingerprint.issubset(cols_here):
                    exact_matches.append((idx, tmpl))
                else:
                    overlap = tmpl.fingerprint & cols_here
                    score = len(overlap) / len(tmpl.fingerprint)
                    if best_fuzzy is None or score > best_fuzzy[0]:
                        best_fuzzy = (score, idx, tmpl)

    if exact_matches:
        exact_matches.sort(key=lambda p: (-len(p[1].fingerprint), p[0]))
        row_idx, tmpl = exact_matches[0]
        debug_info.update({
            "match_type": "exact",
            "candidates": [t.bank_name for _, t in exact_matches],
        })
        return tmpl, row_idx, debug_info

    if best_fuzzy and best_fuzzy[0] >= 0.6:
        score, row_idx, tmpl = best_fuzzy
        debug_info.update({"match_type": "fuzzy", "confidence": round(score, 2)})
        return tmpl, row_idx, debug_info

    debug_info["match_type"] = "none"
    return None, None, debug_info


# ---------------------------------------------------------------------------
# load_bank_statement  (CSV + XLSX)
# ---------------------------------------------------------------------------

def load_bank_statement(
    filepath: str,
    template: Optional[BankTemplate] = None,
    header_row: Optional[int] = None,
    sheet_name: Optional[str | int] = 0,
) -> dict:
    """
    Parse a bank-statement file (CSV **or** Excel .xlsx/.xls) into
    BankStatement records.

    Parameters
    ----------
    filepath   : path to .csv, .xlsx, or .xls file
    template   : BankTemplate to use; auto-detected when None
    header_row : override the detected header row index (CSV only)
    sheet_name : Excel sheet to read (name or 0-based index); None = auto-scan

    Returns
    -------
    {
        "bank_name":        str | None,
        "template_version": str | None,
        "records":          List[BankStatement],
        "warnings":         List[str],
    }
    """
    ext = _file_extension(filepath)

    # ── Branch: Excel ──────────────────────────────────────────────────────
    if ext in (".xlsx", ".xls", ".xlsm"):
        warnings: List[str] = []

        if template is None:
            template, _hrow, debug_info = _detect_bank_excel(filepath, sheet_name)
            if template is None:
                return {
                    "bank_name":        None,
                    "template_version": None,
                    "records":          [],
                    "warnings":         [
                        f"Could not detect bank template from Excel file. "
                        f"debug={debug_info}"
                    ],
                }
            if debug_info.get("match_type") == "fuzzy":
                warnings.append(
                    f"Low-confidence bank match: {template.bank_name} "
                    f"(confidence={debug_info.get('confidence')}). Please verify."
                )

        result = _load_bank_excel(filepath, template, sheet_name=sheet_name)
        result["warnings"] = warnings + result.get("warnings", [])
        return result

    # ── Branch: CSV ────────────────────────────────────────────────────────
    warnings_csv: List[str] = []

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
            warnings_csv.append(
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
        warnings_csv.append(f"Missing expected column(s) after header detection: {missing}")

    records_csv: List[BankStatement] = []
    out_idx = 0

    for raw_row in raw_rows:
        if _is_blank_row(raw_row):
            continue
        records_csv.append(_build_bank_record(raw_row, out_idx, template, field_count))
        out_idx += 1

    return {
        "bank_name":        template.bank_name,
        "template_version": template.version,
        "records":          records_csv,
        "warnings":         warnings_csv,
    }
