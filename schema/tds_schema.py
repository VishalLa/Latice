"""
All data-structures for the TDS (Tax Deducted at Source) layer.

No business logic lives here — only pure dataclasses and the section
registry that the engine reads from.

Section registry  (FY 2025-26 rates)
--------------------------------------
Section    Description                              Rate Indv/HUF   Rate Others   Single ₹    Annual ₹
---------  ---------------------------------------  -------------   -----------   ---------   --------
192        Salaries                                 per-slab        —             2,50,000    —
194A       Interest (non-banking)                   10 %            10 %          0           5,000
194A_bank  Interest (bank / post-office deposits)   10 %            10 %          0           40,000
194B       Lottery / crossword winnings             30 %            30 %          10,000      —
194C       Contractors / sub-contractors            1 %             2 %           30,000      75,000
194D       Insurance commission                     5 %             5 %           0           15,000
194H       Commission or brokerage                  5 %             5 %           0           15,000
194I_a     Rent — plant, machinery, equipment       2 %             2 %           0           2,40,000
194I_b     Rent — land, building, furniture         10 %            10 %          0           2,40,000
194J_a     Fees for technical services              2 %             2 %           0           30,000
194J_b     Fees for professional services           10 %            10 %          0           30,000
194Q       Purchase of goods (turnover > ₹10 Cr)   0.1 %           0.1 %         0           50,00,000

Threshold rules
---------------
• threshold_single    → TDS applies if a single payment exceeds this amount.
                        0 means there is no per-transaction limit.
• threshold_aggregate → TDS applies once the running annual total to the same
                        deductee under this section exceeds this amount.
                        0 means there is no aggregate limit.
• When the aggregate threshold is newly crossed mid-year, previous payments
  may also attract TDS.  TDSEngine logs a warning in that case.

PAN requirement
---------------
If the deductee's PAN is not available, TDS must be deducted at 20 % or the
rate specified above, whichever is higher (Section 206AA).
TDSEntry.deductee_pan = None triggers a flag in to_dict() and Form 26Q.

Deposit due-dates (Section 200)
--------------------------------
• Deducted April–February : 7th of the following month
• Deducted March           : 30 April of the same FY
TDSSection.due_day_month encodes the day (7 or 30).
Callers use it to compute the actual due date.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Dict, List, Optional

from .ledger_schema import JournalEntry


class DeducteeType(Enum):
    """
    Deductee classification that controls which rate column is applied.

    Rates differ only for Section 194C:
        INDIVIDUAL_HUF → 1 %
        COMPANY_OTHERS → 2 %
    For all other sections both columns are equal.

    UNKNOWN defaults to the higher (Company/Others) rate — conservative / safe.
    """
    INDIVIDUAL_HUF = "Individual/HUF"
    COMPANY_OTHERS = "Company/Others"
    UNKNOWN        = "Unknown"          # treated as Company/Others


class TDSStatus(Enum):
    """Lifecycle of one TDS deduction event."""
    DEDUCTED    = "Deducted"    # TDS withheld from payment; not yet deposited
    DEPOSITED   = "Deposited"   # Challan filed; amount paid to government
    LATE        = "Late"        # Deposit deadline passed without deposi



@dataclass(frozen=True)
class TDSSection:
    """
    Immutable descriptor for one TDS section.

    Parameters
    ----------
    code
        Statutory code exactly as used in Form 26Q / 24Q (e.g. "194J_b").
        An underscore separates sub-clauses; the engine strips it when
        writing the NSDL-format section string.
    description
        Human-readable section name for display and register printing.
    rate_individual_huf
        TDS rate (in percent, 0–100) for Individual or HUF deductees.
    rate_others
        TDS rate for Companies, LLPs, and all other entities.
    threshold_single
        Per-transaction threshold (INR).  Payment strictly above this
        amount triggers TDS.  0 = no per-transaction limit.
    threshold_aggregate
        Annual running-total threshold per deductee (INR).  Once the
        aggregate of all payments under this section in the FY exceeds
        this amount, TDS applies going forward.  0 = no aggregate limit.
    tds_on_gst
        If True TDS base = gross invoice amount including GST.
        If False (default) TDS base = taxable amount excluding GST
        (per CBDT Circular 23/2017 — applicable when GST is shown
        separately on the invoice).
    form
        "26Q" for non-salary payments; "24Q" for salary.
    due_day_month
        Day of the month by which TDS must be deposited.
        Typically 7 (7th of following month).  30 for amounts deducted
        in March (due 30 April).
    """
    code:                str
    description:         str
    rate_individual_huf: float
    rate_others:         float
    threshold_single:    float  = 0.0
    threshold_aggregate: float  = 0.0
    tds_on_gst:          bool   = False
    form:                str    = "26Q"
    due_day_month:       int    = 7



TDS_SECTIONS: Dict[str, TDSSection] = {

    # Salary — slab-based; TDSEngine skips auto-calculation and logs a warning
    "192": TDSSection(
        code                = "192",
        description         = "Salaries",
        rate_individual_huf = 0.0,
        rate_others         = 0.0,
        threshold_single    = 250_000.0,
        threshold_aggregate = 0.0,
        form                = "24Q",
        due_day_month       = 7,
    ),

    # Interest — non-bank
    "194A": TDSSection(
        code                = "194A",
        description         = "Interest — other than on securities (non-banking)",
        rate_individual_huf = 10.0,
        rate_others         = 10.0,
        threshold_aggregate = 5_000.0,
    ),

    # Interest — bank / post-office deposits
    "194A_bank": TDSSection(
        code                = "194A_bank",
        description         = "Interest on bank / post-office deposits",
        rate_individual_huf = 10.0,
        rate_others         = 10.0,
        threshold_aggregate = 40_000.0,    # ₹50,000 for senior citizens (use 40K conservatively)
    ),

    # Lottery
    "194B": TDSSection(
        code                = "194B",
        description         = "Winnings from lottery, crossword puzzle, card game, etc.",
        rate_individual_huf = 30.0,
        rate_others         = 30.0,
        threshold_single    = 10_000.0,
    ),

    # Contractors
    "194C": TDSSection(
        code                = "194C",
        description         = "Payment to contractor / sub-contractor",
        rate_individual_huf = 1.0,
        rate_others         = 2.0,
        threshold_single    = 30_000.0,
        threshold_aggregate = 75_000.0,
    ),

    # Insurance commission
    "194D": TDSSection(
        code                = "194D",
        description         = "Insurance commission",
        rate_individual_huf = 5.0,
        rate_others         = 5.0,
        threshold_aggregate = 15_000.0,
    ),

    # Commission / brokerage
    "194H": TDSSection(
        code                = "194H",
        description         = "Commission or brokerage",
        rate_individual_huf = 5.0,
        rate_others         = 5.0,
        threshold_aggregate = 15_000.0,
    ),

    # Rent — plant, machinery, equipment
    "194I_a": TDSSection(
        code                = "194I_a",
        description         = "Rent — plant, machinery or equipment",
        rate_individual_huf = 2.0,
        rate_others         = 2.0,
        threshold_aggregate = 240_000.0,
        due_day_month       = 30,           # due 30 April for March deductions
    ),

    # Rent — land, building, furniture
    "194I_b": TDSSection(
        code                = "194I_b",
        description         = "Rent — land, building, furniture or fittings",
        rate_individual_huf = 10.0,
        rate_others         = 10.0,
        threshold_aggregate = 240_000.0,
        due_day_month       = 30,
    ),

    # Technical services (lower rate)
    "194J_a": TDSSection(
        code                = "194J_a",
        description         = "Fees for technical services / call-centre / royalty",
        rate_individual_huf = 2.0,
        rate_others         = 2.0,
        threshold_aggregate = 30_000.0,
    ),

    # Professional services (higher rate)
    "194J_b": TDSSection(
        code                = "194J_b",
        description         = "Fees for professional services / non-compete",
        rate_individual_huf = 10.0,
        rate_others         = 10.0,
        threshold_aggregate = 30_000.0,
    ),

    # Purchase of goods (Section 194Q — buyer turnover > ₹10 Cr)
    "194Q": TDSSection(
        code                = "194Q",
        description         = "Purchase of goods (buyer turnover > ₹10 crore)",
        rate_individual_huf = 0.1,
        rate_others         = 0.1,
        threshold_aggregate = 5_000_000.0,  # ₹50 lakh per seller per FY
    ),
}


@dataclass
class TDSEntry:
    """
    One TDS deduction event.  Created by TDSEngine for each bill that
    attracts TDS.  One TDSEntry corresponds to one JournalEntry; the
    TDS Payable line in that entry equals tds_amount.

    Amounts
    -------
    gross_amount  The full invoice amount (including GST) that we are paying.
    tds_base      Amount on which TDS is computed:
                    • = taxable_amount (excl. GST) when GST is shown separately
                    • = gross_amount otherwise
    tds_rate      Rate actually applied, in percent (reflects DeducteeType).
    tds_amount    = round(tds_base × tds_rate / 100)  — rounded to nearest rupee.
    net_payment   = gross_amount − tds_amount  (what we actually transfer).

    Section 206AA
    -------------
    If deductee_pan is None, TDS should be at max(applicable rate, 20 %).
    TDSEngine enforces this; the 206AA_applied flag records it for audit.
    """

    # Required — supplied by TDSEngine
    section_code:      str
    deductee_name:     str
    gross_amount:      float
    tds_base:          float
    tds_rate:          float
    tds_amount:        float
    net_payment:       float

    # Auto-generated
    entry_id:          str           = field(default_factory=lambda: str(uuid.uuid4())[:8].upper())
    date:              date          = field(default_factory=date.today)

    # Deductee info
    deductee_pan:      Optional[str] = None
    deductee_type:     DeducteeType  = DeducteeType.UNKNOWN
    deductee_gstin:    Optional[str] = None   # optional, for documentation

    # Back-references
    source_journal_id: Optional[str] = None   # JournalEntry.entry_id
    invoice_number:    Optional[str] = None

    # Section 206AA flag
    rate_enhanced_206aa: bool = False
    """True if the rate was raised to 20 % because deductee PAN was absent."""

    # Deposit details — populated later when challan is filed
    deposit_date:      Optional[date] = None
    challan_bsr_code:  Optional[str]  = None   # 7-digit BSR code of the bank
    challan_serial:    Optional[str]  = None   # challan serial number
    challan_date:      Optional[date] = None

    @property
    def is_deposited(self) -> bool:
        return self.deposit_date is not None

    @property
    def status(self) -> TDSStatus:
        return TDSStatus.DEPOSITED if self.is_deposited else TDSStatus.DEDUCTED

    @property
    def section(self) -> TDSSection:
        """Look up the section descriptor."""
        return TDS_SECTIONS[self.section_code]

    @property
    def section_description(self) -> str:
        s = TDS_SECTIONS.get(self.section_code)
        return s.description if s else "Unknown"

    @property
    def pan_available(self) -> bool:
        return bool(self.deductee_pan and self.deductee_pan.strip().upper() not in ("", "PANNOTAVBL"))

    @property
    def nsdl_section_code(self) -> str:
        """Section code in NSDL FVU format — removes the underscore sub-clause."""
        return self.section_code.replace("_", "")

    def to_dict(self) -> dict:
        return {
            "entry_id":             self.entry_id,
            "date":                 self.date.strftime("%d-%m-%Y"),
            "section_code":         self.section_code,
            "section_description":  self.section_description,
            "deductee_name":        self.deductee_name,
            "deductee_pan":         self.deductee_pan or "PANNOTAVBL",
            "pan_available":        self.pan_available,
            "deductee_type":        self.deductee_type.value,
            "gross_amount":         self.gross_amount,
            "tds_base":             self.tds_base,
            "tds_rate":             self.tds_rate,
            "tds_amount":           self.tds_amount,
            "net_payment":          self.net_payment,
            "rate_enhanced_206aa":  self.rate_enhanced_206aa,
            "invoice_number":       self.invoice_number or "",
            "source_journal_id":    self.source_journal_id or "",
            "status":               self.status.value,
            "deposit_date":         self.deposit_date.strftime("%d-%m-%Y") if self.deposit_date else "",
            "challan_bsr_code":     self.challan_bsr_code or "",
            "challan_serial":       self.challan_serial or "",
            "challan_date":         self.challan_date.strftime("%d-%m-%Y") if self.challan_date else "",
        }


@dataclass
class TDSRegister:
    """
    Complete TDS deduction register for a financial year or sub-period.

    Produced by TDSEngine.get_register().  All analysis methods read
    self.entries and return fresh computations every time — the register
    is never mutated after creation.
    """
    entries:       List[TDSEntry]
    period_start:  date
    period_end:    date


    @property
    def total_tds_deducted(self) -> float:
        return round(sum(e.tds_amount for e in self.entries), 2)

    @property
    def total_tds_deposited(self) -> float:
        return round(sum(e.tds_amount for e in self.entries if e.is_deposited), 2)

    @property
    def total_tds_pending(self) -> float:
        return round(self.total_tds_deducted - self.total_tds_deposited, 2)

    @property
    def total_gross_amount(self) -> float:
        return round(sum(e.gross_amount for e in self.entries), 2)


    def by_section(self) -> Dict[str, List[TDSEntry]]:
        result: Dict[str, List[TDSEntry]] = {}
        for e in self.entries:
            result.setdefault(e.section_code, []).append(e)
        return result

    def by_deductee(self) -> Dict[str, List[TDSEntry]]:
        result: Dict[str, List[TDSEntry]] = {}
        for e in self.entries:
            result.setdefault(e.deductee_name, []).append(e)
        return result

    def pending_deposit(self) -> List[TDSEntry]:
        """Entries where TDS has been deducted but not yet deposited."""
        return [e for e in self.entries if not e.is_deposited]

    def missing_pan(self) -> List[TDSEntry]:
        """Entries where deductee PAN is unavailable (206AA risk)."""
        return [e for e in self.entries if not e.pan_available]


    def section_summary(self) -> List[dict]:
        """One summary row per TDS section — useful for the 'TDS Payable' report."""
        rows = []
        for code, entries in sorted(self.by_section().items()):
            sec = TDS_SECTIONS.get(code)
            rows.append({
                "section_code":          code,
                "description":           sec.description if sec else "Unknown",
                "transaction_count":     len(entries),
                "gross_amount_total":    round(sum(e.gross_amount for e in entries), 2),
                "tds_base_total":        round(sum(e.tds_base    for e in entries), 2),
                "tds_deducted_total":    round(sum(e.tds_amount  for e in entries), 2),
                "tds_deposited_total":   round(sum(e.tds_amount  for e in entries if e.is_deposited), 2),
                "pending_deposit":       round(sum(e.tds_amount  for e in entries if not e.is_deposited), 2),
                "entries_missing_pan":   sum(1 for e in entries if not e.pan_available),
            })
        return rows

    def deductee_summary(self) -> List[dict]:
        """One row per deductee — useful for checking aggregate thresholds."""
        rows = []
        for name, entries in sorted(self.by_deductee().items()):
            sections = list({e.section_code for e in entries})
            rows.append({
                "deductee_name":         name,
                "deductee_pan":          entries[0].deductee_pan or "PANNOTAVBL",
                "sections":              sections,
                "transaction_count":     len(entries),
                "gross_amount_total":    round(sum(e.gross_amount for e in entries), 2),
                "tds_deducted_total":    round(sum(e.tds_amount   for e in entries), 2),
                "tds_pending_total":     round(sum(e.tds_amount   for e in entries if not e.is_deposited), 2),
            })
        return rows

    def to_dict(self) -> dict:
        return {
            "period_start":           self.period_start.strftime("%d-%m-%Y"),
            "period_end":             self.period_end.strftime("%d-%m-%Y"),
            "total_gross_amount":     self.total_gross_amount,
            "total_tds_deducted":     self.total_tds_deducted,
            "total_tds_deposited":    self.total_tds_deposited,
            "total_tds_pending":      self.total_tds_pending,
            "entries_missing_pan":    len(self.missing_pan()),
            "section_summary":        self.section_summary(),
            "deductee_summary":       self.deductee_summary(),
            "entries":                [e.to_dict() for e in self.entries],
        }


@dataclass
class Form26QLine:
    """
    One row in Form 26Q — maps to one TDSEntry after section 192 is excluded.

    NSDL FVU field names are shown in parentheses where they differ from ours.
    """
    deductee_pan:      str           # "PANNOTAVBL" if unavailable  (col 3)
    deductee_name:     str           # (col 4)
    deductee_type_code: str          # "01"=Company "02"=Firm "11"=Individual etc.
    section_code:      str           # NSDL format: "194Jb" not "194J_b"
    payment_date:      str           # DD-MM-YYYY  (col 9)
    gross_amount:      float         # (col 11)
    tds_base:          float         # (col 12 — amount on which TDS computed)
    tds_rate:          float         # (col 13)
    tds_deducted:      float         # (col 14)
    tds_deposited:     float         # (col 15 — 0 until challan filed)
    rate_enhanced_206aa: bool = False
    challan_bsr_code:  Optional[str] = None
    challan_serial:    Optional[str] = None
    challan_date:      Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "deductee_pan":         self.deductee_pan,
            "deductee_name":        self.deductee_name,
            "deductee_type_code":   self.deductee_type_code,
            "section_code":         self.section_code,
            "payment_date":         self.payment_date,
            "gross_amount":         self.gross_amount,
            "tds_base":             self.tds_base,
            "tds_rate":             self.tds_rate,
            "tds_deducted":         self.tds_deducted,
            "tds_deposited":        self.tds_deposited,
            "rate_enhanced_206aa":  self.rate_enhanced_206aa,
            "challan_bsr_code":     self.challan_bsr_code or "",
            "challan_serial":       self.challan_serial or "",
            "challan_date":         self.challan_date or "",
        }


@dataclass
class Form26Q:
    """
    Quarterly Form 26Q — TDS on non-salary payments.
    Maps to NSDL FVU structure (file-validation utility input).

    Filing deadlines (Section 200(3))
    ----------------------------------
    Quarter   Period            Due date
    Q1        Apr–Jun           31 July
    Q2        Jul–Sep           31 October
    Q3        Oct–Dec           31 January
    Q4        Jan–Mar           31 May
    """
    quarter:        str           # "Q1" … "Q4"
    financial_year: str           # "2025-26"
    period_start:   date
    period_end:     date
    lines:          List[Form26QLine] = field(default_factory=list)

    @property
    def total_gross_amount(self) -> float:
        return round(sum(l.gross_amount for l in self.lines), 2)

    @property
    def total_tds_deducted(self) -> float:
        return round(sum(l.tds_deducted for l in self.lines), 2)

    @property
    def total_tds_deposited(self) -> float:
        return round(sum(l.tds_deposited for l in self.lines), 2)

    @property
    def lines_missing_pan(self) -> List[Form26QLine]:
        return [l for l in self.lines if l.deductee_pan == "PANNOTAVBL"]

    def to_dict(self) -> dict:
        return {
            "quarter":             self.quarter,
            "financial_year":      self.financial_year,
            "period_start":        self.period_start.strftime("%d-%m-%Y"),
            "period_end":          self.period_end.strftime("%d-%m-%Y"),
            "total_gross_amount":  self.total_gross_amount,
            "total_tds_deducted":  self.total_tds_deducted,
            "total_tds_deposited": self.total_tds_deposited,
            "lines_missing_pan":   len(self.lines_missing_pan),
            "lines":               [l.to_dict() for l in self.lines],
        }
    

@dataclass
class TDSResult:
    """
    Output of TDSEngine.process_bill() / process_manual_entry().

    Attributes
    ----------
    journal_entry
        The (possibly modified) JournalEntry.  If TDS applies, the credit
        to the vendor / cash account is split: net_payment → vendor,
        tds_amount → TDS Payable A/c.
    tds_entry
        The TDSEntry added to the register.  None if no TDS applies.
    tds_applied
        True if TDS was actually deducted.
    warnings
        Non-fatal issues — e.g. no PAN, aggregate threshold newly crossed,
        Section 192 (salary) skipped.
    """
    journal_entry: JournalEntry
    tds_entry:     Optional[TDSEntry]
    tds_applied:   bool
    warnings:      List[str]

