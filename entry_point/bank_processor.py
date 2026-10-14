from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
from schema import BankTemplate
from schema.template import get_all_templates


_HEADER_TOKEN_RE = re.compile(r"[^a-z0-9]+")

_COLUMN_ALIASES = {
    "date": (
        "date", "transaction date", "tran date", "txn date", "value date"
        ),
    "narration": (
        "narration", "description", "particulars", "transaction remarks", "details", "memo",
    ),
    "debit": (
        "debit", "withdrawal", "withdrawal amount", "debited amount", "dr"
        ),
    "credit": (
        "credit", "deposit", "deposit amount", "credited amount", "cr"
        ),
    "amount": (
        "amount", "transaction amount"
        ),
    "type": (
        "type", "transaction type", "dr cr", "debit credit"
        ),
    "balance": (
        "balance", "closing balance", "available balance"
        ),
    "txn_id": (
        "txn id", "transaction id", "reference no", "ref no", "cheque no", "chq no",
    ),
}


@dataclass
class Detection:
    bank_name: str
    version: str
    date_format: Optional[str]
    rows: List[List[str]]
    header_idx: int
    cols: Dict[str, Optional[int]]
    match_type: str
    confidence: Optional[float] = None
    notes: List[str] = field(default_factory=list)


def normalize_header_name(value: object) -> str:
    text = str(value or "").strip().lstrip("\ufeff").lower()
    text = _HEADER_TOKEN_RE.sub(" ", text)
    return " ".join(text.split())


def normalized_header_set(columns: List[str]) -> set[str]:
    return {normalize_header_name(c) for c in columns if normalize_header_name(c)}


def _read_header_candidates(
    filepath: str,
    encoding: str = "utf-8",
    max_skip: int = 25
) -> List[Tuple[int, List[str]]]:
    
    candidates = []

    try:
        with open(filepath, 'r', encoding=encoding, errors='replace', newline='') as file:
            sample = file.read(4096)
            file.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel

            reader = csv.reader(file, dialect)
            for i, row in enumerate(reader):
                if i > max_skip:
                    break
                cols = [c.strip().lstrip("\ufeff") for c in row]
                if len(cols) >= 3:
                    candidates.append((i, cols))

    except (UnicodeDecodeError, FileNotFoundError):
        pass 
    return candidates


def detect_bank(
    filepath: str, 
    encoding: str = "utf-8"
) -> Tuple[Optional[BankTemplate], Optional[int], dict]:
    """
    Detect which bank template a CSV file matches.

    Returns (template, header_row_index, debug_info).
    template is None if no confident match was found.

    Matching strategy:
      - exact match: a candidate header row's column set is a superset of
        a template's fingerprint -> confident match.
      - if multiple templates match the same header row equally, prefer the
        one whose fingerprint is the largest (most specific) subset.
      - if nothing is an exact superset, fall back to the template/row with
        the highest Jaccard overlap, but only report it if overlap >= 0.6,
        and flag it as a low-confidence guess.
    """

    header_candidates = _read_header_candidates(filepath=filepath, encoding=encoding)
    templates = get_all_templates()

    exact_matches = []
    best_fuzzy = None 

    for row_idx, cols in header_candidates:
        header_set = normalized_header_set(cols)
        for tmpl in templates:
            if not tmpl.fingerprint:
                continue
            fingerprint = {normalize_header_name(c) for c in tmpl.fingerprint}
            if fingerprint.issubset(header_set):
                exact_matches.append((row_idx, tmpl))
            else:
                overlap = fingerprint & header_set
                score = len(overlap) / len(fingerprint)

                if best_fuzzy is None or score > best_fuzzy[0]:
                    best_fuzzy = (score, row_idx, tmpl)

    debug_info = {
        "header_rows_scanned": len(header_candidates),
        "exact_match_count": len(exact_matches),
    }

    if exact_matches:
        exact_matches.sort(key=lambda pair: (-len(pair[1].fingerprint), pair[0]))
        row_idx, tmpl = exact_matches[0]

        debug_info["match_type"] = "exact"
        debug_info["candidates"] = [t.bank_name for _, t in exact_matches]

        return tmpl, row_idx, debug_info
    
    if best_fuzzy and best_fuzzy[0] >= 0.6:
        score, row_idx, tmpl = best_fuzzy

        debug_info["match_type"] = "fuzzy"
        debug_info["confidence"] = round(score, 2)

        return tmpl, row_idx, debug_info

    debug_info["match_type"] = "none"
    return None, None, debug_info


