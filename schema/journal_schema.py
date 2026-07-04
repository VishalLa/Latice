from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import List, Optional, Set

class DrCr(Enum):
    DEBIT  = "Dr"
    CREDIT = "Cr"


class AccountGroup(Enum):
    # Balance Sheet — Liabilities
    CAPITAL_ACCOUNT     = "Capital Account"
    RESERVES_SURPLUS    = "Reserves & Surplus"
    LOANS_LIABILITY     = "Loans (Liability)"
    CURRENT_LIABILITIES = "Current Liabilities"
    SUNDRY_CREDITORS    = "Sundry Creditors"
    DUTIES_TAXES        = "Duties & Taxes"

    # Balance Sheet — Assets
    FIXED_ASSETS        = "Fixed Assets"
    INVESTMENTS         = "Investments"
    CURRENT_ASSETS      = "Current Assets"
    SUNDRY_DEBTORS      = "Sundry Debtors"
    CASH_IN_HAND        = "Cash-in-Hand"
    BANK_ACCOUNTS       = "Bank Accounts"
    STOCK_IN_HAND       = "Stock-in-Hand"
    LOANS_ADVANCES      = "Loans & Advances (Asset)"

    # P&L — Income
    SALES_ACCOUNTS      = "Sales Accounts"
    DIRECT_INCOME       = "Direct Income"
    INDIRECT_INCOME     = "Indirect Income"

    # P&L — Expenses
    PURCHASE_ACCOUNTS   = "Purchase Accounts"
    DIRECT_EXPENSES     = "Direct Expenses"
    INDIRECT_EXPENSES   = "Indirect Expenses"


# Groups whose normal balance is a Debit (assets & expenses)
DEBIT_INCREASES: Set[AccountGroup] = {
    AccountGroup.FIXED_ASSETS,
    AccountGroup.INVESTMENTS,
    AccountGroup.CURRENT_ASSETS,
    AccountGroup.SUNDRY_DEBTORS,
    AccountGroup.CASH_IN_HAND,
    AccountGroup.BANK_ACCOUNTS,
    AccountGroup.STOCK_IN_HAND,
    AccountGroup.LOANS_ADVANCES,
    AccountGroup.PURCHASE_ACCOUNTS,
    AccountGroup.DIRECT_EXPENSES,
    AccountGroup.INDIRECT_EXPENSES,
}


@dataclass
class Account:
    name:     str
    group:    AccountGroup
    gst_code: Optional[str] = None

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Account) and self.name == other.name

    @property
    def normal_balance(self) -> DrCr:
        """Side that *increases* this account."""
        return DrCr.DEBIT if self.group in DEBIT_INCREASES else DrCr.CREDIT


class COA:
    """
    Standard / pre-defined accounts — mirrors Tally ERP predefined ledgers
    so exported data is directly importable.
    """

    # Assets
    CASH           = Account("Cash A/c",           AccountGroup.CASH_IN_HAND)
    BANK           = Account("Bank A/c",            AccountGroup.BANK_ACCOUNTS)
    STOCK          = Account("Stock-in-Hand A/c",   AccountGroup.STOCK_IN_HAND)
    DEBTORS        = Account("Sundry Debtors A/c",  AccountGroup.SUNDRY_DEBTORS)

    # Liabilities
    CREDITORS      = Account("Sundry Creditors A/c", AccountGroup.SUNDRY_CREDITORS)
    CAPITAL        = Account("Capital A/c",           AccountGroup.CAPITAL_ACCOUNT)

    # GST — Input Tax Credit (Current Assets)
    INPUT_CGST     = Account("Input CGST A/c",      AccountGroup.CURRENT_ASSETS,  "CGST")
    INPUT_SGST     = Account("Input SGST A/c",       AccountGroup.CURRENT_ASSETS,  "SGST")
    INPUT_IGST     = Account("Input IGST A/c",       AccountGroup.CURRENT_ASSETS,  "IGST")
    INPUT_CESS     = Account("Input GST Cess A/c",   AccountGroup.CURRENT_ASSETS,  "CESS")

    # GST — Output Tax (Duties & Taxes)
    OUTPUT_CGST    = Account("Output CGST A/c",     AccountGroup.DUTIES_TAXES, "CGST")
    OUTPUT_SGST    = Account("Output SGST A/c",     AccountGroup.DUTIES_TAXES, "SGST")
    OUTPUT_IGST    = Account("Output IGST A/c",     AccountGroup.DUTIES_TAXES, "IGST")
    OUTPUT_CESS    = Account("Output GST Cess A/c", AccountGroup.DUTIES_TAXES, "CESS")

    # GST Payable (net after ITC set-off)
    GST_PAYABLE    = Account("GST Payable A/c",     AccountGroup.DUTIES_TAXES, "NET")

    # TDS
    TDS_PAYABLE    = Account("TDS Payable A/c",     AccountGroup.DUTIES_TAXES)
    TDS_RECEIVABLE = Account("TDS Receivable A/c",  AccountGroup.CURRENT_ASSETS)

    # Purchase / Expense
    PURCHASES      = Account("Purchase A/c",         AccountGroup.PURCHASE_ACCOUNTS)
    FREIGHT_IN     = Account("Freight Inward A/c",   AccountGroup.DIRECT_EXPENSES)
    OTHER_CHARGES  = Account("Other Charges A/c",    AccountGroup.INDIRECT_EXPENSES)
    ROUND_OFF      = Account("Round Off A/c",         AccountGroup.INDIRECT_EXPENSES)
    DISCOUNT_RECV  = Account("Discount Received A/c", AccountGroup.INDIRECT_INCOME)

    # Sales / Income
    SALES          = Account("Sales A/c",            AccountGroup.SALES_ACCOUNTS)
    DISCOUNT_GIVEN = Account("Discount Allowed A/c", AccountGroup.INDIRECT_EXPENSES)

    @classmethod
    def creditor_for(cls, vendor_name: str) -> Account:
        """Vendor-specific Sundry Creditors sub-ledger."""
        return Account(f"{vendor_name} A/c", AccountGroup.SUNDRY_CREDITORS)

    @classmethod
    def debtor_for(cls, buyer_name: str) -> Account:
        """Buyer-specific Sundry Debtors sub-ledger."""
        return Account(f"{buyer_name} A/c", AccountGroup.SUNDRY_DEBTORS)

    @classmethod
    def payment_account(cls, mode: Optional[str]) -> Account:
        """Return Cash or Bank account based on payment mode string."""
        if mode and mode.upper() in {"UPI", "CARD", "NEFT", "RTGS", "IMPS", "ONLINE", "CHEQUE"}:
            return cls.BANK
        return cls.CASH


