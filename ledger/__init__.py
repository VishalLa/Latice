"""
backend.ledger — Complete General Ledger Build Pipeline
========================================================

This module provides the complete workflow to build a general ledger from scanned bills
and optional opening balances.

Main entry point: build_complete_ledger()

Usage:
    from backend.ledger import build_complete_ledger
    
    result = build_complete_ledger(
        bills=[...],                              # from excel scanner
        opening_balances_json="opening_balances.json",  # optional
        as_on_date=None                           # optional, defaults to today
    )
    
    # Access results:
    gl = result['general_ledger']
    trial_bal = result['trial_balance']
    gst_summary = result['gst_summary']
    cash_book = result['cash_book']
    creditors_age = result['creditors_ageing']
    entries = result['all_entries']
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from .ledger import (
    GeneralLedger,
    build_ledger,
    trial_balance,
    gst_summary,
    extract_cash_book,
    creditors_ageing,
)
from .opening_balances import load_opening_balances
from .journal import to_journal_entries
from schema import JournalEntry


def build_complete_ledger(
    bills: list[dict],
    opening_balances_json: Optional[Path | str] = None,
    as_on_date: Optional[date] = None,
    period_label: str = "",
) -> dict:
    """
    Complete pipeline to build a general ledger from bills and optional opening balances.
    
    Args:
        bills                  : list of bill dicts from excel.scan_all() or similar
        opening_balances_json  : optional path to opening_balances.json file.
                                If provided, loads opening balances and manual entries.
        as_on_date            : date for trial balance (default: today)
        period_label          : label for GST summary reporting (e.g., "Apr-2025")
    
    Returns:
        dict with keys:
            'general_ledger'    : GeneralLedger object (populated)
            'trial_balance'     : TrialBalance object
            'gst_summary'       : GSTSummary object
            'cash_book'         : list of CashBookLine objects
            'creditors_ageing'  : list of AgeingLine objects
            'all_entries'       : all JournalEntry objects in date order (audit trail)
            'period_start'      : date of period start (from opening_balances.json, if provided)
    
    Example:
        >>> result = build_complete_ledger(
        ...     bills=scanned_bills,
        ...     opening_balances_json="opening_balances.json"
        ... )
        >>> print(f"Trial Balance Dr: ₹{result['trial_balance'].total_debit}")
        >>> print(f"GST Payable: ₹{result['gst_summary'].net_total_payable}")
    """
    as_on_date = as_on_date or date.today()
    
    # Step 1: Load opening balances (if provided)
    opening_entries: list[JournalEntry] = []
    period_start: Optional[date] = None
    
    if opening_balances_json:
        print(f"Loading opening balances from: {opening_balances_json}")
        opening_entries, period_start = load_opening_balances(opening_balances_json)
        print(f"  ✓ Loaded {len(opening_entries)} opening/manual entries")
    
    # Step 2: Build ledger (posts opening entries + transaction entries)
    print(f"Building general ledger from {len(bills)} bills...")
    gl, all_entries = build_ledger(bills, opening_balances=opening_entries)
    print(f"  ✓ Posted {len(all_entries)} total entries")
    print(f"  ✓ {len(gl.accounts)} accounts created")
    
    # Step 3: Generate trial balance
    print("Generating trial balance...")
    tb = trial_balance(gl, as_on=as_on_date)
    print(f"  ✓ Total Debit:  ₹{tb.total_debit:,.2f}")
    print(f"  ✓ Total Credit: ₹{tb.total_credit:,.2f}")
    
    # Step 4: Generate GST summary
    print("Computing GST summary...")
    gs = gst_summary(gl, period_label=period_label)
    print(f"  ✓ Output CGST: ₹{gs.output_cgst:,.2f} | ITC: ₹{gs.input_cgst:,.2f}")
    print(f"  ✓ Output SGST: ₹{gs.output_sgst:,.2f} | ITC: ₹{gs.input_sgst:,.2f}")
    print(f"  ✓ Output IGST: ₹{gs.output_igst:,.2f} | ITC: ₹{gs.input_igst:,.2f}")
    print(f"  ✓ Net GST Payable: ₹{gs.net_total_payable:,.2f}")
    
    # Step 5: Extract cash book
    print("Extracting cash book...")
    cb = extract_cash_book(gl)
    print(f"  ✓ {len(cb)} cash/bank postings")
    
    # Step 6: Creditors ageing
    print("Computing creditors ageing...")
    ca = creditors_ageing(gl, as_on=as_on_date)
    print(f"  ✓ {len(ca)} creditor accounts")
    
    return {
        'general_ledger': gl,
        'trial_balance': tb,
        'gst_summary': gs,
        'cash_book': cb,
        'creditors_ageing': ca,
        'all_entries': all_entries,
        'period_start': period_start,
    }


# Export all public APIs
__all__ = [
    'build_complete_ledger',
    'GeneralLedger',
    'build_ledger',
    'trial_balance',
    'gst_summary',
    'extract_cash_book',
    'creditors_ageing',
    'load_opening_balances',
]