def parse_bank_date(
    value: object, 
    date_format: Optional[str]
) -> Optional[str]:
    if value is None:
        return None
    
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None

    formats = ([date_format] if date_format else []) + [
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d %m %Y",
        "%d-%b-%Y",
        "%d %b %Y",
        "%b %d, %Y",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _read_csv_table(
    filepath: str, 
    encoding: Optional[str]
) -> Tuple[List[List[str]], List[str]]:
    encodings = [encoding] if encoding else ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
    errors: List[str] = []
    for candidate in encodings:
        try:
            with open(filepath, "r", encoding=candidate, newline="") as source:
                sample = source.read(4096)
                source.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
                    
                rows = [
                    [cell.strip().lstrip("\ufeff") for cell in row]
                    for row in csv.reader(source, dialect)
                ]
            return rows, errors
        except UnicodeDecodeError as exc:
            errors.append(f"Could not decode as {candidate}: {exc}")
        except OSError as exc:
            return [], [f"Could not read file: {exc}"]
        
    return [], errors


def _read_excel_table(
    filepath: str,
    sheet_name: Optional[str | int],
) -> Tuple[List[List[str]], List[str]]:
    try:
        workbook = pd.ExcelFile(filepath, engine="openpyxl")
    except (OSError, ValueError, ImportError) as exc:
        return [], [f"Could not open Excel file: {exc}"]

    sheet_names = workbook.sheet_names
    sheets = sheet_names if sheet_name is None else [sheet_name]
    errors: List[str] = []
    
    for sheet in sheets:
        try:
            frame = workbook.parse(sheet, header=None, dtype=str, keep_default_na=False)
        except (ValueError, KeyError) as exc:
            errors.append(f"Could not read sheet '{sheet}': {exc}")
            continue
        
        rows = [
            [str(value).strip().lstrip("\ufeff") for value in row]
            for row in frame.fillna("").values.tolist()
        ]
        if rows:
            return rows, errors
    return [], errors or ["Excel file contains no rows."]


def _template_column_indices(
    template: BankTemplate,
    headers: List[str],
) -> Dict[str, Optional[int]]:
    header_indices = {
        normalize_header_name(header): index 
        for index, header in enumerate(headers)
    }
    fields = {
        "date": template.date_column,
        "narration": template.narration_column,
        "debit": template.debit_column,
        "credit": template.credit_column,
        "amount": template.amount_column,
        "type": template.type_column,
        "balance": template.balance_column,
        "txn_id": template.txn_id_column,
    }
    return {
        key: header_indices.get(normalize_header_name(column)) if column else None
        for key, column in fields.items()
    }


def _infer_columns(
    headers: List[str]
) -> Dict[str, Optional[int]]:
    normalized = [normalize_header_name(header) for header in headers]
    result: Dict[str, Optional[int]] = {key: None for key in _COLUMN_ALIASES}
    
    for key, aliases in _COLUMN_ALIASES.items():
        normalized_aliases = {normalize_header_name(alias) for alias in aliases}
        result[key] = next(
            (index for index, header in enumerate(normalized) if header in normalized_aliases),
            None,
        )
    return result


def _match_template(
    headers: List[str]
) -> Tuple[Optional[BankTemplate], Optional[float]]:
    header_set = normalized_header_set(headers)
    exact = []
    fuzzy: Optional[Tuple[float, BankTemplate]] = None
    
    for template in get_all_templates():
        fingerprint = {normalize_header_name(column) for column in template.fingerprint}
        if not fingerprint:
            continue
        if fingerprint.issubset(header_set):
            exact.append(template)
            continue
        
        score = len(fingerprint & header_set) / len(fingerprint)
        if fuzzy is None or score > fuzzy[0]:
            fuzzy = (score, template)
            
    if exact:
        exact.sort(key=lambda item: -len(item.fingerprint))
        return exact[0], 1.0
    if fuzzy and fuzzy[0] >= 0.6:
        return fuzzy[1], fuzzy[0]
    return None, None


def detect_table(
    filepath: str,
    template: Optional[BankTemplate] = None,
    header_row: Optional[int] = None,
    sheet_name: Optional[str | int] = 0,
    encoding: Optional[str] = None,
) -> Tuple[Optional[Detection], List[str]]:
    extension = filepath.lower().rsplit(".", 1)[-1] if "." in filepath else ""
    if extension in {"xlsx", "xls", "xlsm"}:
        rows, warnings = _read_excel_table(filepath, sheet_name)
    else:
        rows, warnings = _read_csv_table(filepath, encoding)
        
    if not rows:
        return None, warnings or ["File contains no rows."]

    selected_idx = header_row
    selected_template = template
    selected_confidence: Optional[float] = None

    if selected_idx is None:
        best_inferred: Optional[Tuple[int, int, Dict[str, Optional[int]]]] = None
        for index, row in enumerate(rows[:30]):
            matched_template, confidence = _match_template(row)
            
            if matched_template is not None:
                selected_idx = index
                selected_template = selected_template or matched_template
                selected_confidence = confidence
                break
            
            columns = _infer_columns(row)
            recognized = sum(value is not None for value in columns.values())
            if columns["date"] is not None and columns["narration"] is not None:
                if best_inferred is None or recognized > best_inferred[1]:
                    best_inferred = (index, recognized, columns)
                    
        if selected_idx is None and best_inferred is not None:
            selected_idx = best_inferred[0]
            
    if selected_idx is None:
        return None, warnings + ["Could not identify a transaction header row."]
    if selected_idx < 0 or selected_idx >= len(rows):
        return None, warnings + [f"Header row index {selected_idx} is outside the file."]

    headers = rows[selected_idx]
    detected_template, confidence = _match_template(headers)
    selected_template = selected_template or detected_template
    selected_confidence = selected_confidence or confidence
    columns = _template_column_indices(selected_template, headers) if selected_template else _infer_columns(headers)
    match_type = "exact" if selected_confidence == 1.0 else (
        "fuzzy" if selected_confidence is not None else "inferred"
    )
    
    notes: List[str] = []
    if selected_template is None:
        notes.append("No registered bank template matched; column names were inferred.")
        bank_name, version, date_format = "Unknown Bank", "inferred", None
    else:
        bank_name = selected_template.bank_name
        version = selected_template.version
        date_format = selected_template.date_format
        if match_type == "fuzzy":
            notes.append(f"Template match confidence: {selected_confidence:.2f}.")

    return Detection(
        bank_name=bank_name,
        version=version,
        date_format=date_format,
        rows=rows,
        header_idx=selected_idx,
        cols=columns,
        match_type=match_type,
        confidence=selected_confidence,
        notes=notes,
    ), warnings
