from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from schema import BankStatement, BankTemplate

from .bank_processor import (
    Detection,
    detect_table,
    normalize_header_name,
    normalized_header_set,
    parse_bank_date,
)

# --------------------------------------------------------------------------
# Amount / type parsing
# --------------------------------------------------------------------------

_SIDE_RE = re.compile(r"(?i)\b(dr|cr|debit|credit)\b\.?\s*$")
_SIDE_PREFIX_RE = re.compile(r"(?i)^\s*(dr|cr)\b\.?\s*")
_BLANKS = {"", "nan", "none", "-", "--", "na", "n/a", "nil", "null"}

_DEBIT_TOKENS = {"d", "dr", "debit", "withdrawal", "wd", "debited", "paid", "w"}
_CREDIT_TOKENS = {"c", "cr", "credit", "deposit", "dp", "dep", "credited", "received"}


def parse_amount_side(value: Any) -> Tuple[float, Optional[str]]:
    """Return (signed_amount, side) where side is 'debit'/'credit'/None from a Dr/Cr marker."""
    if value is None:
        return 0.0, None
    if isinstance(value, (int, float)):
        return (0.0, None) if value != value else (float(value), None)
    s = str(value).strip()
    if s.lower() in _BLANKS:
        return 0.0, None

    side: Optional[str] = None
    m = _SIDE_RE.search(s)
    if m:
        side = "debit" if m.group(1).lower() in ("dr", "debit") else "credit"
        s = s[:m.start()].strip()
    else:
        m = _SIDE_PREFIX_RE.match(s)
        if m:
            side = "debit" if m.group(1).lower() == "dr" else "credit"
            s = s[m.end():].strip()

    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-") or s.endswith("-")
    cleaned = re.sub(r"[^\d.,]", "", s)
    if not cleaned.strip(".,"):
        return 0.0, side
    # European style "1.234,56" / "12,50": comma is the decimal separator
    if re.search(r",\d{1,2}$", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        val = float(cleaned)
    except ValueError:
        return 0.0, side
    return (-val if neg else val), side


def classify_type_column(type_value: Any) -> Optional[str]:
    if type_value is None:
        return None
    token = str(type_value).strip().lower()
    if not token:
        return None
    first = re.split(r"[\s/._-]+", token)[0]
    for t in (token, first):
        if t in _DEBIT_TOKENS:
            return "debit"
        if t in _CREDIT_TOKENS:
            return "credit"
    return None


# --------------------------------------------------------------------------
# Row classification helpers
# --------------------------------------------------------------------------

_FOOTER_RE = re.compile(
    r"(?i)(computer generated|does not require|statement summary|opening balance|"
    r"closing balance|^\s*total\b|grand total|\bpage\s*\d+|registered office|"
    r"end of statement|\*{3,}|generated on|balance\s*(b/f|c/f|brought|carried)|"
    r"statement of account|unless the constituent|legend|abbreviations)"
)


def _cell(cells: List[str], idx: Optional[int]) -> str:
    return cells[idx] if idx is not None and idx < len(cells) else ""


def _fallback_narration_col(det: Detection) -> Optional[int]:
    """If no narration column was recognised, use the unmapped column with the longest text."""
    used = {v for v in det.cols.values() if v is not None}
    width = max((len(r) for r in det.rows[det.header_idx + 1: det.header_idx + 200]), default=0)
    best, best_len = None, 0.0
    sample = det.rows[det.header_idx + 1: det.header_idx + 100]
    for j in range(width):
        if j in used:
            continue
        txt = [len(r[j]) for r in sample if j < len(r) and r[j]]
        if txt and sum(txt) / len(txt) > best_len:
            best, best_len = j, sum(txt) / len(txt)
    return best


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def load_bank_statement(
    filepath: str,
    template: Optional[BankTemplate] = None,
    header_row: Optional[int] = None,
    sheet_name: Optional[str | int] = 0,
    encoding: Optional[str] = None,
) -> dict:
    """
    Parse a bank statement (CSV/TSV/TXT or Excel) into BankStatement records.

    template    : force a BankTemplate; auto-detected when None
    header_row  : 0-based index of the header row in the raw file (overrides detection)
    sheet_name  : Excel sheet (name / 0-based index); other sheets are tried if it has no table
    encoding    : force a text encoding (CSV); otherwise utf-8-sig/utf-8/cp1252/latin-1

    Returns {"bank_name", "template_version", "records", "warnings"}.
    """
    det, warnings = detect_table(filepath, template=template, header_row=header_row,
                                 sheet_name=sheet_name, encoding=encoding)
    if det is None:
        return {"bank_name": None, "template_version": None, "records": [],
                "warnings": warnings}

    if det.match_type == "inferred":
        h = det.rows[det.header_idx]
        mapping = {k: h[v] for k, v in det.cols.items() if v is not None}
        warnings.append(f"Bank template not in registry; columns inferred automatically "
                        f"(verify): {mapping}")
    elif det.match_type == "fuzzy":
        warnings.append(f"Low-confidence bank match: {det.bank_name} "
                        f"(confidence={det.confidence}). Please verify.")
    warnings.extend(det.notes)

    cols = dict(det.cols)
    if cols.get("narration") is None:
        cols["narration"] = _fallback_narration_col(det)
        warnings.append("No narration column recognised; "
                        f"using column index {cols['narration']} as narration.")

    header_cells = det.rows[det.header_idx]
    header_set = normalized_header_set(header_cells)
    fmt = det.date_format or "%Y-%m-%d"

    use_debit_credit = cols.get("debit") is not None or cols.get("credit") is not None
    use_amount = (not use_debit_credit) and cols.get("amount") is not None

    pending: List[Dict[str, Any]] = []
    skipped = 0
    stop_continuation = False

    for r_i in range(det.header_idx + 1, len(det.rows)):
        cells = det.rows[r_i]
        if not any(cells):
            continue

        # repeated header (page break in the export)
        rs = normalized_header_set(cells)
        if len(rs) >= 3 and header_set and len(rs & header_set) / len(header_set) >= 0.7:
            skipped += 1
            continue

        row_warn: List[str] = []
        date_raw = _cell(cells, cols.get("date"))
        date_iso = parse_bank_date(date_raw, fmt)
        narration = _cell(cells, cols.get("narration"))
        side: Optional[str] = None

        debit = credit = 0.0
        if use_debit_credit:
            debit = abs(parse_amount_side(_cell(cells, cols.get("debit")))[0])
            credit = abs(parse_amount_side(_cell(cells, cols.get("credit")))[0])
            if debit > 0 and credit > 0:
                row_warn.append("Row has both debit and credit amounts.")
        elif use_amount:
            amt, side = parse_amount_side(_cell(cells, cols.get("amount")))
            kind = classify_type_column(_cell(cells, cols.get("type"))) if cols.get("type") is not None else None
            if kind is None:
                kind = side
            if kind is None and amt != 0:
                if cols.get("type") is not None and _cell(cells, cols.get("type")):
                    row_warn.append(f"Unrecognised type value '{_cell(cells, cols.get('type'))}' "
                                    "— classified by amount sign.")
                kind = "debit" if amt < 0 else "credit"
            if kind == "debit":
                debit = abs(amt)
            elif kind == "credit":
                credit = abs(amt)

        has_amount = debit > 0 or credit > 0

        if date_iso is None:
            if not has_amount:
                # continuation of previous narration? (only the narration cell is filled)
                only_narr = bool(narration) and all(
                    (not c) for j, c in enumerate(cells) if j != cols.get("narration"))
                if only_narr and pending and not stop_continuation \
                        and not _FOOTER_RE.search(narration):
                    pending[-1]["narration"] += " " + narration
                    continue
                if any(_FOOTER_RE.search(c) for c in cells if c):
                    stop_continuation = True
                skipped += 1
                continue
            if not date_raw and any(_FOOTER_RE.search(c) for c in cells if c):
                skipped += 1          # totals / summary row
                stop_continuation = True
                continue
            row_warn.append("empty date" if not date_raw else
                            f"date '{date_raw}' did not parse with format '{fmt}'")
        stop_continuation = False

        balance: Optional[float] = None
        bal_raw = _cell(cells, cols.get("balance"))
        if bal_raw and bal_raw.lower() not in _BLANKS:
            b, bside = parse_amount_side(bal_raw)
            balance = -abs(b) if bside == "debit" else b

        txn_raw = _cell(cells, cols.get("txn_id"))
        pending.append({
            "date": date_iso, "date_raw": date_raw or None, "narration": narration,
            "debit": debit, "credit": credit, "balance": balance,
            "txn_id": txn_raw or None, "warnings": row_warn,
        })

    records: List[BankStatement] = []
    for i, p in enumerate(pending):
        records.append(BankStatement(
            row_index=i, bank_name=det.bank_name, template_version=det.version,
            date=p["date"], date_raw=p["date_raw"], narration=p["narration"].strip(),
            debit_amount=p["debit"], credit_amount=p["credit"], balance=p["balance"],
            txn_id=p["txn_id"], parse_warnings=p["warnings"],
        ))

    if skipped:
        warnings.append(f"Skipped {skipped} non-transaction row(s) "
                        "(footer, totals, repeated header, blank notes).")
    if not records:
        warnings.append(
            f"Header found at row {det.header_idx} (columns: "
            f"{ {k: header_cells[v] for k, v in cols.items() if v is not None} }) "
            "but no transaction rows could be parsed.")
    else:
        bad = sum(1 for p in pending if p["date"] is None)
        if bad / len(pending) > 0.05:
            warnings.append(f"{bad}/{len(pending)} rows have no valid date — check date format.")

    return {"bank_name": det.bank_name, "template_version": det.version,
            "records": records, "warnings": warnings}
    