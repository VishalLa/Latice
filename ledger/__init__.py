from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from .general_ledger_class import GeneralLedger
from .ledger import LedgerBuilder, build_ledger
from .opening_balances import load_opening_balances
from .journal import JournalBuilder
from .tds import TDSEngine
from .gstr1 import GSTR1Builder

from schema import DeducteeType, Form26Q, JournalEntry, DrCr, TrialBalance

__all__ = [
    "build_complete_ledger",
    
    "GeneralLedger",
    "LedgerBuilder",
    "build_ledger",
    "load_opening_balances",
    "JournalBuilder",
    "TDSEngine",
    "GSTR1Builder",
]


def _fy_quarters(financial_year: str) -> dict[str, tuple[date, date]]:
    """
    Return the four quarter date ranges for an Indian financial year.

    "2025-26"  →
        Q1: 01-Apr-2025 … 30-Jun-2025
        Q2: 01-Jul-2025 … 30-Sep-2025
        Q3: 01-Oct-2025 … 31-Dec-2025
        Q4: 01-Jan-2026 … 31-Mar-2026
    """
    try:
        start_year = int(financial_year.split("-")[0])
    except (ValueError, IndexError):
        start_year = date.today().year

    end_year = start_year + 1
    return {
        "Q1": (date(start_year, 4,  1), date(start_year, 6,  30)),
        "Q2": (date(start_year, 7,  1), date(start_year, 9,  30)),
        "Q3": (date(start_year, 10, 1), date(start_year, 12, 31)),
        "Q4": (date(end_year,   1,  1), date(end_year,   3,  31)),
    }


def _deductee_type_from_str(raw: Optional[str]) -> DeducteeType:
    """Convert a string from the bill dict to a DeducteeType enum value."""
    if not raw:
        return DeducteeType.UNKNOWN

    normalised = raw.strip().lower()
    if "individual" in normalised or "huf" in normalised:
        return DeducteeType.INDIVIDUAL_HUF

    if "company" in normalised or "others" in normalised or "firm" in normalised:
        return DeducteeType.COMPANY_OTHERS

    return DeducteeType.UNKNOWN


def logger_print(msg: str) -> None:
    """Thin wrapper so TDS-level logs can be suppressed by setting LOG_TDS=0."""
    import os
    if os.environ.get("LOG_TDS", "1") != "0":
        print(msg)


def _entry_to_bill_stub(entry: JournalEntry) -> dict:
    """
    Construct a minimal bill-dict from a JournalEntry so TDSEngine.process_bill()
    has something to work with even when the original bill dict is unavailable.

    Only fields used by the TDS engine (vendor name, grand_total, narration)
    are populated.  GST components are left as 0 — so TDS base = grand total,
    which is the conservative (safe) fallback.
    """
    total_dr = sum(l.amount for l in entry.lines if l.dr_cr == DrCr.DEBIT)
    return {
        "_status":        "ok",
        "vendor_name":    entry.vendor_name,
        "invoice_number": entry.invoice_number,
        "invoice_date":   entry.date.strftime("%d-%m-%Y"),
        "grand_total":    total_dr,
        "taxable_amount": total_dr,    # conservative: no GST breakdown
        "cgst_amount":    0.0,
        "sgst_amount":    0.0,
        "igst_amount":    0.0,
        "narration":      entry.narration,
        "account_name":   entry.vendor_name,
    }


def _trial_balance_to_opening_format(tb: TrialBalance) -> list[dict]:
    """
    Convert a TrialBalance into the {"account", "balance", "side"} shape that
    opening_balances.load_opening_balances() expects under "opening_balances",
    so a period's closing position can be dropped straight into the next
    period's opening_balances.json without hand-editing.

    Zero-balance accounts are already excluded — LedgerBuilder.trial_balance()
    skips them when building tb.lines.
    """
    out: list[dict] = []
    for line in tb.lines:
        if line.closing_debit:
            out.append({"account": line.account, "balance": line.closing_debit, "side": "Dr"})
        elif line.closing_credit:
            out.append({"account": line.account, "balance": line.closing_credit, "side": "Cr"})
    return out


