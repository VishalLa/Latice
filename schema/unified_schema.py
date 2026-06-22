from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import ClassVar, List, Optional, Set

class Block:
    """
    One detected word returned by PaddleOCR.

    Attributes
    ----------
    x, y : top-left pixel coordinates of the bounding box
    text : recognised text
    conf : recognition confidence in [0, 1]
    """

    def __init__(self, x: int, y: int, text: str, conf: float) -> None:
        self.x    = x
        self.y    = y
        self.text = text
        self.conf = conf

    def __repr__(self) -> str:
        return f"Block(y={self.y}, x={self.x}, text={self.text!r})"


# 2.  Journal / Chart of Accounts
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


# 3.  General Ledger

@dataclass
class LedgerPosting:
    """One line posted into a ledger account folio."""
    date:         date
    particulars:  str       # Opposite account (Indian T-account format)
    journal_id:   str       # Cross-ref to JournalEntry.entry_id
    voucher_type: str
    dr_amount:    float = 0.0
    cr_amount:    float = 0.0
    balance:      float = 0.0
    balance_side: str   = "Dr"   # "Dr" | "Cr"


@dataclass
class LedgerAccount:
    account:  Account
    postings: List[LedgerPosting] = field(default_factory=list)
    _balance: float               = field(default=0.0, init=False, repr=False)

    @property
    def name(self) -> str:
        return self.account.name

    @property
    def group(self) -> AccountGroup:
        return self.account.group

    def post(
        self,
        date_: date,
        particulars: str,
        journal_id: str,
        voucher_type: str,
        dr_cr: DrCr,
        amount: float,
    ) -> None:
        if dr_cr == DrCr.DEBIT:
            dr_amount     = round(amount, 2)
            cr_amount     = 0.0
            self._balance += dr_amount
        else:
            cr_amount     = round(amount, 2)
            dr_amount     = 0.0
            self._balance -= cr_amount

        bal_abs  = abs(round(self._balance, 2))
        bal_side = "Dr" if self._balance >= 0 else "Cr"

        self.postings.append(LedgerPosting(
            date         = date_,
            particulars  = particulars,
            journal_id   = journal_id,
            voucher_type = voucher_type,
            dr_amount    = dr_amount,
            cr_amount    = cr_amount,
            balance      = bal_abs,
            balance_side = bal_side,
        ))

    @property
    def closing_balance(self) -> tuple[float, str]:
        return abs(round(self._balance, 2)), ("Dr" if self._balance >= 0 else "Cr")

    @property
    def total_debits(self) -> float:
        return round(sum(p.dr_amount for p in self.postings), 2)

    @property
    def total_credits(self) -> float:
        return round(sum(p.cr_amount for p in self.postings), 2)

    @property
    def is_debit_balance(self) -> bool:
        return self._balance >= 0

    def to_dict(self) -> dict:
        bal_amt, bal_side = self.closing_balance
        return {
            "account":         self.name,
            "group":           self.group.value,
            "total_debits":    self.total_debits,
            "total_credits":   self.total_credits,
            "closing_balance": bal_amt,
            "balance_side":    bal_side,
            "postings": [
                {
                    "date":         p.date.strftime("%d-%m-%Y"),
                    "particulars":  p.particulars,
                    "journal_id":   p.journal_id,
                    "voucher_type": p.voucher_type,
                    "dr_amount":    p.dr_amount or None,
                    "cr_amount":    p.cr_amount or None,
                    "balance":      p.balance,
                    "balance_side": p.balance_side,
                }
                for p in self.postings
            ],
        }


# Trial Balance
@dataclass
class TrialBalanceLine:
    account:        str
    group:          str
    closing_debit:  float = 0.0
    closing_credit: float = 0.0


