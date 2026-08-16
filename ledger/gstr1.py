from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .helper import _safe, _supply_type, _is_nil_exempt, _effective_rate, _is_output


class GSTR1:
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
        from gstr1 import build_gstr1          # thin functional wrapper
        # or
        from gstr1 import GSTR1

        gstr1 = GSTR1(bills).build(period_label="Apr-2025")

        # gstr1["b2b"]          — list[dict]  (Table 4)
        # gstr1["b2c_large"]    — list[dict]  (Table 7)
        # gstr1["nil_rated"]    — dict        (Table 8 totals)
        # gstr1["hsn_summary"]  — list[dict]  (Table 12)
        # gstr1["period_label"] — str
        # gstr1["totals"]       — dict        (grand totals)
        # gstr1["warnings"]     — list[str]
    """

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

    def __init__(
        self,
        bills: list[dict]
    ) -> None:
        self.bills = bills

    def _state_from_gstin(self, gstin: Optional[str]) -> str:
        if not gstin or len(gstin) < 2:
            return "Unknown"
        return self._STATE_CODE_MAP.get(gstin[:2], f"State {gstin[:2]}")

    def _build_b2b(
        self,
        bills: list[dict],
        warnings: list[str]
    ) -> list[dict]:
        """
        GSTR-1 Table 4 — B2B taxable outward supplies.
        One row per invoice where the buyer has a valid GSTIN.
        Return type is 'Regular' unless bill has return_type set.
        """
        rows = []
        for bill in bills:
            if not _is_output(bill):
                continue

            buyer_gstin = bill.get("buyer_gstin") or ""
            if not buyer_gstin or len(buyer_gstin) < 15:
                continue

            taxable =   _safe(bill.get("taxable_amount") or bill.get("subtotal"))
            cgst =      _safe(bill.get("cgst_amount"))
            sgst =      _safe(bill.get("sgst_amount"))
            igst =      _safe(bill.get("igst_amount"))
            cess =      _safe(bill.get("cess_amount"))
            grand =     _safe(bill.get("grand_total"))
            rate =      _effective_rate(bill.get("cgst_rate"), bill.get("sgst_rate"), bill.get("igst_rate"))
            inv_no =    bill.get("invoice_number") or ""
            inv_date =  bill.get("invoice_date", "")
            supply =    _supply_type(bill)
            pos =       bill.get("place_of_supply") or self._state_from_gstin(buyer_gstin)
            ret_type =  bill.get("return_type")

            if not inv_no:
                warnings.append(
                    f"B2B bill for GSTIN {buyer_gstin} has no invoice number — included with blank invoice no."
                )

            row_type = "Credit Note" if ret_type == "credit_note" else \
                       "Debit Note" if ret_type == "debit_note" else "Regular"
            
            rows.append({
                "receiver_gstin":   buyer_gstin,
                "receiver_name":    bill.get("buyer_name") or "",
                "invoice_number":   inv_no,
                "invoice_date":     inv_date,
                "invoice_value":    grand,
                "place_of_supply":  pos,
                "supply_type":      supply,          # "intra" | "inter"
                "gst_rate":         rate,
                "taxable_value":    taxable,
                "cgst":             cgst,
                "sgst":             sgst,
                "igst":             igst,
                "cess":             cess,
                "row_type":         row_type,            # "Regular" | "Credit Note" | "Debit Note"
            })

        
        def _build_b2c_large(
            self, 
            bills: list[dict]
        ) -> list[dict]:
            """
            GSTR-1 Table 7 — B2C Large inter-state supplies.
            Consolidated per place-of-supply + GST rate.
            Only inter-state invoices above ₹2.5 lakh without a buyer GSTIN.
            """
            # key: (place_of_supply, gst_rate)
        groups: dict[tuple, dict] = defaultdict(lambda: {
            "taxable_value": 0.0, 
            "igst": 0.0, 
            "cess": 0.0, 
            "invoice_count": 0
        })

        for bill in bills:
            if not _is_output(bill):
                continue
            # Must NOT have a buyer GSTIN (those go into B2B)
            if bill.get("buyer_gstin"):
                continue

            if _supply_type(bill) != "inter":
                continue

            grand = _safe(bill.get("grand_total"))
            if grand < self._B2C_LARGE_THRESHOLD:
                continue

            pos = bill.get("place_of_supply") or "Unknown"
            rate = _effective_rate(bill.get("cgst_rate"), bill.get("sgst_rate"), bill.get("igst_rate"))
            key = (pos, rate)

            g = groups[key]
            g["taxable_value"]  += _safe(bill.get("taxable_amount") or bill.get("subtotal"))
            g["igst"]           += _safe(bill.get("igst_amount"))
            g["cess"]           += _safe(bill.get("cess_amount"))
            g["invoice_count"]  += 1

        rows = []
        for (pos, rate), totals in sorted(groups.items()):
            rows.append({
                "place_of_supply":  pos,
                "gst_rate":         rate,
                "taxable_value":    round(totals["taxable_value"], 2),
                "igst":             round(totals["igst"], 2),
                "cess":             round(totals["cess"], 2),
                "invoice_count":    totals["invoice_count"],
            })
        return rows

    
    def _build_nil_rated(
        self, 
        bills: list[dict]
    ) -> dict:
        """
        GSTR-1 Table 8 — Nil-rated, exempt, and non-GST outward supplies.
        Returns aggregate totals (GSTR-1 does not require invoice-wise detail here).

        nil_rated  : supplies attracting 0% GST (e.g. fresh vegetables, books)
        exempt     : supplies exempted under GST Act
        non_gst    : supplies outside GST purview entirely (e.g. petrol, alcohol)
        """

        nil_b2b = nil_b2c = 0.0
        exempt_b2b = exempt_b2c = 0.0
        non_gst_b2b = non_gst_b2c = 0.0

        for bill in bills:
            if not _is_output(bill):
                continue
            if not _is_nil_exempt(bill):
                continue

            taxable =   _safe(bill.get("taxable_amount") or bill.get("subtotal") or bill.get("grand_total"))
            has_buyer = bool(bill.get("buyer_gstin"))
            category =  str(bill.get("nil_category", "nil")).lower()  # "nil" | "exempt" | "non_gst"

            if category == "exempt":
                if has_buyer:
                    exempt_b2b += taxable
                else:
                    exempt_b2c += taxable
            elif category == "non_gst":
                if has_buyer:
                    non_gst_b2b += taxable
                else:
                    non_gst_b2c += taxable
            else:  # default → nil_rated
                if has_buyer:
                    nil_b2b += taxable
                else:
                    nil_b2c += taxable

        return {
            "nil_rated_b2b": round(nil_b2b, 2),
            "nil_rated_b2c": round(nil_b2c, 2),
            "exempt_b2b":    round(exempt_b2b, 2),
            "exempt_b2c":    round(exempt_b2c, 2),
            "non_gst_b2b":   round(non_gst_b2b, 2),
            "non_gst_b2c":   round(non_gst_b2c, 2),
            "total_nil":     round(nil_b2b + nil_b2c, 2),
            "total_exempt":  round(exempt_b2b + exempt_b2c, 2),
            "total_non_gst": round(non_gst_b2b + non_gst_b2c, 2),
        }

    def _build_hsn_summary(
        self, 
        bills: list[dict], 
        warnings: list[str]
    ) -> list[dict]:
        """
        GSTR-1 Table 12 — HSN/SAC-wise summary of outward supplies.
        One row per unique (hsn_sac, uom, gst_rate) combination.
        Items without an HSN code are grouped under 'UNKNOWN' with a warning.
        """

        # key: (hsn_sac, uom, gst_rate)
        groups: dict[tuple, dict] = defaultdict(lambda: {
            "description": "",
            "total_quantity": 0.0,
            "total_value": 0.0,
            "taxable_value": 0.0,
            "cgst": 0.0,
            "sgst": 0.0,
            "igst": 0.0,
            "cess": 0.0,
            "invoice_count": 0,
        })

        unknown_found = False

        for bill in bills:
            if not _is_output(bill):
                continue

            items  = bill.get("items") or []
            grand  = _safe(bill.get("grand_total"))
            cgst_r = bill.get("cgst_rate")
            sgst_r = bill.get("sgst_rate")
            igst_r = bill.get("igst_rate")
            rate   = _effective_rate(cgst_r, sgst_r, igst_r)

            # If there are no line items, treat the entire bill as a single HSN entry
            if not items:
                hsn = bill.get("hsn_sac") or "UNKNOWN"
                desc = bill.get("vendor_name") or bill.get("account_name") or ""
                key = (hsn, "NOS", rate)
                g = groups[key]
                g["description"] = g["description"] or desc
                g["total_value"] += grand
                g["taxable_value"] += _safe(bill.get("taxable_amount") or bill.get("subtotal"))
                g["cgst"] += _safe(bill.get("cgst_amount"))
                g["sgst"] += _safe(bill.get("sgst_amount"))
                g["igst"] += _safe(bill.get("igst_amount"))
                g["cess"] += _safe(bill.get("cess_amount"))
                g["invoice_count"] += 1

                if hsn == "UNKNOWN":
                    unknown_found = True
                continue

            # Distribute bill-level tax across items proportionally by amount
            items_total = sum(_safe(i.get("amount")) for i in items) or grand or 1
            cgst_total = _safe(bill.get("cgst_amount"))
            sgst_total = _safe(bill.get("sgst_amount"))
            igst_total = _safe(bill.get("igst_amount"))
            cess_total = _safe(bill.get("cess_amount"))

            for item in items:
                amt = _safe(item.get("amount"))
                hsn = item.get("hsn_sac") or "UNKNOWN"
                uom = item.get("unit") or "NOS"
                qty = _safe(item.get("quantity"), default=1.0)
                desc = item.get("description") or ""
                share = amt / items_total if items_total else 0

                key = (hsn, uom.upper(), rate)
                g = groups[key]
                g["description"] = g["description"] or desc
                g["total_quantity"] += qty
                g["total_value"] += amt
                g["taxable_value"] += amt   # pre-tax; approximation when taxable_amount not on item
                g["cgst"] += round(cgst_total * share, 2)
                g["sgst"] += round(sgst_total * share, 2)
                g["igst"] += round(igst_total * share, 2)
                g["cess"] += round(cess_total * share, 2)
                g["invoice_count"] += 1

                if hsn == "UNKNOWN":
                    unknown_found = True

        if unknown_found:
            warnings.append(
                "Some outward supply items have no HSN/SAC code. "
                "They are grouped under 'UNKNOWN' in the HSN Summary (Table 12). "
                "Assign correct HSN codes in your invoices for a complete GSTR-1 filing."
            )

        rows = []
        for (hsn, uom, rate), g in sorted(groups.items(), key=lambda x: x[0][0]):
            rows.append({
                "hsn_sac": hsn,
                "description": g["description"],
                "uom": uom,
                "gst_rate": rate,
                "total_quantity": round(g["total_quantity"], 3),
                "total_value": round(g["total_value"], 2),
                "taxable_value": round(g["taxable_value"], 2),
                "cgst": round(g["cgst"], 2),
                "sgst": round(g["sgst"], 2),
                "igst": round(g["igst"], 2),
                "cess": round(g["cess"], 2),
            })
        
        return rows

    
    def _grand_totals(
        self, 
        b2b: list[dict], 
        b2c_large: list[dict], 
        hsn: list[dict]
    ) -> dict:

        def _sum(rows, key):
            return round(sum(r.get(key, 0) or 0 for r in rows), 2)

        return {
            "b2b_invoice_count":len(b2b),
            "b2b_taxable":      _sum(b2b, "taxable_value"),
            "b2b_cgst":         _sum(b2b, "cgst"),
            "b2b_sgst":         _sum(b2b, "sgst"),
            "b2b_igst":         _sum(b2b, "igst"),
            "b2b_cess":         _sum(b2b, "cess"),
            "b2c_large_taxable": _sum(b2c_large, "taxable_value"),
            "b2c_large_igst":   _sum(b2c_large, "igst"),
            "total_taxable":    round(_sum(b2b, "taxable_value") + _sum(b2c_large, "taxable_value"), 2),
            "total_tax":        round(_sum(b2b, "cgst") + _sum(b2b, "sgst") + _sum(b2b, "igst") + _sum(b2c_large, "igst") + _sum(b2b, "cess") + _sum(b2c_large, "cess"), 2),
        }

 
    def build(
        self,
        bills: list[dict],
        period_label: str = "",
    ) -> dict:
        """
        Build GSTR-1 data from a list of bill dicts.

        Parameters
        ----------
        bills
            List of bill dicts — the same list produced by the scanner.
            Only bills with direction='output' are included.
        period_label
            Human-readable filing period, e.g. "Apr-2025" or "Q1 2025-26".

        Returns
        -------
        dict with keys:
            "b2b"          list[dict]   — Table 4  (invoice-wise B2B)
            "b2c_large"    list[dict]   — Table 7  (B2C large inter-state)
            "nil_rated"    dict         — Table 8  (nil / exempt / non-GST totals)
            "hsn_summary"  list[dict]   — Table 12 (HSN-wise summary)
            "period_label" str
            "totals"       dict         — grand totals across all tables
            "warnings"     list[str]    — data quality issues (no HSN, no inv no, etc.)
        """
        warnings: list[str] = []

        b2b = self._build_b2b(bills, warnings)
        b2c_large = self._build_b2c_large(bills)
        nil_rated = self._build_nil_rated(bills)
        hsn = self._build_hsn_summary(bills, warnings)
        totals = self._grand_totals(b2b, b2c_large, hsn)

        return {
            "b2b":          b2b,
            "b2c_large":    b2c_large,
            "nil_rated":    nil_rated,
            "hsn_summary":  hsn,
            "period_label": period_label,
            "totals":       totals,
            "warnings":     warnings,
        }


def build_gstr1(
    bills: list[dict], 
    period_label: str = ""
) -> dict:
    return GSTR1().build(bills, period_label)
