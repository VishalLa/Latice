from __future__ import annotations

from typing import Optional
from schema import (
    LedgerAccount, 
    JournalEntry, 
    Account, 
    DrCr, 
    AccountGroup, 
)


class GeneralLedger:
    """
    The complete General Ledger — a collection of all LedgerAccounts.
    Usage:
        gl = GeneralLedger()
        gl.post_entries(journal_entries)
        tb = gl.trial_balance()
        gst = gl.gst_summary()
    """
    def __init__(self) -> None:
        # account name → LedgerAccount
        self._accounts: dict[str, LedgerAccount] = {}

    def _get_or_create(self, account: Account) -> LedgerAccount:
        if account.name not in self._accounts:
            self._accounts[account.name] = LedgerAccount(account=account)
        return self._accounts[account.name]

    def post_entries(self, entries: list[JournalEntry]) -> None:
        for entry in entries:
            # Build a map: for each line, the "particulars" = names of opposite side accounts
            debit_names  = [l.account.name for l in entry.lines if l.dr_cr == DrCr.DEBIT]
            credit_names = [l.account.name for l in entry.lines if l.dr_cr == DrCr.CREDIT]

            for line in entry.lines:
                ledger_acc = self._get_or_create(line.account)

                # Particulars: "By <Credit A/c>" for debits, "To <Debit A/c>" for credits
                # Indian format: Dr entries show "To <opposite>", Cr entries show "By <opposite>"
                if line.dr_cr == DrCr.DEBIT:
                    opposite = ", ".join(credit_names)
                    particulars = f"To {opposite}"
                else:
                    opposite = ", ".join(debit_names)
                    particulars = f"By {opposite}"

                ledger_acc.post(
                    date         = entry.date,
                    particulars  = particulars,
                    journal_id   = entry.entry_id,
                    voucher_type = entry.voucher_type,
                    dr_cr        = line.dr_cr,
                    amount       = line.amount,
                )

    @property
    def accounts(self) -> list[LedgerAccount]:
        return sorted(self._accounts.values(), key=lambda a: a.name)

    def get(self, account_name: str) -> Optional[LedgerAccount]:
        return self._accounts.get(account_name)

    def accounts_in_group(self, group: AccountGroup) -> list[LedgerAccount]:
        return [a for a in self.accounts if a.group == group]