@dataclass
class TrialBalance:
    lines:        List[TrialBalanceLine]
    as_on:        date
    total_debit:  float
    total_credit: float

    @property
    def is_balanced(self) -> bool:
        return abs(self.total_debit - self.total_credit) < 0.05

    @property
    def difference(self) -> float:
        return round(abs(self.total_debit - self.total_credit), 2)

    def to_dict(self) -> dict:
        return {
            "as_on":        self.as_on.strftime("%d-%m-%Y"),
            "is_balanced":  self.is_balanced,
            "total_debit":  self.total_debit,
            "total_credit": self.total_credit,
            "difference":   self.difference,
            "lines": [
                {
                    "account":        l.account,
                    "group":          l.group,
                    "closing_debit":  l.closing_debit  or None,
                    "closing_credit": l.closing_credit or None,
                }
                for l in self.lines
            ],
        }


# GST Summary
@dataclass
class GSTSummary:
    """
    GST liability / ITC summary for one filing period.
    Mirrors GSTR-3B: Section 3.1 (output), Section 4 (ITC), Section 4D (payable).
    """
    period_label: str

    output_cgst: float = 0.0
    output_sgst: float = 0.0
    output_igst: float = 0.0
    output_cess: float = 0.0

    input_cgst: float = 0.0
    input_sgst: float = 0.0
    input_igst: float = 0.0
    input_cess: float = 0.0

    cgst_itc_used: float = 0.0
    sgst_itc_used: float = 0.0
    igst_itc_used: float = 0.0

    net_cgst_payable:  float = 0.0
    net_sgst_payable:  float = 0.0
    net_igst_payable:  float = 0.0
    net_total_payable: float = 0.0
    itc_carry_forward: float = 0.0

    @property
    def total_output_tax(self) -> float:
        return round(self.output_cgst + self.output_sgst + self.output_igst + self.output_cess, 2)

    @property
    def total_input_tax(self) -> float:
        return round(self.input_cgst + self.input_sgst + self.input_igst + self.input_cess, 2)

    def to_dict(self) -> dict:
        return {
            "period":           self.period_label,
            "output_tax":       {
                "cgst":  self.output_cgst,
                "sgst":  self.output_sgst,
                "igst":  self.output_igst,
                "cess":  self.output_cess,
                "total": self.total_output_tax,
            },
            "input_tax_credit": {
                "cgst":  self.input_cgst,
                "sgst":  self.input_sgst,
                "igst":  self.input_igst,
                "cess":  self.input_cess,
                "total": self.total_input_tax,
            },
            "itc_utilised": {
                "cgst_used": self.cgst_itc_used,
                "sgst_used": self.sgst_itc_used,
                "igst_used": self.igst_itc_used,
            },
            "net_payable": {
                "cgst":  self.net_cgst_payable,
                "sgst":  self.net_sgst_payable,
                "igst":  self.net_igst_payable,
                "total": self.net_total_payable,
            },
            "itc_carry_forward": self.itc_carry_forward,
        }


# Cash Book
@dataclass
class CashBookLine:
    date:         date
    particulars:  str
    voucher_type: str
    journal_id:   str
    account_type: str        # "Cash" | "Bank"
    receipts:     float = 0.0
    payments:     float = 0.0
    balance:      float = 0.0


# Ageing
@dataclass
class AgeingLine:
    account:      str
    balance:      float
    balance_side: str
    current:      float = 0.0    # 0–30 days
    days_30_60:   float = 0.0    # 31–60 days
    days_60_90:   float = 0.0    # 61–90 days
    over_90:      float = 0.0    # 91 + days




# 4.  Bank Reconciliation
@dataclass
class BankTemplate:
    """Column-mapping descriptor for a specific bank's CSV export format."""
    bank_name:        str
    version:          str
    date_column:      str
    date_format:      str
    narration_column: str

    file_type:        str          = "csv"
    skip_rows:        int          = 0
    encoding:         str          = "utf-8"
    debit_column:     Optional[str] = None
    credit_column:    Optional[str] = None
    txn_id_column:    Optional[str] = None
    balance_column:   Optional[str] = None
    type_column:      Optional[str] = None
    amount_column:    Optional[str] = None
    fingerprint:      Set[str]      = field(default_factory=set)


