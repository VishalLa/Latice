"""
journal.py — Book of Original Entry (Journal)
Implements double-entry bookkeeping as per:
- Indian Accounting Standards (AS / Ind AS)
- Companies Act 2013 — Section 128 (Books of Accounts)
- GST Act 2017 — Input Tax Credit rules

Every financial transaction is recorded as one or more JournalEntry objects,
where total debits always equal total credits (fundamental rule of double-entry).

Chart of Accounts follows the standard Indian format used in Tally ERP:
- Capital Account
- Loans (Liabilities)
- Current Liabilities
- Fixed Assets
- Current Assets
- Purchase Accounts
- Sales Accounts
- Direct Expenses / Indirect Expenses
- Direct Income / Indirect Income
- Duties & Taxes (for GST)
"""

from __future__ import annotations

from datetime import datetime, date
from typing import Optional
from dateutil import parser as dateparser

from schema import (
    DrCr, 
    AccountGroup, 
    Account, 
    COA, 
    EntryLine, 
    JournalEntry
)


# DATE NORMALISATION
def parse_indian_date(raw: Optional[str]) -> date:
    if not raw:
        return date.today()
    try:
        return dateparser.parse(raw, dayfirst=True).date()
    except Exception:
        return date.today()


# JOURNAL ENTRY GENERATORS
def _safe(val, default=0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
    

def _purchase_entry(bill: dict) -> JournalEntry:
    """
    Generate journal entry for a PURCHASE (input bill).
    Standard Indian purchase entry with GST:
        Purchase A/c                Dr.   [taxable amount]
        Input CGST A/c              Dr.   [cgst amount]     ← ITC asset
        Input SGST A/c              Dr.   [sgst amount]     ← ITC asset
        Input IGST A/c              Dr.   [igst amount]     ← ITC asset
        Input GST Cess A/c          Dr.   [cess amount]
        Freight Inward A/c          Dr.   [freight/other charges]
        Round Off A/c               Dr.   [round off, if any]
          To <Vendor Name> A/c          Cr.   [grand total]
          To Discount Received A/c      Cr.   [discount, if any]
    If the bill is already paid (Cash/UPI/Card), the credit goes to
    Cash A/c or Bank A/c instead of Sundry Creditors.
    """
    taxable     = _safe(bill.get("taxable_amount") or bill.get("subtotal"))
    cgst        = _safe(bill.get("cgst_amount"))
    sgst        = _safe(bill.get("sgst_amount"))
    igst        = _safe(bill.get("igst_amount"))
    cess        = _safe(bill.get("cess_amount"))
    other_chg   = _safe(bill.get("other_charges"))
    round_off   = _safe(bill.get("round_off"))
    discount    = _safe(bill.get("discount"))
    grand       = _safe(bill.get("grand_total"))
    vendor      = bill.get("vendor_name") or "Unknown Vendor"
    pay_mode    = bill.get("payment_mode")
    inv_no      = bill.get("invoice_number") or "N/A"
    entry_date  = parse_indian_date(bill.get("invoice_date"))

# If taxable is missing, back-calculate from grand and tax
    if taxable == 0 and grand > 0:
        taxable = round(grand - cgst - sgst - igst - cess - other_chg, 2)
        taxable = max(taxable, 0)

    lines: list[EntryLine] = []

# Debit lines 
    if taxable > 0:
        lines.append(EntryLine(COA.PURCHASES, DrCr.DEBIT, taxable, f"Purchases from {vendor}"))
    if cgst > 0:
        lines.append(EntryLine(COA.INPUT_CGST, DrCr.DEBIT, cgst, f"Input CGST on purchases — Inv {inv_no}"))
    if sgst > 0:
        lines.append(EntryLine(COA.INPUT_SGST, DrCr.DEBIT, sgst, f"Input SGST on purchases — Inv {inv_no}"))
    if igst > 0:
        lines.append(EntryLine(COA.INPUT_IGST, DrCr.DEBIT, igst, f"Input IGST on purchases — Inv {inv_no}"))
    if cess > 0:
        lines.append(EntryLine(COA.INPUT_CESS, DrCr.DEBIT, cess, f"Input GST Cess — Inv {inv_no}"))
    if other_chg > 0:
        lines.append(EntryLine(COA.OTHER_CHARGES, DrCr.DEBIT, other_chg, "Other charges / freight"))
    if round_off != 0:
        # Round off can be positive or negative on invoices
        lines.append(EntryLine(COA.ROUND_OFF, DrCr.DEBIT,  abs(round_off), "Round off"))

    # Credit lines (If paid immediately → Cash / Bank, else → Sundry Creditor)
    if pay_mode:
        credit_account = COA.payment_account(pay_mode)
        voucher_type   = "Payment Voucher"
    else:
        credit_account = COA.creditor_for(vendor)
        voucher_type   = "Purchase Voucher"

    credit_amount = grand if grand > 0 else (taxable + cgst + sgst + igst + cess + other_chg)

    if discount > 0:
        credit_amount = round(credit_amount - discount, 2)
        lines.append(EntryLine(COA.DISCOUNT_RECV, DrCr.CREDIT, discount, "Discount received from vendor"))

    if credit_amount > 0:
        lines.append(EntryLine(credit_account, DrCr.CREDIT, credit_amount, f"Being purchase from {vendor} vide Inv {inv_no}"))

    # Balance check & fix micro-rounding
    total_dr = round(sum(l.amount for l in lines if l.dr_cr == DrCr.DEBIT),  2)
    total_cr = round(sum(l.amount for l in lines if l.dr_cr == DrCr.CREDIT), 2)
    diff = round(total_dr - total_cr, 2)

    if diff != 0 and abs(diff) <= 1.0:
        # Absorb into round-off
        existing_roff = next((l for l in lines if l.account == COA.ROUND_OFF), None)
        if existing_roff:
            lines.remove(existing_roff)
            new_amt = round(existing_roff.amount + abs(diff), 2)
            if new_amt > 0:
                lines.append(EntryLine(COA.ROUND_OFF, existing_roff.dr_cr, new_amt, "Round off (adjusted)"))
        else:
            if diff > 0:
                lines.append(EntryLine(COA.ROUND_OFF, DrCr.CREDIT, diff, "Round off"))
            else:
                lines.append(EntryLine(COA.ROUND_OFF, DrCr.DEBIT, abs(diff), "Round off"))

    narration = (
        f"Being purchase of goods/services from {vendor}, "
        f"Invoice No. {inv_no}, "
        f"dated {entry_date.strftime('%d-%m-%Y')}"
    )

    return JournalEntry(
        date         = entry_date,
        voucher_type = voucher_type,
        narration    = narration,
        lines        = lines,
        source_file  = bill.get("_source_file", ""),
        invoice_number = inv_no,
        vendor_name  = vendor,
        direction    = "input",
    )


def _sales_entry(bill: dict) -> JournalEntry:
    """
    Generate journal entry for a SALE (output bill).
    Standard Indian sales entry with GST:
        <Buyer Name> A/c / Cash A/c / Bank A/c   Dr.   [grand total]
          To Sales A/c                                Cr.   [taxable amount]
          To Output CGST A/c                          Cr.   [cgst]  ← liability
          To Output SGST A/c                          Cr.   [sgst]  ← liability
          To Output IGST A/c                          Cr.   [igst]  ← liability
          To Output GST Cess A/c                      Cr.   [cess]
          To Discount Allowed A/c (if Dr side)
    Output GST is a liability — we collect it from the buyer and pay it to govt.
    """
    taxable     = _safe(bill.get("taxable_amount") or bill.get("subtotal"))
    cgst        = _safe(bill.get("cgst_amount"))
    sgst        = _safe(bill.get("sgst_amount"))
    igst        = _safe(bill.get("igst_amount"))
    cess        = _safe(bill.get("cess_amount"))
    other_chg   = _safe(bill.get("other_charges"))
    round_off   = _safe(bill.get("round_off"))
    discount    = _safe(bill.get("discount"))
    grand       = _safe(bill.get("grand_total"))
    buyer       = bill.get("buyer_name") or "Cash Customer"
    vendor      = bill.get("vendor_name") or ""
    pay_mode    = bill.get("payment_mode")
    inv_no      = bill.get("invoice_number") or "N/A"
    entry_date  = parse_indian_date(bill.get("invoice_date"))

    if taxable == 0 and grand > 0:
        taxable = round(grand - cgst - sgst - igst - cess - other_chg, 2)
        taxable = max(taxable, 0)

    lines: list[EntryLine] = []

    # Debit lines (For retail/POS bills buyer is often "Cash Customer" → debit Cash/Bank)
    if pay_mode:
        debit_account = COA.payment_account(pay_mode)
        voucher_type  = "Receipt Voucher"
    elif buyer.lower() in {"cash customer", "retail customer", "walk-in", "consumer"}:
        debit_account = COA.CASH
        voucher_type  = "Sales Voucher"
    else:
        debit_account = COA.debtor_for(buyer)
        voucher_type  = "Sales Voucher"

    debit_amount = grand if grand > 0 else (taxable + cgst + sgst + igst + cess + other_chg)

    if discount > 0:
    # Discount reduces what debtor pays, but we record the full sale
        lines.append(EntryLine(COA.DISCOUNT_GIVEN, DrCr.DEBIT, discount, "Discount allowed to customer"))
        debit_amount = round(debit_amount - discount, 2)

    if debit_amount > 0:
        lines.append(EntryLine(debit_account, DrCr.DEBIT, debit_amount, f"Being sale to {buyer}"))

    # Credit lines 
    if taxable > 0:
        lines.append(EntryLine(COA.SALES, DrCr.CREDIT, taxable, f"Sales — Inv {inv_no}"))
    if cgst > 0:
        lines.append(EntryLine(COA.OUTPUT_CGST, DrCr.CREDIT, cgst, f"Output CGST collected — Inv {inv_no}"))
    if sgst > 0:
        lines.append(EntryLine(COA.OUTPUT_SGST, DrCr.CREDIT, sgst, f"Output SGST collected — Inv {inv_no}"))
    if igst > 0:
        lines.append(EntryLine(COA.OUTPUT_IGST, DrCr.CREDIT, igst, f"Output IGST collected — Inv {inv_no}"))
    if cess > 0:
        lines.append(EntryLine(COA.OUTPUT_CESS, DrCr.CREDIT, cess, f"Output GST Cess — Inv {inv_no}"))
    if other_chg > 0:
        lines.append(EntryLine(COA.OTHER_CHARGES, DrCr.CREDIT, other_chg, "Other charges billed"))
    if round_off != 0:
        lines.append(EntryLine(COA.ROUND_OFF, DrCr.CREDIT, abs(round_off), "Round off"))

    # Balance micro-rounding fix
    total_dr = round(sum(l.amount for l in lines if l.dr_cr == DrCr.DEBIT),  2)
    total_cr = round(sum(l.amount for l in lines if l.dr_cr == DrCr.CREDIT), 2)
    diff = round(total_dr - total_cr, 2)

    if diff != 0 and abs(diff) <= 1.0:
        if diff > 0:
            lines.append(EntryLine(COA.ROUND_OFF, DrCr.CREDIT, diff, "Round off"))
        else:
            lines.append(EntryLine(COA.ROUND_OFF, DrCr.DEBIT, abs(diff), "Round off"))

    narration = (
        f"Being sale of goods/services to {buyer}, "
        f"Invoice No. {inv_no}, "
        f"dated {entry_date.strftime('%d-%m-%Y')}"
    )

    return JournalEntry(
        date           = entry_date,
        voucher_type   = voucher_type,
        narration      = narration,
        lines          = lines,
        source_file    = bill.get("_source_file", ""),
        invoice_number = inv_no,
        vendor_name    = vendor or buyer,
        direction      = "output",
    )


def _purchase_return_entry(bill: dict) -> JournalEntry:
    """
    Generate journal entry for a PURCHASE RETURN / DEBIT NOTE.
    Reverse the purchase and input GST amounts, reducing the creditor or bank/cash liability.
    """
    taxable   = _safe(bill.get("taxable_amount") or bill.get("subtotal"))
    cgst      = _safe(bill.get("cgst_amount"))
    sgst      = _safe(bill.get("sgst_amount"))
    igst      = _safe(bill.get("igst_amount"))
    cess      = _safe(bill.get("cess_amount"))
    other_chg = _safe(bill.get("other_charges"))
    round_off = _safe(bill.get("round_off"))
    discount  = _safe(bill.get("discount"))
    grand     = _safe(bill.get("grand_total"))
    vendor    = bill.get("vendor_name") or "Unknown Vendor"
    pay_mode  = bill.get("payment_mode")
    inv_no    = bill.get("invoice_number") or "N/A"
    entry_date = parse_indian_date(bill.get("invoice_date"))

    if taxable == 0 and grand > 0:
        taxable = round(grand - cgst - sgst - igst - cess - other_chg, 2)
        taxable = max(taxable, 0)

    lines: list[EntryLine] = []

    # Credit lines (reduce purchase expense and input GST assets)
    if taxable > 0:
        lines.append(EntryLine(COA.PURCHASES, DrCr.CREDIT, taxable, f"Purchase return from {vendor} — Inv {inv_no}"))
    if cgst > 0:
        lines.append(EntryLine(COA.INPUT_CGST, DrCr.CREDIT, cgst, f"Input CGST reversed on return — Inv {inv_no}"))
    if sgst > 0:
        lines.append(EntryLine(COA.INPUT_SGST, DrCr.CREDIT, sgst, f"Input SGST reversed on return — Inv {inv_no}"))
    if igst > 0:
        lines.append(EntryLine(COA.INPUT_IGST, DrCr.CREDIT, igst, f"Input IGST reversed on return — Inv {inv_no}"))
    if cess > 0:
        lines.append(EntryLine(COA.INPUT_CESS, DrCr.CREDIT, cess, f"Input GST Cess reversed — Inv {inv_no}"))
    if other_chg > 0:
        lines.append(EntryLine(COA.OTHER_CHARGES, DrCr.CREDIT, other_chg, "Other charges reversed on purchase return"))
    if round_off != 0:
        lines.append(EntryLine(COA.ROUND_OFF, DrCr.CREDIT, abs(round_off), "Round off reversed on purchase return"))
    if discount > 0:
        lines.append(EntryLine(COA.DISCOUNT_RECV, DrCr.DEBIT, discount, "Discount received reversed on purchase return"))

    if pay_mode:
        debit_account = COA.payment_account(pay_mode)
        voucher_type = "Payment Voucher"
    else:
        debit_account = COA.creditor_for(vendor)
        voucher_type = "Purchase Return Voucher"

    debit_amount = grand if grand > 0 else (taxable + cgst + sgst + igst + cess + other_chg)
    if discount > 0:
        debit_amount = round(debit_amount - discount, 2)

    if debit_amount > 0:
        lines.append(EntryLine(debit_account, DrCr.DEBIT, debit_amount, f"Being purchase return to {vendor} — Inv {inv_no}"))

    return JournalEntry(
        date           = entry_date,
        voucher_type   = voucher_type,
        narration      = (
            f"Being purchase return / debit note from {vendor}, "
            f"Invoice No. {inv_no}, dated {entry_date.strftime('%d-%m-%Y')}"
        ),
        lines          = lines,
        source_file    = bill.get("_source_file", ""),
        invoice_number = inv_no,
        vendor_name    = vendor,
        direction      = "input",
    )


def _sales_return_entry(bill: dict) -> JournalEntry:
    """
    Generate journal entry for a SALES RETURN / CREDIT NOTE.
    Reverse the sales and output GST liability, reducing the debtor or bank/cash asset.
    """
    taxable   = _safe(bill.get("taxable_amount") or bill.get("subtotal"))
    cgst      = _safe(bill.get("cgst_amount"))
    sgst      = _safe(bill.get("sgst_amount"))
    igst      = _safe(bill.get("igst_amount"))
    cess      = _safe(bill.get("cess_amount"))
    other_chg = _safe(bill.get("other_charges"))
    round_off = _safe(bill.get("round_off"))
    discount  = _safe(bill.get("discount"))
    grand     = _safe(bill.get("grand_total"))
    buyer     = bill.get("buyer_name") or "Customer"
    vendor    = bill.get("vendor_name") or buyer
    pay_mode  = bill.get("payment_mode")
    inv_no    = bill.get("invoice_number") or "N/A"
    entry_date = parse_indian_date(bill.get("invoice_date"))

    if taxable == 0 and grand > 0:
        taxable = round(grand - cgst - sgst - igst - cess - other_chg, 2)
        taxable = max(taxable, 0)

    lines: list[EntryLine] = []

    if pay_mode:
        credit_account = COA.payment_account(pay_mode)
        voucher_type = "Receipt Voucher"
    else:
        credit_account = COA.debtor_for(buyer)
        voucher_type = "Sales Return Voucher"

    credit_amount = grand if grand > 0 else (taxable + cgst + sgst + igst + cess + other_chg)
    if discount > 0:
        credit_amount = round(credit_amount - discount, 2)

    if credit_amount > 0:
        lines.append(EntryLine(credit_account, DrCr.CREDIT, credit_amount, f"Being sales return credited by {buyer} — Inv {inv_no}"))

    if taxable > 0:
        lines.append(EntryLine(COA.SALES, DrCr.DEBIT, taxable, f"Sales return — Inv {inv_no}"))
    if cgst > 0:
        lines.append(EntryLine(COA.OUTPUT_CGST, DrCr.DEBIT, cgst, f"Output CGST reversed on credit note — Inv {inv_no}"))
    if sgst > 0:
        lines.append(EntryLine(COA.OUTPUT_SGST, DrCr.DEBIT, sgst, f"Output SGST reversed on credit note — Inv {inv_no}"))
    if igst > 0:
        lines.append(EntryLine(COA.OUTPUT_IGST, DrCr.DEBIT, igst, f"Output IGST reversed on credit note — Inv {inv_no}"))
    if cess > 0:
        lines.append(EntryLine(COA.OUTPUT_CESS, DrCr.DEBIT, cess, f"Output GST Cess reversed on credit note — Inv {inv_no}"))
    if other_chg > 0:
        lines.append(EntryLine(COA.OTHER_CHARGES, DrCr.DEBIT, other_chg, "Other charges reversed on sales return"))
    if round_off != 0:
        lines.append(EntryLine(COA.ROUND_OFF, DrCr.DEBIT, abs(round_off), "Round off reversed on sales return"))
    if discount > 0:
        lines.append(EntryLine(COA.DISCOUNT_GIVEN, DrCr.CREDIT, discount, "Discount allowed reversed on sales return"))

    return JournalEntry(
        date           = entry_date,
        voucher_type   = voucher_type,
        narration      = (
            f"Being sales return / credit note to {buyer}, "
            f"Invoice No. {inv_no}, dated {entry_date.strftime('%d-%m-%Y')}"
        ),
        lines          = lines,
        source_file    = bill.get("_source_file", ""),
        invoice_number = inv_no,
        vendor_name    = vendor,
        direction      = "output",
    )


def to_journal_entry(bill: dict) -> Optional[JournalEntry]:

    if bill.get("_status") != "ok":
        return None

    grand = _safe(bill.get("grand_total"))
    if grand <= 0:
        return None

    direction = bill.get("_direction", "input")
    return_type = bill.get("return_type")
    
    try:
        if return_type == 'credit_note':
            return _sales_return_entry(bill)
        if return_type == 'debit_note':
            return _purchase_return_entry(bill)
        if direction == "output":
            return _sales_entry(bill)
        else:
            return _purchase_entry(bill)
    except Exception as e:
        # Log but don't crash — return None so caller can skip
        print(f"  [journal] WARNING: Could not create entry for {bill.get('_source_file', '?')}: {e}")
        return None
    

def to_journal_entries(bills: list[dict]) -> list[JournalEntry]:
    entries = []
    for bill in bills:
        entry = to_journal_entry(bill)
        if entry:
            entries.append(entry)
    return sorted(entries, key=lambda e: e.date)


def gst_settlement_entry(
    input_cgst:  float,
    input_sgst:  float,
    input_igst:  float,
    output_cgst: float,
    output_sgst: float,
    output_igst: float,
    period_label: str = "",
) -> list[JournalEntry]:
    """
    Generate the GST ITC set-off and payment journal entries.
    Per GSTIN rules:
      1. IGST ITC is first used to offset IGST liability, then CGST, then SGST.
      2. CGST ITC offsets CGST liability only.
      3. SGST ITC offsets SGST liability only.
      4. Remaining liability → GST Payable A/c (to be paid via PMT-06/challan).
    Returns a list of JournalEntries (typically 1-2 entries).
    """
    entries = []
    today   = date.today()
    lines   = []

    cgst_offset = min(input_cgst, output_cgst)
    if cgst_offset > 0:
        lines.append(EntryLine(COA.OUTPUT_CGST, DrCr.DEBIT,  cgst_offset, "CGST ITC set-off against output CGST liability"))
        lines.append(EntryLine(COA.INPUT_CGST,  DrCr.CREDIT, cgst_offset, "Input CGST ITC utilised"))

    # Step 2:Offset Output SGST with Input SGST 
    sgst_offset = min(input_sgst, output_sgst)
    if sgst_offset > 0:
        lines.append(EntryLine(COA.OUTPUT_SGST, DrCr.DEBIT,  sgst_offset, "SGST ITC set-off against output SGST liability"))
        lines.append(EntryLine(COA.INPUT_SGST,  DrCr.CREDIT, sgst_offset, "Input SGST ITC utilised"))

    # Step 3:Offset Output IGST with Input IGST 
    igst_offset = min(input_igst, output_igst)
    if igst_offset > 0:
        lines.append(EntryLine(COA.OUTPUT_IGST, DrCr.DEBIT,  igst_offset, "IGST ITC set-off against output IGST liability"))
        lines.append(EntryLine(COA.INPUT_IGST,  DrCr.CREDIT, igst_offset, "Input IGST ITC utilised"))

    if lines:
        entries.append(JournalEntry(
            date         = today,
            voucher_type = "Journal Voucher",
            narration    = f"GST ITC set-off — {period_label}",
            lines        = lines,
        ))

    # Step 4:Remaining net GST payable 
    net_cgst = round(output_cgst - cgst_offset, 2)
    net_sgst = round(output_sgst - sgst_offset, 2)
    net_igst = round(output_igst - igst_offset, 2)
    net_payable = round(net_cgst + net_sgst + net_igst, 2)

    if net_payable > 0:
        pay_lines = []
        if net_cgst > 0:
            pay_lines.append(EntryLine(COA.OUTPUT_CGST, DrCr.DEBIT, net_cgst, "Net CGST payable to govt"))
        if net_sgst > 0:
            pay_lines.append(EntryLine(COA.OUTPUT_SGST, DrCr.DEBIT, net_sgst, "Net SGST payable to govt"))
        if net_igst > 0:
            pay_lines.append(EntryLine(COA.OUTPUT_IGST, DrCr.DEBIT, net_igst, "Net IGST payable to govt"))

        pay_lines.append(EntryLine(COA.GST_PAYABLE, DrCr.CREDIT, net_payable, "GST payable via challan PMT-06"))
        entries.append(JournalEntry(
            date         = today,
            voucher_type = "Journal Voucher",
            narration    = f"Net GST payable to Government — {period_label}",
            lines        = pay_lines,
        ))

    return entries



# YEAR-END CLOSING ENTRIES
def close_books(
    gl,                      # GeneralLedger — passed in to avoid circular import
    period_end: date,
    period_label: str = "",
) -> list["JournalEntry"]:
    """
    Generate year-end closing journal entries.

    In Indian double-entry bookkeeping, at period-end all nominal accounts
    (P&L accounts — Sales, Purchases, Income, Expenses) are closed to
    Trading A/c → P&L A/c → Capital A/c.  Only real accounts (assets,
    liabilities, capital) carry forward to the next period.

    Entry sequence:
      1. Close all Sales & Direct Income → Trading A/c (Credit side)
      2. Close all Purchases & Direct Expenses → Trading A/c (Debit side)
      3. Transfer Gross Profit/Loss from Trading A/c → P&L A/c
      4. Close Indirect Income → P&L A/c (Credit side)
      5. Close Indirect Expenses → P&L A/c (Debit side)
      6. Transfer Net Profit/Loss from P&L A/c → Capital A/c

    Args:
        gl           : GeneralLedger (from Ledger.py build_ledger())
        period_end   : Last date of the accounting period (e.g. 31-Mar-2026)
        period_label : Human-readable period label for narration

    Returns:
        list[JournalEntry] — Closing entries ready to be posted to the GL.
        After posting, all P&L accounts will have zero balance.
    """
    entries: list[JournalEntry] = []
    suffix  = f" — {period_label}" if period_label else ""

    # Internal transfer accounts (not in COA as they cancel each other)
    TRADING_AC = Account("Trading A/c",          AccountGroup.RESERVES_SURPLUS)
    PL_AC      = Account("Profit & Loss A/c",    AccountGroup.RESERVES_SURPLUS)

    # Helper: get closing balance of a group (positive float = Dr balance)
    def group_bal(group: AccountGroup) -> dict[str, float]:
        """Return {account_name: balance} for all accounts in a group."""
        result = {}
        for acc in gl.accounts_in_group(group):
            amt, _ = acc.closing_balance
            if abs(amt) > 0.005:
                result[acc.name] = round(amt, 2)
        return result

    def make_account(name: str, group: AccountGroup) -> Account:
        return Account(name, group)

    # ── Step 1: Close Sales Accounts → Trading A/c (Dr Sales / Cr Trading)
    sales_bals = {**group_bal(AccountGroup.SALES_ACCOUNTS), **group_bal(AccountGroup.DIRECT_INCOME)}
    if sales_bals:
        lines = []
        total = 0.0
        for acc_name, bal in sales_bals.items():
            if bal > 0:
                # Sales/Income accounts normally have Cr balance;
                # to close, Debit them
                lines.append(EntryLine(
                    make_account(acc_name,
                        AccountGroup.SALES_ACCOUNTS if "Sales" in acc_name
                        else AccountGroup.DIRECT_INCOME),
                    DrCr.DEBIT, bal,
                    f"Closing {acc_name} to Trading A/c",
                ))

                total += bal

        if lines and total > 0:
            lines.append(EntryLine(TRADING_AC, DrCr.CREDIT, round(total, 2), "Transfer of Sales & Direct Income to Trading A/c"))
            entries.append(JournalEntry(
                date         = period_end,
                voucher_type = "Journal Voucher",
                narration    = f"Closing Sales & Direct Income to Trading A/c{suffix}",
                lines        = lines,
            ))

    # ── Step 2: Close Purchase & Direct Expenses → Trading A/c
    purchase_bals = {**group_bal(AccountGroup.PURCHASE_ACCOUNTS), **group_bal(AccountGroup.DIRECT_EXPENSES)}
    if purchase_bals:
        lines = []
        total = 0.0
        for acc_name, bal in purchase_bals.items():
            if bal > 0:
                lines.append(EntryLine(TRADING_AC, DrCr.DEBIT, bal, f"Closing {acc_name} to Trading A/c"))
                total += bal

        if lines and total > 0:
            for acc_name, bal in purchase_bals.items():
                if bal > 0:
                    lines.insert(
                        len(lines) - len([x for x in lines if x.account == TRADING_AC]),
                        EntryLine(
                            make_account(acc_name,
                                AccountGroup.PURCHASE_ACCOUNTS if "Purchase" in acc_name
                                else AccountGroup.DIRECT_EXPENSES),
                            DrCr.CREDIT, bal,
                            f"Closing {acc_name} to Trading A/c",
                        )
                    )
            
            # Rebuild correctly: Cr Purchases/Direct Exp, Dr Trading
            lines = []
            t = 0.0
            for acc_name, bal in purchase_bals.items():
                if bal > 0:
                    lines.append(EntryLine(
                        make_account(acc_name,
                            AccountGroup.PURCHASE_ACCOUNTS if "Purchase" in acc_name
                            else AccountGroup.DIRECT_EXPENSES),
                        DrCr.CREDIT, bal,
                        f"Closing {acc_name}",
                    ))
                    t += bal

            if t > 0:
                lines.insert(0, EntryLine(TRADING_AC, DrCr.DEBIT, round(t, 2), "Transfer of Purchases & Direct Expenses to Trading A/c"))
                entries.append(JournalEntry(
                    date         = period_end,
                    voucher_type = "Journal Voucher",
                    narration    = f"Closing Purchases & Direct Expenses to Trading A/c{suffix}",
                    lines        = lines,
                ))

    # ── Step 3: Gross Profit/Loss from Trading A/c → P&L A/c
    # Trading A/c balance after steps 1 & 2:
    # Sales+DirInc − Purchases−DirExp = Gross Profit (Cr if positive)
    sales_total = sum(v for v in (group_bal(AccountGroup.SALES_ACCOUNTS).values()))
    sales_total += sum(v for v in (group_bal(AccountGroup.DIRECT_INCOME).values()))
    purch_total = sum(v for v in (group_bal(AccountGroup.PURCHASE_ACCOUNTS).values()))
    purch_total += sum(v for v in (group_bal(AccountGroup.DIRECT_EXPENSES).values()))
    gross_profit = round(sales_total - purch_total, 2)

    if abs(gross_profit) > 0.005:
        if gross_profit > 0:
            # Gross Profit: Dr Trading A/c → Cr P&L A/c
            entries.append(JournalEntry(
                date         = period_end,
                voucher_type = "Journal Voucher",
                narration    = f"Transfer of Gross Profit to Profit & Loss A/c{suffix}",
                lines        = [
                    EntryLine(TRADING_AC, DrCr.DEBIT,  gross_profit, "Gross Profit transferred to P&L A/c"),
                    EntryLine(PL_AC,      DrCr.CREDIT, gross_profit, "Gross Profit b/d from Trading A/c"),
                ],
            ))
        else:
            # Gross Loss: Dr P&L A/c → Cr Trading A/c
            entries.append(JournalEntry(
                date         = period_end,
                voucher_type = "Journal Voucher",
                narration    = f"Transfer of Gross Loss to Profit & Loss A/c{suffix}",
                lines        = [
                    EntryLine(PL_AC,      DrCr.DEBIT,  abs(gross_profit), "Gross Loss b/d from Trading A/c"),
                    EntryLine(TRADING_AC, DrCr.CREDIT, abs(gross_profit), "Gross Loss transferred to P&L A/c"),
                ],
            ))

    # ── Step 4: Close Indirect Income → P&L A/c
    indir_inc_bals = group_bal(AccountGroup.INDIRECT_INCOME)
    if indir_inc_bals:
        lines = []
        total = 0.0
        for acc_name, bal in indir_inc_bals.items():
            if bal > 0:
                lines.append(EntryLine(
                    make_account(acc_name, AccountGroup.INDIRECT_INCOME),
                    DrCr.DEBIT, bal,
                    f"Closing {acc_name} to P&L A/c",
                ))
                total += bal

        if lines and total > 0:
            lines.append(EntryLine(PL_AC, DrCr.CREDIT, round(total, 2), "Transfer of Indirect Income to P&L A/c"))
            entries.append(JournalEntry(
                date         = period_end,
                voucher_type = "Journal Voucher",
                narration    = f"Closing Indirect Income to Profit & Loss A/c{suffix}",
                lines        = lines,
            ))

    # ── Step 5: Close Indirect Expenses → P&L A/c
    indir_exp_bals = group_bal(AccountGroup.INDIRECT_EXPENSES)
    if indir_exp_bals:
        lines = []
        total = 0.0
        for acc_name, bal in indir_exp_bals.items():
            if bal > 0:
                lines.append(EntryLine(
                    make_account(acc_name, AccountGroup.INDIRECT_EXPENSES),
                    DrCr.CREDIT, bal,
                    f"Closing {acc_name}",
                ))
                total += bal
        if lines and total > 0:
            lines.insert(0, EntryLine(PL_AC, DrCr.DEBIT, round(total, 2),
                                      "Transfer of Indirect Expenses from P&L A/c"))
            entries.append(JournalEntry(
                date         = period_end,
                voucher_type = "Journal Voucher",
                narration    = f"Closing Indirect Expenses to Profit & Loss A/c{suffix}",
                lines        = lines,
            ))

    # ── Step 6: Transfer Net Profit/Loss → Capital A/c
    indir_inc_total = sum(v for v in indir_inc_bals.values())
    indir_exp_total = sum(v for v in indir_exp_bals.values())
    net_profit = round(gross_profit + indir_inc_total - indir_exp_total, 2)

    if abs(net_profit) > 0.005:
        if net_profit > 0:
            entries.append(JournalEntry(
                date         = period_end,
                voucher_type = "Journal Voucher",
                narration    = f"Transfer of Net Profit to Capital A/c{suffix}",
                lines        = [
                    EntryLine(PL_AC,      DrCr.DEBIT,  net_profit, "Net Profit transferred to Capital A/c"),
                    EntryLine(COA.CAPITAL, DrCr.CREDIT, net_profit, "Net Profit added to Capital (owner's equity increases)"),
                ],
            ))
        else:
            entries.append(JournalEntry(
                date         = period_end,
                voucher_type = "Journal Voucher",
                narration    = f"Transfer of Net Loss to Capital A/c{suffix}",
                lines        = [
                    EntryLine(COA.CAPITAL, DrCr.DEBIT,  abs(net_profit), "Net Loss deducted from Capital (owner's equity decreases)"),
                    EntryLine(PL_AC,       DrCr.CREDIT, abs(net_profit), "Net Loss transferred from P&L A/c"),
                ],
            ))

    return entries
