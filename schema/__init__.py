from .journal_schema import (
    DrCr,
    AccountGroup,
    Account,
    COA,
    EntryLine,
    JournalEntry,
    ClosingResult
)

from .ledger_schema import (
    LedgerPosting,
    LedgerAccount,
    TrialBalance,
    TrialBalanceLine,
    GSTSummary,
    CashBookLine,
    AgeingLine
)

from .bank_renc_schema import (
    BankStatement,
    BankTemplate,
    LedgerFormat,
    LedgerSource
)

from .tds_schema import (
    DeducteeType,
    TDSStatus,
    TDSSection,
    TDS_SECTIONS,
    TDSEntry,
    TDSRegister,
    Form26QLine,
    Form26Q,
    TDSResult
)

from typing import Optional, Set, List

__all__ = [
    # Journal / COA
    "DrCr",
    "AccountGroup",
    "Account",
    "COA",
    "EntryLine",
    "JournalEntry",
    "ClosingResult",

    # General Ledger
    "LedgerPosting",
    "LedgerAccount",
    "TrialBalanceLine",
    "TrialBalance",
    "GSTSummary",
    "CashBookLine",
    "AgeingLine",

    # Bank Reconciliation
    "BankTemplate",
    "BankStatement",
    "LedgerSource",
    "LedgerFormat",

    # TDS
    "DeducteeType",
    "TDSStatus",
    "TDSSection",
    "TDS_SECTIONS",
    "TDSEntry",
    "TDSRegister",
    "Form26QLine",
    "Form26Q",
    "TDSResult",


    "journal_entries_to_ledger_records",
]


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