@dataclass
class BankStatement:
    """One row parsed from a bank CSV export."""
    row_index:        int
    bank_name:        str
    template_version: str

    date:             Optional[str]   = None   # ISO YYYY-MM-DD
    date_raw:         Optional[str]   = None
    narration:        str             = ""
    debit:            float           = 0.0    # Money leaving the account
    credit:           float           = 0.0    # Money entering the account
    balance:          Optional[float] = None
    txn_id:           Optional[str]   = None
    parse_warnings:   List[str]       = field(default_factory=list)

    def __post_init__(self) -> None:
        self.debit  = float(self.debit)  if self.debit  not in (None, "") else 0.0
        self.credit = float(self.credit) if self.credit not in (None, "") else 0.0
        self.balance = (
            float(self.balance) if self.balance not in (None, "") else None
        )



# --- 4b. Ledger record for reconciliation ------------------------------------

class LedgerSource(Enum):
    """
    Discriminates between the two paths that produce a LedgerFormat record.

    AUTO   – record was derived automatically from scanned bills via build_ledger()
             / to_journal_entries().  The ledger was never a user-supplied file.
    MANUAL – record was loaded from a CSV that the user uploaded directly.
    """
    AUTO   = "auto"    # Path A: bill scan → journal entry → ledger
    MANUAL = "manual"  # Path B: user uploaded ledger CSV


@dataclass
class LedgerFormat:
    """
    One reconcilable row of the company's ledger, regardless of which path
    produced it.

    Path A (AUTO)
    -------------
    Populated by ledger_to_recon_records() which converts JournalEntry objects
    (from build_ledger()) into this format.  Key fields:
        ledger_id        → JournalEntry.entry_id
        account_name     → vendor / buyer name (narration)
        transaction_date → ISO date from the journal entry
        debit_amount     → sum of debit lines for BANK/CASH account
        credit_amount    → sum of credit lines for BANK/CASH account
        reference_id     → invoice_number from the JournalEntry
        source           → LedgerSource.AUTO
        journal_entry_id → back-ref to the originating JournalEntry.entry_id

    Path B (MANUAL)
    ---------------
    Populated by load_ledger_csv() reading a user-supplied CSV.  Key fields:
        ledger_id        → from CSV column or auto-generated "L0001" …
        account_name     → from "particulars" / "description" / "account_name" column
        transaction_date → ISO date parsed from CSV
        debit_amount     → from "debit" / "withdrawal" / "dr" column
        credit_amount    → from "credit" / "deposit" / "cr" column
        reference_id     → from "voucher_no" / "cheque_no" / "ref" column
        source           → LedgerSource.MANUAL
        journal_entry_id → None (no journal entry exists for manual records)

    Fields common to both paths
    ---------------------------
    All matching logic (exact_match, fuzzy_match, ai_agent_match) depends only
    on: ledger_id, account_name, transaction_date, debit_amount, credit_amount,
    reference_id.  The extra fields (source, journal_entry_id, …) are metadata
    for audit / UI display and are never read by the matchers.
    """

    _id_counter:       ClassVar[int] = 1

    account_name:         str

    ledger_id:            Optional[str]  = None
    account_number:       Optional[str]  = None
    transaction_date:     Optional[str]  = None    # ISO YYYY-MM-DD
    transaction_date_raw: Optional[str]  = None
    debit_amount:         float          = 0.0     # Payment out / expense
    credit_amount:        float          = 0.0     # Receipt in / income
    reference_id:         Optional[str]  = None    # Invoice / cheque / ref no.
    parse_warnings:       List[str]      = field(default_factory=list)

    # ── Path discriminator & metadata ────────────────────────────────────────
    source:               LedgerSource   = LedgerSource.MANUAL
    """
    LedgerSource.AUTO   → created automatically from bill scan pipeline.
    LedgerSource.MANUAL → loaded from a user-uploaded CSV.
    """

    journal_entry_id:     Optional[str]  = None
    """
    Set only when source == AUTO.  Equals JournalEntry.entry_id so the
    originating double-entry record can be retrieved for audit.
    """

    voucher_type:         Optional[str]  = None
    """
    E.g. "Purchase Voucher", "Payment Voucher".  Populated for AUTO records
    from JournalEntry.voucher_type; may be None for MANUAL records.
    """

    vendor_name:          Optional[str]  = None
    """
    Vendor / counter-party name.  For AUTO records this comes from
    JournalEntry.vendor_name; for MANUAL records it mirrors account_name.
    """

    def __post_init__(self) -> None:
        if not self.ledger_id:
            self.ledger_id = f"L{LedgerFormat._id_counter:04d}"
            LedgerFormat._id_counter += 1

        self.debit_amount  = (
            float(self.debit_amount)
            if self.debit_amount  not in (None, "") else 0.0
        )
        self.credit_amount = (
            float(self.credit_amount)
            if self.credit_amount not in (None, "") else 0.0
        )

        # Convenience default: for MANUAL records vendor_name == account_name
        if self.vendor_name is None and self.source == LedgerSource.MANUAL:
            self.vendor_name = self.account_name

    @property
    def is_credit(self) -> bool:
        """True when this is a pure credit row (money IN)."""
        return self.credit_amount > 0.0 and self.debit_amount == 0.0

    @property
    def is_debit(self) -> bool:
        """True when this is a pure debit row (money OUT)."""
        return self.debit_amount > 0.0 and self.credit_amount == 0.0

    @property
    def is_auto(self) -> bool:
        return self.source == LedgerSource.AUTO

    @property
    def is_manual(self) -> bool:
        return self.source == LedgerSource.MANUAL

    def to_dict(self) -> dict:
        return {
            "ledger_id":            self.ledger_id,
            "source":               self.source.value,
            "account_name":         self.account_name,
            "account_number":       self.account_number,
            "transaction_date":     self.transaction_date,
            "transaction_date_raw": self.transaction_date_raw,
            "debit_amount":         self.debit_amount,
            "credit_amount":        self.credit_amount,
            "reference_id":         self.reference_id,
            "voucher_type":         self.voucher_type,
            "vendor_name":          self.vendor_name,
            "journal_entry_id":     self.journal_entry_id,
            "parse_warnings":       self.parse_warnings,
        }


