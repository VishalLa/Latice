from __future__ import annotations
from typing import Any, Dict, Union

STRING_CONFIDENCE_MAP: Dict[str, float] = {
    "high":   0.90,
    "medium": 0.60,
    "low":    0.30,
}

EXACT_MATCH_CONFIDENCE: float = 1.0
_UNKNOWN_CONFIDENCE: float = 0.0

def normalize_confidence(raw: Union[str, float, int, None]) -> float:
    if raw is None:
        return _UNKNOWN_CONFIDENCE

    if isinstance(raw, (float, int)) and not isinstance(raw, bool):
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            return _UNKNOWN_CONFIDENCE

    if isinstance(raw, str):
        return STRING_CONFIDENCE_MAP.get(raw.strip().lower(), _UNKNOWN_CONFIDENCE)

    return _UNKNOWN_CONFIDENCE

def confidence_bucket(numeric: float) -> str:
    if numeric >= 0.85:
        return "High"
    if numeric >= 0.5:
        return "Medium"
    return "Low"

def annotate_match_confidence(match: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(match)

    if "confidence" in out and isinstance(out["confidence"], (float, int)):
        out["confidence_numeric"] = normalize_confidence(out["confidence"])
    elif "confidence_score" in out:
        out["confidence_numeric"] = normalize_confidence(out["confidence_score"])
    else:
        # No confidence field at all => this is an exact match.
        out["confidence_numeric"] = EXACT_MATCH_CONFIDENCE

    return out
