from __future__ import annotations

import csv
from typing import List, Optional, Tuple

from schema import BankTemplate
from schema.template import get_all_templates


def _read_header_candidates(
    filepath: str, 
    encoding: str = "utf-8", 
    max_skip: int = 25
) -> List[Tuple[int, List[str]]]:
    
    candidates = []

    try: 
        with open(filepath, 'r', encoding=encoding, errors='replace', newline='') as file:
            reader = csv.reader(file)
            for i, row in enumerate(reader):
                if i > max_skip:
                    break
                cols = [c.strip() for c in row]
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
        header_set = set(cols)
        for tmpl in templates:
            if not tmpl.fingerprint:
                continue
            if tmpl.fingerprint.issubset(header_set):
                exact_matches.append((row_idx, tmpl))
            else:
                overlap = tmpl.fingerprint & header_set
                score = len(overlap) / len(tmpl.fingerprint)

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

