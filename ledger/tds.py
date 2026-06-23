from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date
from typing import Dict, List, Optional, Tuple

from schema import (
    Account,
    AccountGroup,
    COA,
    DrCr,
    EntryLine,
    JournalEntry,
    DeducteeType,
    Form26Q,
    Form26QLine,
    TDSEntry,
    TDSRegister,
    TDSSection,
    TDS_SECTIONS,
    TDSStatus,
    TDSResult
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Section 206AA — minimum rate when PAN is unavailable
_SECTION_206AA_RATE = 20.0

# Keywords → TDS section (scanned left-to-right; first match wins)
# Keys are lowercase substrings; values are TDS_SECTIONS keys.
_KEYWORD_SECTION_MAP: List[Tuple[str, str]] = [
    # Salary — must come before "wages" keyword match for contractors
    ("salary",              "192"),
    ("salaries",            "192"),
    # Rent — specific types first, generic last
    ("plant rent",          "194I_a"),
    ("machinery rent",      "194I_a"),
    ("equipment rent",      "194I_a"),
    ("office rent",         "194I_b"),
    ("building rent",       "194I_b"),
    ("godown rent",         "194I_b"),
    ("shop rent",           "194I_b"),
    ("house rent",          "194I_b"),
    ("rent",                "194I_b"),      # generic rent → land/building
    # Professional services
    ("professional fee",    "194J_b"),
    ("professional fees",   "194J_b"),
    ("advocate",            "194J_b"),
    ("lawyer",              "194J_b"),
    ("solicitor",           "194J_b"),
    ("chartered accountant","194J_b"),
    (" ca fee",             "194J_b"),
    ("company secretary",   "194J_b"),
    (" cs fee",             "194J_b"),
    ("doctor",              "194J_b"),
    ("medical consultation","194J_b"),
    ("non-compete",         "194J_b"),
    # Technical services
    ("technical service",   "194J_a"),
    ("it service",          "194J_a"),
    ("software service",    "194J_a"),
    ("software support",    "194J_a"),
    ("call centre",         "194J_a"),
    ("call center",         "194J_a"),
    ("data processing",     "194J_a"),
    ("royalty",             "194J_a"),
    # Contractors
    ("contractor",          "194C"),
    ("sub-contractor",      "194C"),
    ("subcontractor",       "194C"),
    ("labour supply",       "194C"),
    ("manpower",            "194C"),
    ("erection",            "194C"),
    ("installation",        "194C"),
    ("civil work",          "194C"),
    ("construction",        "194C"),
    ("fabrication",         "194C"),
    ("hauling",             "194C"),
    ("carriage",            "194C"),
    ("transport contract",  "194C"),
    # Commission / brokerage
    ("commission",          "194H"),
    ("brokerage",           "194H"),
    ("agent fee",           "194H"),
    ("agent fees",          "194H"),
    ("insurance commission","194D"),
    # Interest
    ("bank interest",       "194A_bank"),
    ("fd interest",         "194A_bank"),
    ("deposit interest",    "194A_bank"),
    ("interest",            "194A"),
]

# NSDL deductee type codes for Form 26Q
_DEDUCTEE_TYPE_CODES: Dict[DeducteeType, str] = {
    DeducteeType.COMPANY_OTHERS : "01",
    DeducteeType.INDIVIDUAL_HUF : "11",
    DeducteeType.UNKNOWN        : "01",   # treat as company — conservative
}


class TDSEngine:
    """
    Stateful TDS engine for one financial year.

    Parameters
    ----------
    financial_year
        "2025-26" format.  Used in Form 26Q header and deposit due-date
        computation.
    deductor_tan
        Tax Deduction Account Number of the business.  Printed on Form 26Q.
    deductor_name
        Legal name of the deducting entity.
    """

    def __init__(
        self,
        financial_year: str = "2025-26",
        deductor_tan:   Optional[str] = None,
        deductor_name:  Optional[str] = None,
    ) -> None:
        self.financial_year = financial_year
        self.deductor_tan   = deductor_tan
        self.deductor_name  = deductor_name

        self._entries: List[TDSEntry] = []

        # Aggregate tracker: (deductee_name, section_code) → cumulative gross paid
        self._aggregates: Dict[Tuple[str, str], float] = {}


    def process_bill(
        self,
        bill:              dict,
        journal_entry:     Optional[JournalEntry] = None,
        deductee_pan:      Optional[str] = None,
        deductee_type:     DeducteeType  = DeducteeType.UNKNOWN,
        deductee_gstin:    Optional[str] = None,
        tds_section:       Optional[str] = None,   # override auto-detection
    ) -> TDSResult:
        """
        Evaluate a bill dict for TDS applicability and (if applicable) modify
        its journal entry to split the credit between vendor and TDS Payable.

        Parameters
        ----------
        bill
            Raw bill dict as produced by the scanner / data_extractor.
        journal_entry
            Pre-built JournalEntry for this bill (from journal.to_journal_entry).
            If None the engine cannot modify the entry — only TDS detection
            and register update are performed.
        deductee_pan
            PAN of the party being paid.  Pass None if unavailable.
        deductee_type
            Individual/HUF or Company/Others — affects rate for 194C.
        deductee_gstin
            Optional GSTIN for documentation.
        tds_section
            Explicit section code (e.g. "194J_b").  If provided, bypasses
            keyword auto-detection.

        Returns
        -------
        TDSResult
        """
        warnings:  List[str] = []
        vendor     = bill.get("vendor_name") or "Unknown Vendor"
        inv_no     = bill.get("invoice_number") or ""
        entry_date = _parse_date(bill.get("invoice_date"))
        grand      = _safe(bill.get("grand_total"))
        taxable    = _safe(bill.get("taxable_amount") or bill.get("subtotal"))
        cgst       = _safe(bill.get("cgst_amount"))
        sgst       = _safe(bill.get("sgst_amount"))
        igst       = _safe(bill.get("igst_amount"))

        # Build a description string for keyword matching
        description = " ".join(filter(None, [
            str(bill.get("account_name", "")),
            str(bill.get("narration", "")),
            str(bill.get("expense_type", "")),
            vendor,
        ]))

        # Determine section
        section_code = tds_section or self.detect_section(description)
        if not section_code:
            # No TDS applicable
            return TDSResult(
                journal_entry = journal_entry,
                tds_entry     = None,
                tds_applied   = False,
                warnings      = [],
            )

        section = TDS_SECTIONS.get(section_code)
        if not section:
            warnings.append(f"Unknown TDS section '{section_code}' — TDS skipped.")
            return TDSResult(
                journal_entry = journal_entry,
                tds_entry     = None,
                tds_applied   = False,
                warnings      = warnings,
            )

        # Section 192 — salary; slab-based, cannot auto-calculate
        if section_code == "192":
            warnings.append(
                f"Section 192 (Salary) detected for '{vendor}'. "
                "TDS on salaries is slab-based and must be computed manually. "
                "No TDS entry was created — record it via a manual journal entry."
            )
            return TDSResult(
                journal_entry = journal_entry,
                tds_entry     = None,
                tds_applied   = False,
                warnings      = warnings,
            )

        # Compute TDS base
        tds_base = self._compute_tds_base(section, grand, taxable, cgst, sgst, igst)

        # Check thresholds
        applies, agg_warning = self._check_thresholds(
            section_code, vendor, grand, tds_base, entry_date
        )
        if agg_warning:
            warnings.append(agg_warning)
        if not applies:
            return TDSResult(
                journal_entry = journal_entry,
                tds_entry     = None,
                tds_applied   = False,
                warnings      = warnings,
            )

        # Determine rate (Section 206AA if no PAN)
        rate, enhanced_206aa = self._compute_rate(section, deductee_type, deductee_pan)
        if enhanced_206aa:
            warnings.append(
                f"Section 206AA: PAN not available for '{vendor}'. "
                f"TDS rate raised to {rate}%."
            )

        tds_amount  = round(tds_base * rate / 100)   # rounded to nearest rupee per IT rules
        net_payment = round(grand - tds_amount, 2)

        # Build TDSEntry
        tds_entry = TDSEntry(
            section_code         = section_code,
            deductee_name        = vendor,
            gross_amount         = grand,
            tds_base             = tds_base,
            tds_rate             = rate,
            tds_amount           = tds_amount,
            net_payment          = net_payment,
            date                 = entry_date,
            deductee_pan         = deductee_pan,
            deductee_type        = deductee_type,
            deductee_gstin       = deductee_gstin,
            source_journal_id    = journal_entry.entry_id if journal_entry else None,
            invoice_number       = inv_no,
            rate_enhanced_206aa  = enhanced_206aa,
        )
        self._entries.append(tds_entry)
        logger.info(
            "TDS entry created: %s | %s | ₹%s @ %s%% = ₹%s",
            tds_entry.entry_id, section_code, grand, rate, tds_amount,
        )

        # Modify journal entry if one was provided
        modified_entry = journal_entry
        if journal_entry is not None:
            modified_entry = self._inject_tds_into_entry(
                journal_entry, tds_amount, net_payment, vendor, section_code, inv_no
            )

        return TDSResult(
            journal_entry = modified_entry,
            tds_entry     = tds_entry,
            tds_applied   = True,
            warnings      = warnings,
        )

    def process_manual_entry(
        self,
        entry_dict:     dict,
        journal_entry:  JournalEntry,
        deductee_name:  str,
        section_code:   str,
        gross_amount:   float,
        deductee_pan:   Optional[str] = None,
        deductee_type:  DeducteeType  = DeducteeType.UNKNOWN,
        deductee_gstin: Optional[str] = None,
        tds_base:       Optional[float] = None,
    ) -> TDSResult:
        """
        Process a manually entered journal entry for TDS.

        Used when the user has already written an entry in opening_balances.json
        or a manual_entries block that explicitly records a TDS-attracting payment.
        All TDS parameters are supplied explicitly — no auto-detection.

        Parameters
        ----------
        entry_dict
            The raw dict from opening_balances.json / manual_entries.
        journal_entry
            The already-built JournalEntry for this payment.
        deductee_name
            Name of the party from whom TDS is deducted.
        section_code
            Mandatory — must be a key in TDS_SECTIONS.
        gross_amount
            Full invoice / payment amount including GST.
        tds_base
            Amount on which TDS is computed.  If None, falls back to
            gross_amount (conservative).
        """
        warnings: List[str] = []
        entry_date = journal_entry.date

        if section_code == "192":
            warnings.append(
                "Section 192 (Salary) — slab-based TDS cannot be auto-calculated. "
                "No TDS entry was created."
            )
            return TDSResult(
                journal_entry = journal_entry,
                tds_entry     = None,
                tds_applied   = False,
                warnings      = warnings,
            )

        section = TDS_SECTIONS.get(section_code)
        if not section:
            warnings.append(f"Unknown TDS section '{section_code}' — TDS skipped.")
            return TDSResult(
                journal_entry = journal_entry,
                tds_entry     = None,
                tds_applied   = False,
                warnings      = warnings,
            )

        effective_tds_base = tds_base if tds_base is not None else gross_amount

        # Threshold check
        applies, agg_warning = self._check_thresholds(
            section_code, deductee_name, gross_amount, effective_tds_base, entry_date
        )
        if agg_warning:
            warnings.append(agg_warning)
        if not applies:
            return TDSResult(
                journal_entry = journal_entry,
                tds_entry     = None,
                tds_applied   = False,
                warnings      = warnings,
            )

        rate, enhanced_206aa = self._compute_rate(section, deductee_type, deductee_pan)
        if enhanced_206aa:
            warnings.append(
                f"Section 206AA: PAN not available for '{deductee_name}'. "
                f"TDS rate raised to {rate}%."
            )

        tds_amount  = round(effective_tds_base * rate / 100)
        net_payment = round(gross_amount - tds_amount, 2)

        inv_no = entry_dict.get("invoice_number") or entry_dict.get("narration") or ""

        tds_entry = TDSEntry(
            section_code         = section_code,
            deductee_name        = deductee_name,
            gross_amount         = gross_amount,
            tds_base             = effective_tds_base,
            tds_rate             = rate,
            tds_amount           = tds_amount,
            net_payment          = net_payment,
            date                 = entry_date,
            deductee_pan         = deductee_pan,
            deductee_type        = deductee_type,
            deductee_gstin       = deductee_gstin,
            source_journal_id    = journal_entry.entry_id,
            invoice_number       = inv_no,
            rate_enhanced_206aa  = enhanced_206aa,
        )
        self._entries.append(tds_entry)

        modified_entry = self._inject_tds_into_entry(
            journal_entry, tds_amount, net_payment, deductee_name, section_code, inv_no
        )

        return TDSResult(
            journal_entry = modified_entry,
            tds_entry     = tds_entry,
            tds_applied   = True,
            warnings      = warnings,
        )

    def tds_deposit_entry(
        self,
        tds_entries:   List[TDSEntry],
        deposit_date:  date,
        challan_bsr:   str,
        challan_serial: str,
        payment_mode:  str = "BANK",
    ) -> JournalEntry:
        """
        Generate the journal entry for depositing TDS with the government.

        One challan can cover multiple TDS entries (common in practice).
        The entry debits TDS Payable and credits Bank.

            TDS Payable A/c     Dr.   [total tds_amount]
              To Bank A/c           Cr.   [total tds_amount]

        Parameters
        ----------
        tds_entries
            List of TDSEntry objects being settled in this challan.
        deposit_date
            Date of the challan / online payment.
        challan_bsr
            7-digit BSR code of the bank branch.
        challan_serial
            Challan serial number from the bank.
        payment_mode
            "BANK" (default) or "CASH".

        Returns
        -------
        JournalEntry ready to be posted to the General Ledger.
        """
        total = round(sum(e.tds_amount for e in tds_entries), 2)
        if total <= 0:
            raise ValueError("TDS deposit entry: total TDS amount must be positive.")

        section_codes = ", ".join(sorted({e.section_code for e in tds_entries}))
        payment_acc   = COA.BANK if payment_mode.upper() != "CASH" else COA.CASH

        entry = JournalEntry(
            date         = deposit_date,
            voucher_type = "Payment Voucher",
            narration    = (
                f"TDS deposited — Challan BSR {challan_bsr} / "
                f"Serial {challan_serial} — Sections: {section_codes}"
            ),
            lines        = [
                EntryLine(
                    COA.TDS_PAYABLE,
                    DrCr.DEBIT,
                    total,
                    f"TDS Payable settled via challan {challan_bsr}/{challan_serial}",
                ),
                EntryLine(
                    payment_acc,
                    DrCr.CREDIT,
                    total,
                    f"TDS deposited to govt — {section_codes}",
                ),
            ],
        )
        return entry

    def mark_deposited(
        self,
        entry_id:       str,
        deposit_date:   date,
        challan_bsr:    str,
        challan_serial: str,
        challan_date:   Optional[date] = None,
    ) -> bool:
        """
        Mark an existing TDSEntry as deposited after the challan is filed.

        Parameters
        ----------
        entry_id
            TDSEntry.entry_id to update.
        deposit_date
            Date of the challan deposit.
        challan_bsr
            7-digit BSR code.
        challan_serial
            Challan serial number from the bank receipt.
        challan_date
            Date printed on the challan (usually same as deposit_date).

        Returns
        -------
        True if the entry was found and updated; False if not found.
        """
        for tds_e in self._entries:
            if tds_e.entry_id == entry_id:
                # TDSEntry is a dataclass (not frozen) so direct assignment works
                object.__setattr__(tds_e, "deposit_date",    deposit_date)
                object.__setattr__(tds_e, "challan_bsr_code", challan_bsr)
                object.__setattr__(tds_e, "challan_serial",   challan_serial)
                object.__setattr__(tds_e, "challan_date",     challan_date or deposit_date)
                logger.info("TDS entry %s marked as deposited on %s.", entry_id, deposit_date)
                return True
        logger.warning("mark_deposited: entry_id '%s' not found in register.", entry_id)
        return False

    def get_register(
        self,
        period_start: Optional[date] = None,
        period_end:   Optional[date] = None,
    ) -> TDSRegister:
        """
        Return the TDS register, optionally filtered to a date range.

        Parameters
        ----------
        period_start / period_end
            If both are provided only entries whose date falls within
            [period_start, period_end] (inclusive) are included.
        """
        entries = self._entries
        if period_start and period_end:
            entries = [
                e for e in entries
                if period_start <= e.date <= period_end
            ]
        start = period_start or (min(e.date for e in entries) if entries else date.today())
        end   = period_end   or (max(e.date for e in entries) if entries else date.today())

        return TDSRegister(
            entries      = list(entries),
            period_start = start,
            period_end   = end,
        )

    def build_form_26q(
        self,
        quarter:      str,
        period_start: date,
        period_end:   date,
    ) -> Form26Q:
        """
        Build a Form 26Q summary for a quarter.

        Excludes Section 192 (salary — goes to Form 24Q).
        All amounts in INR.

        Parameters
        ----------
        quarter
            "Q1" | "Q2" | "Q3" | "Q4"
        period_start / period_end
            Quarter date range.

        Returns
        -------
        Form26Q ready to be serialised via .to_dict().
        """
        register = self.get_register(period_start, period_end)

        lines: List[Form26QLine] = []
        for e in register.entries:
            if e.section_code == "192":
                continue     # salary → Form 24Q, not 26Q

            lines.append(Form26QLine(
                deductee_pan        = e.deductee_pan or "PANNOTAVBL",
                deductee_name       = e.deductee_name,
                deductee_type_code  = _DEDUCTEE_TYPE_CODES[e.deductee_type],
                section_code        = e.nsdl_section_code,
                payment_date        = e.date.strftime("%d-%m-%Y"),
                gross_amount        = e.gross_amount,
                tds_base            = e.tds_base,
                tds_rate            = e.tds_rate,
                tds_deducted        = e.tds_amount,
                tds_deposited       = e.tds_amount if e.is_deposited else 0.0,
                rate_enhanced_206aa = e.rate_enhanced_206aa,
                challan_bsr_code    = e.challan_bsr_code,
                challan_serial      = e.challan_serial,
                challan_date        = (
                    e.challan_date.strftime("%d-%m-%Y")
                    if e.challan_date else None
                ),
            ))

        return Form26Q(
            quarter        = quarter,
            financial_year = self.financial_year,
            period_start   = period_start,
            period_end     = period_end,
            lines          = lines,
        )

    def pending_deposits(self, as_on: Optional[date] = None) -> List[dict]:
        """
        Return all TDS entries where deposit is overdue.

        Parameters
        ----------
        as_on
            Reference date for due-date computation (defaults to today).

        Returns
        -------
        List of dicts with keys: entry_id, section_code, deductee_name,
        tds_amount, deduction_date, due_date, days_overdue.
        """
        ref = as_on or date.today()
        result = []
        for e in self._entries:
            if e.is_deposited:
                continue
            due = _tds_due_date(e.date, TDS_SECTIONS.get(e.section_code))
            days_overdue = (ref - due).days
            result.append({
                "entry_id":       e.entry_id,
                "section_code":   e.section_code,
                "deductee_name":  e.deductee_name,
                "invoice_number": e.invoice_number or "",
                "tds_amount":     e.tds_amount,
                "deduction_date": e.date.strftime("%d-%m-%Y"),
                "due_date":       due.strftime("%d-%m-%Y"),
                "days_overdue":   max(days_overdue, 0),
                "is_overdue":     days_overdue > 0,
            })
        result.sort(key=lambda x: x["days_overdue"], reverse=True)
        return result


    @staticmethod
    def detect_section(description: str) -> Optional[str]:
        """
        Auto-detect TDS section from a free-text description.

        Scans the keyword table in _KEYWORD_SECTION_MAP.
        Returns the section code string (e.g. "194J_b") or None.
        """
        text = description.lower()
        for keyword, section_code in _KEYWORD_SECTION_MAP:
            if keyword in text:
                return section_code
        return None

    @staticmethod
    def applicable_sections_for_amount(
        gross_amount:   float,
        section_code:   str,
        deductee_name:  str,
        aggregates:     Dict[Tuple[str, str], float],
    ) -> bool:
        """
        Check whether thresholds are met for a given payment.

        Exposed as a static helper so callers can pre-check before committing.
        """
        section = TDS_SECTIONS.get(section_code)
        if not section:
            return False
        agg_key = (deductee_name.lower().strip(), section_code)
        running = aggregates.get(agg_key, 0.0)

        if section.threshold_single > 0 and gross_amount <= section.threshold_single:
            if running + gross_amount <= section.threshold_aggregate:
                return False
        if section.threshold_aggregate > 0 and (running + gross_amount) <= section.threshold_aggregate:
            return False
        return True
    

    def _compute_tds_base(
        self,
        section:  TDSSection,
        grand:    float,
        taxable:  float,
        cgst:     float,
        sgst:     float,
        igst:     float,
    ) -> float:
        """
        Compute the amount on which TDS is applied.

        Per CBDT Circular 23/2017: if GST is shown separately on the invoice
        (section.tds_on_gst == False), TDS base = taxable_amount.
        Otherwise (section.tds_on_gst == True), TDS base = gross including GST.
        """
        if section.tds_on_gst:
            return round(grand, 2)

        gst_total = cgst + sgst + igst
        if taxable > 0:
            return round(taxable, 2)
        # Fall back: strip GST from grand
        derived_taxable = round(grand - gst_total, 2)
        return max(derived_taxable, grand)   # if no GST info, use grand


    def _check_thresholds(
        self,
        section_code:  str,
        deductee_name: str,
        gross_amount:  float,
        tds_base:      float,
        entry_date:    date,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check single-payment and aggregate thresholds.

        Updates the running aggregate unconditionally (the gross is always
        counted toward the annual limit, whether or not TDS is deducted).

        Returns (applies: bool, warning: str | None).
        """
        section  = TDS_SECTIONS.get(section_code)
        if not section:
            return False, None

        agg_key  = (deductee_name.lower().strip(), section_code)
        running  = self._aggregates.get(agg_key, 0.0)
        warning: Optional[str] = None

        # Single-payment threshold check
        single_ok = (
            section.threshold_single == 0.0
            or gross_amount > section.threshold_single
        )

        # Aggregate threshold check
        new_running = running + gross_amount
        if section.threshold_aggregate > 0:
            if new_running <= section.threshold_aggregate:
                # Not crossed yet
                self._aggregates[agg_key] = new_running
                return False, None
            elif running <= section.threshold_aggregate:
                # Crossed for the first time with this payment
                warning = (
                    f"Section {section_code}: Aggregate threshold "
                    f"₹{section.threshold_aggregate:,.0f} crossed for "
                    f"'{deductee_name}'. Running total: "
                    f"₹{new_running:,.2f}. Review prior payments — "
                    f"TDS may have been missed on earlier transactions."
                )

        # Update aggregate
        self._aggregates[agg_key] = new_running

        applies = single_ok or (
            section.threshold_single == 0.0 and section.threshold_aggregate == 0.0
        )
        # If neither threshold type applies, TDS always applies
        if section.threshold_single == 0.0 and section.threshold_aggregate == 0.0:
            applies = True

        return applies, warning


    @staticmethod
    def _compute_rate(
        section:       TDSSection,
        deductee_type: DeducteeType,
        pan:           Optional[str],
    ) -> Tuple[float, bool]:
        """
        Return (rate_percent, enhanced_206aa).

        Picks rate_individual_huf or rate_others based on deductee_type.
        Applies Section 206AA (20 % minimum) when PAN is absent.
        """
        if deductee_type == DeducteeType.INDIVIDUAL_HUF:
            base_rate = section.rate_individual_huf
        else:
            base_rate = section.rate_others

        pan_missing = not pan or pan.strip().upper() in ("", "PANNOTAVBL")
        if pan_missing and base_rate < _SECTION_206AA_RATE:
            return _SECTION_206AA_RATE, True
        return base_rate, False


    @staticmethod
    def _inject_tds_into_entry(
        original:      JournalEntry,
        tds_amount:    float,
        net_payment:   float,
        vendor:        str,
        section_code:  str,
        inv_no:        str,
    ) -> JournalEntry:
        """
        Return a new JournalEntry where the vendor / cash credit line is
        split: net_payment → vendor/cash, tds_amount → TDS Payable A/c.

        Strategy
        --------
        1. Find the largest credit line (the vendor / cash / bank credit).
        2. Reduce its amount to net_payment.
        3. Insert a new credit line for TDS Payable.
        4. Re-validate the entry (debits == credits).
        """
        # Identify the credit line to split — the one with the largest amount
        credit_lines = [l for l in original.lines if l.dr_cr == DrCr.CREDIT]
        if not credit_lines:
            logger.warning(
                "_inject_tds_into_entry: no credit lines found in entry %s — "
                "TDS Payable line appended without reducing any credit.",
                original.entry_id,
            )
            # Append TDS Payable without touching existing lines
            new_lines = list(original.lines) + [
                EntryLine(
                    COA.TDS_PAYABLE,
                    DrCr.CREDIT,
                    tds_amount,
                    f"TDS Payable — Sec {section_code} — {vendor} — Inv {inv_no}",
                )
            ]
            return _rebuild_entry(original, new_lines)

        # The primary credit is typically the vendor creditor or cash/bank
        # Sort: prefer non-GST, non-discount lines; take the largest
        primary = max(
            credit_lines,
            key=lambda l: (
                l.account not in (
                    COA.OUTPUT_CGST, COA.OUTPUT_SGST, COA.OUTPUT_IGST,
                    COA.OUTPUT_CESS, COA.DISCOUNT_RECV,
                ),
                l.amount,
            ),
        )

        new_lines = []
        for line in original.lines:
            if line is primary:
                # Replace with reduced amount
                if net_payment > 0:
                    new_lines.append(EntryLine(
                        line.account,
                        DrCr.CREDIT,
                        net_payment,
                        f"{line.narration} (net after TDS)",
                    ))
                # Add TDS Payable credit
                new_lines.append(EntryLine(
                    COA.TDS_PAYABLE,
                    DrCr.CREDIT,
                    tds_amount,
                    f"TDS Payable u/s {section_code} on payment to {vendor} — Inv {inv_no}",
                ))
            else:
                new_lines.append(line)

        return _rebuild_entry(original, new_lines)


# Standalone helpers — used by journal.py integration points
def tds_receivable_entry(
    gross_amount:  float,
    tds_deducted:  float,
    payer_name:    str,
    section_code:  str,
    invoice_date:  date,
    invoice_number: str = "",
) -> JournalEntry:
    """
    Record TDS deducted BY the customer on our sales invoice.

    When our customer deducts TDS before paying us, we receive less cash
    but retain the right to claim TDS credit when filing our IT return.
    Entry:
        Cash / Bank A/c         Dr.   [net_received]
        TDS Receivable A/c      Dr.   [tds_amount]
          To Sales A/c / Debtor     Cr.   [gross_amount]

    This entry is generated on the SALES side — the company is the deductee.

    Parameters
    ----------
    gross_amount
        Full invoice value (what was originally booked as debtor balance).
    tds_deducted
        Amount withheld by the payer.
    payer_name
        Name of the customer / debtor who deducted TDS.
    section_code
        Section under which TDS was deducted (for narration).
    invoice_date
        Date of settlement / receipt.
    """
    net_received = round(gross_amount - tds_deducted, 2)
    narration    = (
        f"TDS deducted by {payer_name} u/s {section_code} "
        f"on Inv {invoice_number} — Net received ₹{net_received:,.2f}"
    )
    return JournalEntry(
        date         = invoice_date,
        voucher_type = "Receipt Voucher",
        narration    = narration,
        lines        = [
            EntryLine(COA.BANK, DrCr.DEBIT, net_received, f"Net receipt from {payer_name} after TDS"),
            EntryLine(COA.TDS_RECEIVABLE, DrCr.DEBIT, tds_deducted, f"TDS Receivable u/s {section_code} — {payer_name}"),
            EntryLine(COA.debtor_for(payer_name), DrCr.CREDIT, gross_amount, f"Settlement of debtor balance — {payer_name}"),
        ],
        invoice_number = invoice_number,
        vendor_name    = payer_name,
        direction      = "output",
    )


def tds_certificate_entry(
    tds_receivable_amount: float,
    certificate_date:      date,
    form_16a_number:       str = "",
) -> JournalEntry:
    """
    Adjust TDS Receivable once Form 16A is received and the credit is
    confirmed in Form 26AS / AIS.

    Entry:
        Advance Tax / Self-Assessment A/c   Dr.   [tds_receivable_amount]
          To TDS Receivable A/c                 Cr.   [tds_receivable_amount]

    Note: "Advance Tax A/c" is mapped to Current Assets here; some firms
    use a dedicated Advance Tax account.
    """
    advance_tax = Account("Advance Tax / TDS Credit A/c", AccountGroup.CURRENT_ASSETS)
    narration   = (
        f"TDS credit confirmed via Form 16A / 26AS "
        f"{'— Cert ' + form_16a_number if form_16a_number else ''}"
    )
    return JournalEntry(
        date         = certificate_date,
        voucher_type = "Journal Voucher",
        narration    = narration,
        lines        = [
            EntryLine(advance_tax, DrCr.DEBIT,  tds_receivable_amount, "TDS credit transferred to Advance Tax A/c"),
            EntryLine(COA.TDS_RECEIVABLE, DrCr.CREDIT, tds_receivable_amount, "TDS Receivable — credit confirmed in 26AS"),
        ],
    )


def _safe(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_date(raw) -> date:
    if not raw:
        return date.today()
    from dateutil import parser as dp
    try:
        return dp.parse(str(raw), dayfirst=True).date()
    except Exception:
        return date.today()


def _tds_due_date(deduction_date: date, section: Optional[TDSSection]) -> date:
    """
    Compute TDS deposit due date.

    Section 200 rules:
    • Deducted Apr–Feb : 7th of the following month
    • Deducted March   : 30 April of the same year
    """
    due_day = section.due_day_month if section else 7

    if deduction_date.month == 3 and due_day == 30:
        # March deductions for rent sections — due 30 April
        return date(deduction_date.year, 4, 30)
    elif deduction_date.month == 3:
        # Standard March — due 30 April
        return date(deduction_date.year, 4, 30)
    else:
        # Any other month — 7th of next month
        next_month = deduction_date.month + 1
        next_year  = deduction_date.year
        if next_month > 12:
            next_month = 1
            next_year += 1
        last_day = monthrange(next_year, next_month)[1]
        return date(next_year, next_month, min(7, last_day))


def _rebuild_entry(original: JournalEntry, new_lines: List[EntryLine]) -> JournalEntry:
    """Return a new JournalEntry with updated lines, preserving all metadata."""
    return JournalEntry(
        date           = original.date,
        voucher_type   = original.voucher_type,
        narration      = original.narration,
        lines          = new_lines,
        source_file    = original.source_file,
        invoice_number = original.invoice_number,
        vendor_name    = original.vendor_name,
        direction      = original.direction,
    )
