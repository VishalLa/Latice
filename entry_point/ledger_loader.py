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

Ledger loading only (CSV / Excel).  Bank statement loading lives in
bank_loader.py (format detection in bank_processor.py).
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from schema import LedgerFormat, LedgerSource


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
    # First, try the provided explicit format (template-driven)
    try:
        dt = datetime.strptime(s, date_format)
        return dt.date().isoformat(), None
    except Exception:
        pass

    # Fallback: try pandas to parse common/ambiguous formats (infer dayfirst too)
    try:
        # Try with default parsing (pandas may vary by version)
        pdt = pd.to_datetime(s, errors="coerce")
        if pdt is pd.NaT or pdt is None:
            # Try with dayfirst=True to handle D/M/Y formats
            pdt = pd.to_datetime(s, errors="coerce", dayfirst=True)
        if pdt is not pd.NaT and pdt is not None:
            return pdt.date().isoformat(), None
    except Exception:
        pass

    # Try a set of common explicit formats (day-first and month names)
    COMMON_FALLBACKS = [
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d %b %Y",
        "%b %d, %Y",
        "%Y/%m/%d",
    ]
    for fm in COMMON_FALLBACKS:
        try:
            dt = datetime.strptime(s, fm)
            return dt.date().isoformat(), None
        except Exception:
            continue

    # Last resort: mention the formats we tried in the warning.
    tried = [date_format, "pandas(auto, dayfirst=False)", "pandas(auto, dayfirst=True)"] + COMMON_FALLBACKS
    return None, f"date '{s}' did not match/parse any known formats (tried: {tried})"


def _file_extension(filepath: str) -> str:
    return Path(filepath).suffix.lower()


# ---------------------------------------------------------------------------
# Ledger column aliases
# ---------------------------------------------------------------------------

_LEDGER_ALIASES: Dict[str, Tuple[str, ...]] = {
    "transaction_date": (
        "transaction_date", "date", "txn date", "txn_date",
        "voucher date", "entry date",
    ),
    "account_name": (
        "account_name", "particulars", "description",
        "narration", "details", "ledger name",
    ),
    "debit_amount": (
        "debit_amount", "debit", "dr", "withdrawal",
        "withdrawl", "paid", "debit amount",
    ),
    "credit_amount": (
        "credit_amount", "credit", "cr", "deposit",
        "received", "credit amount",
    ),
    "reference_id": (
        "reference_id", "ref_no", "ref no", "ref", "txn ref",
        "chq no", "cheque no", "voucher_no", "voucher no",
    ),
    "ledger_id": (
        "ledger_id", "voucher_no", "voucher no", "id", "sl no", "sr no",
    ),
    "account_number": (
        "account_number", "account no", "acc no", "account_no",
    ),
    # Excel-only extras (ignored gracefully if absent in CSV)
    "opening_balance": (
        "opening_balance", "opening balance", "op balance",
        "ob", "open bal",
    ),
    "closing_balance": (
        "closing_balance", "closing balance", "cl balance",
        "cb", "close bal",
    ),
    "ledger_type": (
        "ledger_type", "ledger type", "account type",
        "type", "group", "category",
    ),
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
