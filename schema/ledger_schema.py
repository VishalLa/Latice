from __future__ import annotations

from datetime import date as Date
from enum import Enum
from typing import List

from pydantic import Field, field_validator

from .base import SchemaBase
from .journal_schema import DrCr, Account, AccountGroup


TRAILBALANCE_TOLARANCE: float = 0.05

class LedgerPosting(SchemaBase):
    """One line posted into a ledger account folio."""
    date:           Date
    particulars:    str       # Opposite account (Indian T-account format)
    journal_id:     str       # Cross-ref to JournalEntry.entry_id
    voucher_type:   str
    dr_amount:      float = 0.0
    cr_amount:      float = 0.0
    balance:        float = 0.0
    balance_side:   str   = "Dr"   # "Dr" | "Cr"


class LedgerAccount(SchemaBase):
    account:    Account
    posting:    List[LedgerPosting] = Field(default_factory=list)
    _balance:   float               = Field(default=0.0, init=False, repr=False)

    @property
    def name(self) -> str:
        return self.account.name

    @property
    def group(self) -> AccountGroup:
        return self.account.group

    def post(
        self,
        date: Date,
        particulars: str,
        journal_id: str,
        voucher_type: str,
        dr_cr: DrCr,
        amount: float
    ) -> None:
        if dr_cr == DrCr.DEBIT:
            dr_amount = round(amount, 2)
            cr_amount = 0.0
            self._balance += dr_amount

        else:
            cr_amount = round(amount, 2)
            dr_amount = 0.0
            self._balance -= cr_amount

        bal_abs = abs(round(self._balance, 2))
        bal_side = "Dr" if self._balance >= 0 else "Cr"

        self.posting.append(
            date         = date,
            particulars  = particulars,
            journal_id   = journal_id,
            voucher_type = voucher_type,
            dr_amount    = dr_amount,
            cr_amount    = cr_amount,
            balance      = bal_abs,
            balance_side = bal_side,
        )

    @property
    def closing_balance(self) -> tuple[float, str]:
        return abs(round(self._balance, 2)), ("Dr" if self._balance >= 0 else "Cr")
    
    @property
    def total_debits(self) -> float:
        return round(sum(p.dr_amount for p in self.posting), 2)

    @property
    def total_credits(self) -> float:
        return  round(sum(p.cr_amount for p in self.posting), 2)

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


class TrialBalanceLine(SchemaBase):
    account:        str
    group:          str
    closing_debit:  float = 0.0
    closing_credit: float = 0.0


class TrialBalance(SchemaBase):
    lines:          List[TrialBalanceLine]
    as_on:          Date
    total_debits:   float
    total_credits:  float

    @property
    def is_balanced(self) -> bool:
        return abs(self.total_debits - self.total_credits) < TRAILBALANCE_TOLARANCE

    @property
    def difference(self) -> float:
        return round(abs(self.total_debits - self.total_credits), 2)

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


class GSTSummary(SchemaBase):
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
        return round(
            self.output_cgst + self.output_sgst + self.output_igst + self.output_cess, 2
        )

    @property
    def total_input_tax(self) -> float:
        return round(
            self.input_cgst + self.input_sgst + self.input_igst + self.input_cess, 2
        )

    def to_dict(self) -> dict:
        return {
            "period":    self.period_label,
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


class CashBookLine(SchemaBase):
    date:         Date
    particulars:  str
    voucher_type: str
    journal_id:   str
    account_type: str        # "Cash" | "Bank"
    receipts:     float = 0.0
    payments:     float = 0.0
    balance:      float = 0.0


class AgeingLine(SchemaBase):
    account:      str
    balance:      float
    balance_side: str
    current:      float = 0.0    # 0–30 days
    days_30_60:   float = 0.0    # 31–60 days
    days_60_90:   float = 0.0    # 61–90 days
    over_90:      float = 0.0    # 91 + days
