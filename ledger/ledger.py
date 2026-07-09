from __future__ import annotations

from datetime import date
from typing import Optional

from schema import (
    TrialBalance, 
    TrialBalanceLine, 
    GSTSummary, 
    CashBookLine, 
    AgeingLine, 
    LedgerPosting, 
    JournalEntry, 
    AccountGroup, 
    COA
)
from .journal import to_journal_entries, close_books, ClosingResult
from .general_ledger_class import GeneralLedger
    

def trial_balance(gl: GeneralLedger, as_on: Optional[date] = None) -> TrialBalance:
    as_on = as_on or date.today()
    lines = []

    for ledger_acc in gl.accounts:
        if not ledger_acc.postings:
            continue
        bal_amt, bal_side = ledger_acc.closing_balance
        if bal_amt == 0:
            continue
        line = TrialBalanceLine(
            account        = ledger_acc.name,
            group          = ledger_acc.group.value,
            closing_debit  = bal_amt if bal_side == "Dr" else 0.0,
            closing_credit = bal_amt if bal_side == "Cr" else 0.0,
        )
        lines.append(line)

    total_dr = round(sum(l.closing_debit  for l in lines), 2)
    total_cr = round(sum(l.closing_credit for l in lines), 2)

    return TrialBalance(
        lines        = lines,
        as_on        = as_on,
        total_debit  = total_dr,
        total_credit = total_cr,
    )



def gst_summary(gl: GeneralLedger, period_label: str = "") -> GSTSummary:
    """
    Compute GST liability position from the General Ledger.
    Applies ITC set-off rules per GST law:
      IGST ITC → first IGST, then CGST, then SGST
      CGST ITC → CGST only
      SGST ITC → SGST only
    """
    def _bal(account_name: str) -> float:
        acc = gl.get(account_name)
        if not acc:
            return 0.0
        amt, _ = acc.closing_balance
        return amt

    out_cgst = _bal(COA.OUTPUT_CGST.name)
    out_sgst = _bal(COA.OUTPUT_SGST.name)
    out_igst = _bal(COA.OUTPUT_IGST.name)
    out_cess = _bal(COA.OUTPUT_CESS.name)

    in_cgst  = _bal(COA.INPUT_CGST.name)
    in_sgst  = _bal(COA.INPUT_SGST.name)
    in_igst  = _bal(COA.INPUT_IGST.name)
    in_cess  = _bal(COA.INPUT_CESS.name)

    # ITC utilisation per GST cross-utilisation rules 

    remaining_igst_itc = in_igst
    remaining_cgst_itc = in_cgst
    remaining_sgst_itc = in_sgst

    # Step 1: IGST ITC vs IGST liability
    igst_itc_used = min(remaining_igst_itc, out_igst)
    remaining_igst_itc -= igst_itc_used
    net_igst = round(out_igst - igst_itc_used, 2)

    # Step 2: Remaining IGST ITC vs CGST liability
    igst_vs_cgst = min(remaining_igst_itc, net_igst if net_igst > 0 else out_cgst)
    igst_vs_cgst = min(remaining_igst_itc, out_cgst)
    remaining_igst_itc -= igst_vs_cgst
    cgst_after_igst_itc = round(out_cgst - igst_vs_cgst, 2)

    # Step 3: Remaining IGST ITC vs SGST liability
    igst_vs_sgst = min(remaining_igst_itc, out_sgst)
    remaining_igst_itc -= igst_vs_sgst
    sgst_after_igst_itc = round(out_sgst - igst_vs_sgst, 2)

    # Step 4: CGST ITC vs remaining CGST liability
    cgst_itc_used = min(remaining_cgst_itc, cgst_after_igst_itc)
    remaining_cgst_itc -= cgst_itc_used
    net_cgst = round(cgst_after_igst_itc - cgst_itc_used, 2)

    # Step 5: SGST ITC vs remaining SGST liability
    sgst_itc_used = min(remaining_sgst_itc, sgst_after_igst_itc)
    remaining_sgst_itc -= sgst_itc_used
    net_sgst = round(sgst_after_igst_itc - sgst_itc_used, 2)

    total_itc_used = round(igst_itc_used + igst_vs_cgst + igst_vs_sgst + cgst_itc_used + sgst_itc_used, 2)
    net_payable    = round(max(net_cgst, 0) + max(net_sgst, 0) + max(net_igst, 0), 2)
    itc_carry      = round(remaining_cgst_itc + remaining_sgst_itc + remaining_igst_itc, 2)

    return GSTSummary(
        period_label     = period_label,
        output_cgst      = out_cgst,
        output_sgst      = out_sgst,
        output_igst      = out_igst,
        output_cess      = out_cess,
        input_cgst       = in_cgst,
        input_sgst       = in_sgst,
        input_igst       = in_igst,
        input_cess       = in_cess,
        cgst_itc_used    = cgst_itc_used,
        sgst_itc_used    = sgst_itc_used,
        igst_itc_used    = igst_itc_used,
        net_cgst_payable = max(net_cgst, 0),
        net_sgst_payable = max(net_sgst, 0),
        net_igst_payable = max(net_igst, 0),
        net_total_payable= net_payable,
        itc_carry_forward= itc_carry,
    )


