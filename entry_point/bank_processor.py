from __future__ import annotations

import csv
import re
from typing import List, Optional, Tuple

from schema import BankTemplate
from schema.template import get_all_templates


_HEADER_TOKEN_RE = re.compile(r"[^a-z0-9]+")


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
