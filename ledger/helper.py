from __future__ import annotations

from typing import Optional
from datetime import date
from dateutil import parser as dateparser

# date normalisation
def parse_indian_date(raw: Optional[str]) -> date:
    if not raw:
        return date.today()
    try:
        return dateparser.parse(raw, dayfirst=True).date()
    except Exception:
        return date.today()


# journal entry generators
def _safe(val, default=0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _effective_rate(cgst_rate, sgst_rate, igst_rate) -> float:
    """Return the total GST rate as a percentage (e.g. 18.0)."""
    c = _safe(cgst_rate)
    s = _safe(sgst_rate)
    ig = _safe(igst_rate)
    if c and s:
        return round(c + s, 2)
    if ig:
        return ig
    return 0.0


def _is_output(bill: dict) -> bool:
    return str(bill.get("direction", "")).lower() == "output"


def _is_nil_exempt(bill: dict) -> bool:
    """Bill has zero GST — nil-rated, exempt, or non-GST supply."""
    return (
        not _safe(bill.get("cgst_amount"))
        and not _safe(bill.get("sgst_amount"))
        and not _safe(bill.get("igst_amount"))
    )


def _supply_type(bill: dict) -> str:
    """
    'inter'  → IGST transaction (buyer in different state)
    'intra'  → CGST + SGST transaction (buyer in same state)
    """
    if _safe(bill.get("igst_amount")):
        return "inter"
    return "intra"
