"""
GSTR-1 (Outward Supplies Return) Data Aggregator

Consumes the same list[dict] produced by the scanner / data_extractor.py
and produces structured data for every table required in GSTR-1:

    Table 4  — B2B Taxable Outward Supplies  (invoice-wise, GSTIN-holder buyers)
    Table 7  — B2C (Large) Taxable Outward Supplies  (inter-state, > ₹2.5 lakh)
    Table 8  — Nil-rated / Exempt / Non-GST Outward Supplies
    Table 12 — HSN-wise Summary of Outward Supplies

Usage
-----
    from gstr1_builder import build_gstr1

    gstr1 = build_gstr1(bills, period_label="Apr-2025")

    # gstr1["b2b"]          — list[dict]  (Table 4)
    # gstr1["b2c_large"]    — list[dict]  (Table 7)
    # gstr1["nil_rated"]    — dict        (Table 8 totals)
    # gstr1["hsn_summary"]  — list[dict]  (Table 12)
    # gstr1["period_label"] — str
    # gstr1["totals"]       — dict        (grand totals)
    # gstr1["warnings"]     — list[str]
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .helper import _safe, _supply_type, _is_nil_exempt, _effective_rate

# B2C Large threshold: inter-state invoice > ₹2.5 lakh goes into Table 7
_B2C_LARGE_THRESHOLD = 250_000.0

# Indian state codes (first 2 digits of GSTIN → state name)
_STATE_CODE_MAP: dict[str, str] = {
    "01": "Jammu & Kashmir", 
    "02": "Himachal Pradesh", 
    "03": "Punjab",
    "04": "Chandigarh", 
    "05": "Uttarakhand", 
    "06": "Haryana",
    "07": "Delhi", 
    "08": "Rajasthan", 
    "09": "Uttar Pradesh",
    "10": "Bihar", 
    "11": "Sikkim", 
    "12": "Arunachal Pradesh",
    "13": "Nagaland", 
    "14": "Manipur", 
    "15": "Mizoram",
    "16": "Tripura", 
    "17": "Meghalaya", 
    "18": "Assam",
    "19": "West Bengal", 
    "20": "Jharkhand", 
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman & Diu", 
    "26": "Dadra & Nagar Haveli", 
    "27": "Maharashtra",
    "28": "Andhra Pradesh (Old)", 
    "29": "Karnataka", 
    "30": "Goa",
    "31": "Lakshadweep", 
    "32": "Kerala", 
    "33": "Tamil Nadu",
    "34": "Puducherry", 
    "35": "Andaman & Nicobar Islands",
    "36": "Telangana", 
    "37": "Andhra Pradesh",
}


def _state_from_gstin(gstin: Optional[str]) -> str:
    if not gstin or len(gstin) < 2:
        return "Unknown"
    return _STATE_CODE_MAP.get(gstin[:2], f"State {gstin[:2]}")