# 5.  Conversion helper — AUTO path
def journal_entries_to_ledger_records(
    entries: List[JournalEntry],
    cash_credit_accounts: Optional[Set[str]] = None,
) -> List[LedgerFormat]:
    """
    Convert JournalEntry objects (from the bill-scan pipeline) into
    LedgerFormat records suitable for bank reconciliation.

    Only entries that touch a cash / bank account are converted, because
    those are the ones that will appear on a bank statement.

    Parameters
    ----------
    entries
        Output of build_ledger() or to_journal_entries().
    cash_credit_accounts
        Names of accounts that represent cash or bank movements.
        Defaults to {COA.CASH.name, COA.BANK.name}.

    Returns
    -------
    List[LedgerFormat] with source == LedgerSource.AUTO.
    """
    if cash_credit_accounts is None:
        cash_credit_accounts = {COA.CASH.name, COA.BANK.name}

    records: List[LedgerFormat] = []

    for entry in entries:
        debit_cash  = sum(
            l.amount for l in entry.lines
            if l.dr_cr == DrCr.DEBIT  and l.account.name in cash_credit_accounts
        )
        credit_cash = sum(
            l.amount for l in entry.lines
            if l.dr_cr == DrCr.CREDIT and l.account.name in cash_credit_accounts
        )

        # Skip entries that have no cash / bank movement
        if debit_cash == 0.0 and credit_cash == 0.0:
            continue

        # Narration / description for the reconciliation layer
        non_cash_names = [
            l.account.name for l in entry.lines
            if l.account.name not in cash_credit_accounts
        ]
        description = (
            entry.vendor_name
            or (non_cash_names[0] if non_cash_names else "")
            or entry.narration
        )

        records.append(LedgerFormat(
            account_name         = description,
            transaction_date     = entry.date.strftime("%Y-%m-%d"),
            debit_amount         = round(debit_cash,  2),
            credit_amount        = round(credit_cash, 2),
            reference_id         = entry.invoice_number or None,
            source               = LedgerSource.AUTO,
            journal_entry_id     = entry.entry_id,
            voucher_type         = entry.voucher_type,
            vendor_name          = entry.vendor_name or None,
        ))

    return records