@dataclass
class EntryLine:
    account:   Account
    dr_cr:     DrCr
    amount:    float        # Always positive
    narration: str = ""

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError(f"EntryLine amount must be positive, got {self.amount}")


@dataclass
class JournalEntry:
    entry_id:     str             = field(default_factory=lambda: str(uuid.uuid4())[:8].upper())
    date:         date            = field(default_factory=date.today)
    voucher_type: str             = "Journal Voucher"
    narration:    str             = ""
    lines:        List[EntryLine] = field(default_factory=list)

    # Source bill metadata (traceability)
    source_file:    str = ""
    invoice_number: str = ""
    vendor_name:    str = ""
    direction:      str = ""    # "input" | "output"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Assert debits == credits (double-entry rule)."""
        if not self.lines:
            return
        debits  = round(sum(l.amount for l in self.lines if l.dr_cr == DrCr.DEBIT),  2)
        credits = round(sum(l.amount for l in self.lines if l.dr_cr == DrCr.CREDIT), 2)
        if abs(debits - credits) > 0.02:     # 2-paise tolerance
            raise ValueError(
                f"Journal entry {self.entry_id} out of balance: "
                f"Dr ₹{debits:.2f} ≠ Cr ₹{credits:.2f}\nLines: {self.lines}"
            )

    @property
    def total_amount(self) -> float:
        return round(sum(l.amount for l in self.lines if l.dr_cr == DrCr.DEBIT), 2)

    def to_dict(self) -> dict:
        return {
            "entry_id":       self.entry_id,
            "date":           self.date.strftime("%d-%m-%Y"),
            "voucher_type":   self.voucher_type,
            "narration":      self.narration,
            "source_file":    self.source_file,
            "invoice_number": self.invoice_number,
            "vendor_name":    self.vendor_name,
            "direction":      self.direction,
            "total_amount":   self.total_amount,
            "lines": [
                {
                    "account":  l.account.name,
                    "group":    l.account.group.value,
                    "dr_cr":    l.dr_cr.value,
                    "amount":   l.amount,
                }
                for l in self.lines
            ],
        }


# YEAR-END CLOSING — result container
@dataclass
class ClosingResult:
    """
    Returned by close_books().  Bundles the generated journal entries with
    the key P&L figures so callers can log / display results without having
    to re-derive them from the entries.

    Attributes
    ----------
    entries          : List of closing JournalEntry objects, in sequence order.
                       Post these to the GeneralLedger after all normal entries.
    gross_profit     : Sales + Direct Income − Purchases − Direct Expenses.
                       Negative value = Gross Loss.
    net_profit       : Gross Profit ± Indirect Income/Expenses.
                       Negative value = Net Loss.
    period_end       : The closing date passed to close_books().
    period_label     : Human-readable label ("FY 2025-26", "Apr-2025", …).
    warnings         : Non-fatal anomalies found during closing (e.g. accounts
                       with unexpected balance sides, zero-balance P&L groups).
    """
    entries:       list[JournalEntry]  = field(default_factory=list)
    gross_profit:  float               = 0.0
    net_profit:    float               = 0.0
    period_end:    Optional[date]      = None
    period_label:  str                 = ""
    warnings:      list[str]           = field(default_factory=list)

    @property
    def is_profit(self) -> bool:
        return self.net_profit >= 0

    @property
    def net_profit_label(self) -> str:
        return "Net Profit" if self.is_profit else "Net Loss"

    @property
    def gross_profit_label(self) -> str:
        return "Gross Profit" if self.gross_profit >= 0 else "Gross Loss"