def build_complete_ledger(
    bills:                  list[dict],
    opening_balances_json:  Optional[Path | str] = None,
    as_on_date:             Optional[date]        = None,
    period_label:           str                   = "",
    financial_year:         str                   = "2025-26",
    deductor_tan:           Optional[str]         = None,
    deductor_name:          Optional[str]         = None,
    form_26q_quarters:      Optional[list[str]]   = None,
    close_books_on:         Optional[date]        = None,
) -> dict:
    """
    Complete pipeline: bills → journal entries → TDS layer → general ledger → reports.

    Parameters
    ----------
    bills
        List of bill dicts from the scanner.  Each dict may carry optional
        TDS metadata fields (see module docstring).
    opening_balances_json
        Path to opening_balances.json.  Loaded first so opening entries
        precede all transactions.
    as_on_date
        Date for trial balance / creditors-ageing computation (default: today).
    period_label
        Human-readable label for GST summary (e.g. "Apr-2025").
    financial_year
        Indian FY string "YYYY-YY" used for Form 26Q headers and quarter
        date ranges (default: "2025-26").
    deductor_tan
        TAN of the deducting business — printed on Form 26Q.
    deductor_name
        Legal name of the deducting entity — printed on Form 26Q.
    form_26q_quarters
        Which quarters to generate Form 26Q for.
        Default: all four ["Q1", "Q2", "Q3", "Q4"].
    close_books_on
        If provided, run year-end closing entries after posting all transactions.
        Should be the last day of the financial year, e.g. date(2026, 3, 31).
        The closing entries are posted to the GL and included in the audit trail.
        When not provided (default), books are left open (current behaviour).

    Returns
    -------
    dict with keys:
        "general_ledger"        GeneralLedger
        "trial_balance"         TrialBalance
        "gst_summary"           GSTSummary
        "cash_book"             list[CashBookLine]
        "creditors_ageing"      list[AgeingLine]
        "all_entries"           list[JournalEntry]  — full audit trail
        "period_start"          date | None
        "tds_engine"            TDSEngine           — for downstream mark_deposited()
        "tds_register"          TDSRegister         — full FY register
        "tds_warnings"          list[str]           — 206AA, threshold alerts, etc.
        "tds_pending_deposits"  list[dict]          — overdue TDS with due dates
        "form_26q"              dict[str, Form26Q]  — keyed by quarter "Q1"…"Q4"
        "closing_result"        ClosingResult | None — populated when close_books_on given
        "books_closed"          bool                — True if closing entries were generated
        "closing_balances"      list[dict]          — {"account","balance","side"} snapshot of
                                                        every non-zero account as_on_date; once
                                                        close_books_on has been posted, nominal
                                                        (P&L) accounts are already zeroed out by
                                                        journal.py's close_books(), so this is a
                                                        ready-made drop-in for next period's
                                                        opening_balances.json "opening_balances" list.
    """

    as_on_date         = as_on_date or date.today()
    form_26q_quarters  = form_26q_quarters or ["Q1", "Q2", "Q3", "Q4"]
    all_tds_warnings:  list[str] = []

    # Stage 1: Opening balances
    opening_entries: list[JournalEntry] = []
    period_start:    Optional[date]     = None

    if opening_balances_json:
        print(f"[pipeline] Loading opening balances from: {opening_balances_json}")
        opening_entries, period_start = load_opening_balances(opening_balances_json)
        print(f"[pipeline]   ✓ {len(opening_entries)} opening / manual entries loaded")


    # Stage 2: Raw journal entries from bills
    print(f"[pipeline] Building raw journal entries from {len(bills)} bills...")
    raw_entries: list[JournalEntry] = JournalBuilder.to_journal_entries(bills)
    print(f"[pipeline]   ✓ {len(raw_entries)} raw journal entries created")

    # Stage 3: TDS layer
    # The engine is stateful — keeps running aggregates for threshold tracking.
    print("[pipeline] Running TDS layer...")
    engine = TDSEngine(
        financial_year = financial_year,
        deductor_tan   = deductor_tan,
        deductor_name  = deductor_name,
    )

    # Build a lookup: vendor|invoice_number → bill dict (for TDS metadata)
    _bill_index: dict[str, dict] = {
        f"{b.get('vendor_name', '')}|{b.get('invoice_number', '')}": b
        for b in bills
        if b.get("_status") == "ok"
    }

    tds_modified_entries: list[JournalEntry] = []
    tds_skipped_count    = 0
    tds_applied_count    = 0

    for entry in raw_entries:
        # Only purchase / payment vouchers attract TDS deducted by us
        # (sales entries attract TDS receivable — handled separately)
        if entry.direction == "output":
            tds_modified_entries.append(entry)
            continue

        # Retrieve the source bill to read TDS metadata fields
        bill_key  = f"{entry.vendor_name}|{entry.invoice_number}"
        src_bill  = _bill_index.get(bill_key, {})

        # Pull TDS metadata from bill dict (all optional)
        tds_section    = src_bill.get("tds_section")        # explicit override
        deductee_pan   = src_bill.get("deductee_pan")
        deductee_type  = _deductee_type_from_str(src_bill.get("deductee_type"))
        deductee_gstin = src_bill.get("deductee_gstin")

        result = engine.process_bill(
            bill           = src_bill or _entry_to_bill_stub(entry),
            journal_entry  = entry,
            deductee_pan   = deductee_pan,
            deductee_type  = deductee_type,
            deductee_gstin = deductee_gstin,
            tds_section    = tds_section,
        )

        all_tds_warnings.extend(result.warnings)

        if result.tds_applied:
            tds_applied_count += 1
            tds_modified_entries.append(result.journal_entry)
            logger_print(
                f"[tds]   ✓ {entry.vendor_name} | "
                f"Inv {entry.invoice_number} | "
                f"₹{result.tds_entry.tds_amount:,.0f} @ "
                f"{result.tds_entry.tds_rate}% u/s "
                f"{result.tds_entry.section_code}"
            )
        else:
            tds_skipped_count += 1
            tds_modified_entries.append(entry)   # original unmodified entry

    print(
        f"[pipeline]   ✓ TDS applied on {tds_applied_count} entries, "
        f"skipped {tds_skipped_count} (no section detected or threshold not met)"
    )
    if all_tds_warnings:
        print(f"[pipeline]   ⚠ {len(all_tds_warnings)} TDS warning(s) — see result['tds_warnings']")

    # Stage 4: Post all entries to General Ledger
    close_label = period_label or financial_year
    if close_books_on:
        print(f"[pipeline] Posting entries to General Ledger (with year-end closing on {close_books_on})...")
    else:
        print("[pipeline] Posting entries to General Ledger...")

    ledger_builder, all_entries, closing_result = LedgerBuilder.build(
        bills             = [],                     # bills already converted above
        opening_balances  = opening_entries,
        _prebuilt_entries = tds_modified_entries,   # pass TDS-modified entries directly
        close_books_on    = close_books_on,
        period_label      = close_label,
    )
    # Reports (trial balance / creditors ageing) are wanted "as on" as_on_date,
    # which may differ from close_books_on — so point the builder at it explicitly.
    ledger_builder.as_on = as_on_date
    gl = ledger_builder.gl

    print(f"[pipeline]   ✓ {len(all_entries)} total entries posted")
    print(f"[pipeline]   ✓ {len(gl.accounts)} accounts in ledger")

    # Stage 4b: Log closing result
    if closing_result is not None:
        sign = "+" if closing_result.is_profit else "-"
        print(
            f"[pipeline]   ✓ Books closed — "
            f"Gross {'Profit' if closing_result.gross_profit >= 0 else 'Loss'}: "
            f"₹{abs(closing_result.gross_profit):,.2f} | "
            f"{closing_result.net_profit_label}: "
            f"₹{abs(closing_result.net_profit):,.2f} ({sign}) | "
            f"{len(closing_result.entries)} closing entries"
        )
        if closing_result.warnings:
            for w in closing_result.warnings:
                print(f"[pipeline]   ⚠ [closing] {w}")
    else:
        print("[pipeline]   ℹ Books NOT closed (pass close_books_on=date(YYYY,3,31) to close)")

    # Stage 5: Trial balance
    print("[pipeline] Generating trial balance...")
    tb = ledger_builder.trial_balance()
    status = "✓ BALANCED" if tb.is_balanced else f"⚠ OUT OF BALANCE by ₹{tb.difference}"
    print(f"[pipeline]   {status} | Dr ₹{tb.total_debit:,.2f} | Cr ₹{tb.total_credit:,.2f}")

    # Stage 5b: Closing balances (carry-forward snapshot)
    closing_balances = _trial_balance_to_opening_format(tb)
    if closing_result is not None:
        print(f"[pipeline]   ✓ Closing balances: {len(closing_balances)} accounts ready for carry-forward")

    # Stage 6: GST summary
    print("[pipeline] Computing GST summary...")
    gs = ledger_builder.gst_summary(period_label=period_label)
    print(
        f"[pipeline]   ✓ Output: CGST ₹{gs.output_cgst:,.2f} | "
        f"SGST ₹{gs.output_sgst:,.2f} | IGST ₹{gs.output_igst:,.2f}"
    )
    print(f"[pipeline]   ✓ Net GST payable: ₹{gs.net_total_payable:,.2f}")

    # Stage 7: Cash book & creditors ageing
    cb = ledger_builder.extract_cash_book()
    ca = ledger_builder.creditors_ageing()
    print(f"[pipeline]   ✓ Cash book: {len(cb)} postings | Creditors: {len(ca)} accounts")

    # Stage 8: TDS register & Form 26Q
    print("[pipeline] Building TDS register and Form 26Q...")
    tds_register = engine.get_register()

    quarters      = _fy_quarters(financial_year)
    form_26q_dict: dict[str, Form26Q] = {}
    for q in form_26q_quarters:
        if q not in quarters:
            print(f"[pipeline]   ⚠ Unknown quarter '{q}' — skipped")
            continue
        q_start, q_end = quarters[q]
        form_26q_dict[q] = engine.build_form_26q(q, q_start, q_end)

    pending = engine.pending_deposits(as_on=as_on_date)
    if pending:
        overdue = [p for p in pending if p["is_overdue"]]
        print(
            f"[pipeline]   ⚠ {len(pending)} TDS deposit(s) pending "
            f"({len(overdue)} overdue) — see result['tds_pending_deposits']"
        )
    else:
        print("[pipeline]   ✓ No pending TDS deposits")

    print(
        f"[pipeline]   ✓ TDS register: {len(tds_register.entries)} entries | "
        f"Deducted ₹{tds_register.total_tds_deducted:,.2f} | "
        f"Deposited ₹{tds_register.total_tds_deposited:,.2f} | "
        f"Pending ₹{tds_register.total_tds_pending:,.2f}"
    )

    if tds_register.missing_pan():
        print(
            f"[pipeline]   ⚠ {len(tds_register.missing_pan())} TDS entries without PAN "
            f"(Section 206AA rate applied)"
        )

    print("[pipeline] ✓ Complete.")

    return {
        # Core ledger outputs
        "general_ledger":       gl,
        "trial_balance":        tb,
        "gst_summary":          gs,
        "cash_book":            cb,
        "creditors_ageing":     ca,
        "all_entries":          all_entries,
        "period_start":         period_start,
        # TDS outputs
        "tds_engine":           engine,         # keep for mark_deposited() calls
        "tds_register":         tds_register,
        "tds_warnings":         all_tds_warnings,
        "tds_pending_deposits": pending,
        "form_26q":             form_26q_dict,
        # Year-end closing
        "closing_result":       closing_result,
        "books_closed":         closing_result is not None,
        "closing_balances":     closing_balances,
    }