def extract_cash_book(gl: GeneralLedger) -> list[CashBookLine]:
    cash_acc = gl.get(COA.CASH.name)
    bank_acc = gl.get(COA.BANK.name)

    combined: list[tuple[date, LedgerPosting, str]] = []
    if cash_acc:
        for p in cash_acc.postings:
            combined.append((p.date, p, "Cash"))
    if bank_acc:
        for p in bank_acc.postings:
            combined.append((p.date, p, "Bank"))

    combined.sort(key=lambda x: x[0])

    lines   = []
    balance = 0.0
    for _, posting, book in combined:
        receipts = posting.dr_amount   # Dr to cash = receipt
        payments = posting.cr_amount   # Cr to cash = payment
        balance  = round(balance + receipts - payments, 2)
        lines.append(CashBookLine(
            date         = posting.date,
            particulars  = posting.particulars,
            voucher_type = posting.voucher_type,
            journal_id   = posting.journal_id,
            account_type = book,
            receipts     = receipts,
            payments     = payments,
            balance      = balance,
        ))

    return lines


def creditors_ageing(gl: GeneralLedger, as_on: Optional[date] = None) -> list[AgeingLine]:
    as_on = as_on or date.today()
    result = []

    for acc in gl.accounts_in_group(AccountGroup.SUNDRY_CREDITORS):
        bal_amt, bal_side = acc.closing_balance
        if bal_amt == 0:
            continue

        # Group postings by age
        current = days_30_60 = days_60_90 = over_90 = 0.0
        for posting in acc.postings:
            days = (as_on - posting.date).days
            amt  = posting.cr_amount - posting.dr_amount  # Net outstanding per posting
            if days <= 30:
                current    += amt
            elif days <= 60:
                days_30_60 += amt
            elif days <= 90:
                days_60_90 += amt
            else:
                over_90    += amt

        result.append(AgeingLine(
            account      = acc.name,
            balance      = bal_amt,
            balance_side = bal_side,
            current      = max(current, 0),
            days_30_60   = max(days_30_60, 0),
            days_60_90   = max(days_60_90, 0),
            over_90      = max(over_90, 0),
        ))

    return sorted(result, key=lambda x: x.balance, reverse=True)


def build_ledger(
    bills:              list[dict],
    opening_balances:   Optional[list[JournalEntry]] = None,
    _prebuilt_entries:  Optional[list[JournalEntry]] = None,
    close_books_on:     Optional[date]               = None,
    period_label:       str                          = "",
) -> tuple[GeneralLedger, list[JournalEntry], Optional[ClosingResult]]:
    """
    Full pipeline: bills → journal entries → general ledger → (optional) closing.

    Args:
        bills             : list of bill dicts from excel.scan_all()
        opening_balances  : optional list of JournalEntry objects for opening balances
                            (from opening_balances.py). Posted first.
        _prebuilt_entries : TDS-modified entries from __init__.py pipeline.
                            When supplied, bills are ignored (already converted).
        close_books_on    : If provided, run close_books() after posting all entries
                            and also post the closing entries to the GL.
                            Should be the last date of the financial year,
                            e.g. date(2026, 3, 31).
        period_label      : Human-readable FY label for closing entry narrations,
                            e.g. "FY 2025-26".

    Returns:
        gl              : the populated GeneralLedger (includes closing entries if requested)
        all_entries     : all JournalEntry objects in date order (audit trail)
        closing_result  : ClosingResult if close_books_on was supplied, else None.
    """
    gl          = GeneralLedger()
    all_entries : list[JournalEntry] = []

    # Post opening balances first (carry the date of period start)
    if opening_balances:
        gl.post_entries(opening_balances)
        all_entries.extend(opening_balances)

    # Use pre-built (TDS-modified) entries if available, otherwise convert bills
    if _prebuilt_entries is not None:
        tx_entries = _prebuilt_entries
    else:
        tx_entries = to_journal_entries(bills)

    gl.post_entries(tx_entries)
    all_entries.extend(tx_entries)

    # Sort full audit trail by date (before closing entries)
    all_entries.sort(key=lambda e: e.date)

    # Optional year-end closing
    closing_result: Optional[ClosingResult] = None
    if close_books_on is not None:
        closing_result = close_books(gl, close_books_on, period_label)
        if closing_result.entries:
            gl.post_entries(closing_result.entries)
            all_entries.extend(closing_result.entries)
            # Closing entries are dated period_end so they naturally sort last

    return gl, all_entries, closing_result
