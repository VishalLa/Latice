"""
SQLAlchemy ORM models for the Multi-Period / Financial Year Management layer.

Relationship to existing models
--------------------------------
  FiscalPeriod         ← anchor for every period-scoped table
  PeriodAccountBalance ← one row per (period, account); feeds the next
                         period's opening balance journal entry
  PeriodGSTPosition    ← monthly GSTR-3B snapshot; carries ITC forward
  PeriodTDSPosition    ← monthly TDS register snapshot

No columns in the existing tables (models.py) are modified.

Indian FY conventions honoured
-------------------------------
• FY label: "2025-26" (Apr 2025 → Mar 2026)
• Months labelled "Apr-2025", "May-2025", …, "Mar-2026"
• Quarters labelled "Q1 2025-26", "Q2 2025-26", "Q3 2025-26", "Q4 2025-26"
• ITC carry-forward lives in PeriodGSTPosition.itc_carry_forward.
  PeriodService reads it and inserts an opening-balance adjustment journal
  entry when the next period is initialised.

How period_type works
---------------------
  "month"   → one row per calendar month within the FY (most granular)
  "quarter" → one row per quarter (aggregated view; computed from months)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base, _uuid


class FiscalPeriod(Base):
    """
    One accounting period within an Indian financial year.

    Columns
    -------
    financial_year
        "2025-26" — the FY this period belongs to.
    period_type
        "month"   → Apr-2025, May-2025, … Mar-2026  (12 rows per FY)
        "quarter" → Q1 2025-26, …, Q4 2025-26        (4 rows per FY)
        Only "month" rows are written by PeriodService.close_period().
        Quarter rows are computed on read by PeriodService.quarter_summary().
    period_label
        Human-readable, unique within a FY.
        "Apr-2025" | "Q1 2025-26"
    period_start / period_end
        Inclusive date range (both ends are included in the period).
    sequence_number
        1 = first period of the FY (April), 12 = last period (March).
        Used for natural sort order without parsing the label string.
    is_closed
        True once PeriodService.close_period() has committed data.
        A closed period is read-only — re-running bills for the same
        period would create a new FiscalPeriod row (draft) until the
        accountant explicitly closes it.
    itc_carry_forward
        Convenience copy of PeriodGSTPosition.itc_carry_forward so
        callers can read the carry-forward without loading the full
        GST position object.
    net_profit
        Convenience copy of the period's net profit / loss from the
        closing computation (when available).  Positive = profit,
        negative = loss.
    books_closed
        True if close_books() was run on this period (typically only
        for the March period / last month of the FY).
    """

    __tablename__ = "fiscal_period"
    __table_args__ = (
        UniqueConstraint("financial_year", "period_label", name="uq_fy_period_label"),
    )

    id:             Mapped[str]           = mapped_column(String(36),  primary_key=True, default=_uuid)
    financial_year: Mapped[str]           = mapped_column(String(10),  nullable=False, index=True)
    period_type:    Mapped[str]           = mapped_column(String(10),  nullable=False, default="month")
    period_label:   Mapped[str]           = mapped_column(String(30),  nullable=False)
    period_start:   Mapped[date]          = mapped_column(Date,        nullable=False)
    period_end:     Mapped[date]          = mapped_column(Date,        nullable=False)
    sequence_number: Mapped[int]          = mapped_column(Integer,     nullable=False, default=0)

    is_closed:      Mapped[bool]          = mapped_column(Boolean,     default=False)
    closed_at:      Mapped[Optional[datetime]] = mapped_column(DateTime)
    closed_by:      Mapped[Optional[str]] = mapped_column(String(100))

    # Convenience summary fields populated by PeriodService
    itc_carry_forward: Mapped[float]      = mapped_column(Float, default=0.0)
    net_profit:        Mapped[float]      = mapped_column(Float, default=0.0)
    books_closed:      Mapped[bool]       = mapped_column(Boolean, default=False)
    notes:             Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=datetime.utcnow)

    # Relationships
    account_balances: Mapped[List["PeriodAccountBalance"]] = relationship(
        back_populates="period",
        cascade="all, delete-orphan",
        order_by="PeriodAccountBalance.account_name",
    )
    gst_position:  Mapped[Optional["PeriodGSTPosition"]]  = relationship(
        back_populates="period", uselist=False, cascade="all, delete-orphan"
    )
    tds_position:  Mapped[Optional["PeriodTDSPosition"]]  = relationship(
        back_populates="period", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        status = "CLOSED" if self.is_closed else "open"
        return f"<FiscalPeriod {self.period_label!r} [{self.financial_year}] {status}>"


class PeriodAccountBalance(Base):
    """
    Closing balance for one ledger account at the end of a FiscalPeriod.

    One row per (period, account_name).  Populated from
    GeneralLedger.accounts after all entries for the period are posted.

    Columns
    -------
    account_name
        Matches Account.name / LedgerAccount.name in the in-memory GL.
        E.g. "Cash A/c", "Input CGST A/c", "Sundry Creditors A/c".
    account_group
        AccountGroup.value string, e.g. "Bank Accounts", "Duties & Taxes".
        Stored here so the opening-balance loader can reconstruct the
        Account object without querying the account table.
    total_debits / total_credits
        Cumulative YTD totals up to and including this period end.
        These are the running LedgerAccount totals, NOT the month-only
        movements.  Monthly movements are derived by subtracting the
        previous period's totals.
    closing_balance
        Absolute value of the closing balance (always positive).
    balance_side
        "Dr" if the account has a debit balance (assets, expenses),
        "Cr" if it has a credit balance (liabilities, income, capital).
    is_nominal
        True for P&L accounts (Sales, Purchases, Expenses, Income).
        These balances are carried forward within the FY but zeroed out
        at year-end close.  False for balance-sheet accounts.
    """

    __tablename__ = "period_account_balance"
    __table_args__ = (
        UniqueConstraint("period_id", "account_name", name="uq_period_account"),
    )

    id:            Mapped[str]  = mapped_column(String(36),  primary_key=True, default=_uuid)
    period_id:     Mapped[str]  = mapped_column(ForeignKey("fiscal_period.id"), nullable=False, index=True)
    account_name:  Mapped[str]  = mapped_column(String(255), nullable=False)
    account_group: Mapped[str]  = mapped_column(String(100), nullable=False)
    total_debits:  Mapped[float] = mapped_column(Float, default=0.0)
    total_credits: Mapped[float] = mapped_column(Float, default=0.0)
    closing_balance: Mapped[float] = mapped_column(Float, default=0.0)
    balance_side:  Mapped[str]  = mapped_column(String(2),   default="Dr")
    is_nominal:    Mapped[bool] = mapped_column(Boolean,     default=False)

    # Relationships
    period: Mapped["FiscalPeriod"] = relationship(back_populates="account_balances")

    def __repr__(self) -> str:
        return (
            f"<PeriodAccountBalance {self.account_name!r} "
            f"{self.closing_balance} {self.balance_side}>"
        )


class PeriodGSTPosition(Base):
    """
    GST liability / ITC snapshot for one FiscalPeriod.

    Mirrors the GSTR-3B structure.  One row per period (one-to-one
    with FiscalPeriod).

    ITC carry-forward chain
    -----------------------
    When PeriodService.load_opening_balances() is called for period N+1,
    it reads this period's (period N) itc_carry_forward and posts it as
    an opening-balance credit to the relevant Input GST accounts.  This
    ensures the carry-forward is visible in the GL for the new period
    without re-processing any bills.

    GSTR-3B filing tracking
    -----------------------
    is_filed     : True once the return has been filed on the GST portal.
    filed_at     : Datetime of filing (set by the user / API integration).
    challan_ref  : PMT-06 challan reference for the net tax payment.
    """

    __tablename__ = "period_gst_position"

    id:        Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    period_id: Mapped[str] = mapped_column(ForeignKey("fiscal_period.id"), nullable=False, unique=True)

    # Section 3.1 — Outward supplies (output tax collected)
    output_cgst: Mapped[float] = mapped_column(Float, default=0.0)
    output_sgst: Mapped[float] = mapped_column(Float, default=0.0)
    output_igst: Mapped[float] = mapped_column(Float, default=0.0)
    output_cess: Mapped[float] = mapped_column(Float, default=0.0)

    # Section 4 — ITC available
    input_cgst: Mapped[float] = mapped_column(Float, default=0.0)
    input_sgst: Mapped[float] = mapped_column(Float, default=0.0)
    input_igst: Mapped[float] = mapped_column(Float, default=0.0)
    input_cess: Mapped[float] = mapped_column(Float, default=0.0)

    # ITC actually utilised (after cross-utilisation rules)
    cgst_itc_used: Mapped[float] = mapped_column(Float, default=0.0)
    sgst_itc_used: Mapped[float] = mapped_column(Float, default=0.0)
    igst_itc_used: Mapped[float] = mapped_column(Float, default=0.0)

    # Section 4D — Net payable
    net_cgst_payable:  Mapped[float] = mapped_column(Float, default=0.0)
    net_sgst_payable:  Mapped[float] = mapped_column(Float, default=0.0)
    net_igst_payable:  Mapped[float] = mapped_column(Float, default=0.0)
    net_total_payable: Mapped[float] = mapped_column(Float, default=0.0)

    # ITC carry-forward to next period (unused Input GST balance)
    # Broken down by type so the loader can post to the correct account
    itc_carry_forward:       Mapped[float] = mapped_column(Float, default=0.0)
    itc_carry_cgst:          Mapped[float] = mapped_column(Float, default=0.0)
    itc_carry_sgst:          Mapped[float] = mapped_column(Float, default=0.0)
    itc_carry_igst:          Mapped[float] = mapped_column(Float, default=0.0)

    # Month-only movements (derived during close; total_output - prior period total_output)
    # These power the month-wise GST ledger without re-processing bills
    monthly_output_cgst:     Mapped[float] = mapped_column(Float, default=0.0)
    monthly_output_sgst:     Mapped[float] = mapped_column(Float, default=0.0)
    monthly_output_igst:     Mapped[float] = mapped_column(Float, default=0.0)
    monthly_input_cgst:      Mapped[float] = mapped_column(Float, default=0.0)
    monthly_input_sgst:      Mapped[float] = mapped_column(Float, default=0.0)
    monthly_input_igst:      Mapped[float] = mapped_column(Float, default=0.0)
    monthly_net_payable:     Mapped[float] = mapped_column(Float, default=0.0)

    # Filing status
    is_filed:     Mapped[bool]          = mapped_column(Boolean, default=False)
    filed_at:     Mapped[Optional[datetime]] = mapped_column(DateTime)
    challan_ref:  Mapped[Optional[str]] = mapped_column(String(100))

    # Relationships
    period: Mapped["FiscalPeriod"] = relationship(back_populates="gst_position")

    def __repr__(self) -> str:
        return (
            f"<PeriodGSTPosition period={self.period_id!r} "
            f"payable={self.net_total_payable} carry={self.itc_carry_forward}>"
        )


class PeriodTDSPosition(Base):
    """
    TDS deduction summary for one FiscalPeriod.

    Aggregated from TDSRegister.entries after filtering by the period's
    date range.  One row per period.

    section_breakdown stores a JSON object keyed by section code:
        {
          "194J_b": {
            "transaction_count": 3,
            "gross_total": 150000.0,
            "tds_deducted": 15000.0,
            "tds_deposited": 15000.0,
            "pending": 0.0,
            "entries_missing_pan": 0
          },
          ...
        }
    """

    __tablename__ = "period_tds_position"

    id:        Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    period_id: Mapped[str] = mapped_column(ForeignKey("fiscal_period.id"), nullable=False, unique=True)

    total_tds_deducted:   Mapped[float] = mapped_column(Float, default=0.0)
    total_tds_deposited:  Mapped[float] = mapped_column(Float, default=0.0)
    total_tds_pending:    Mapped[float] = mapped_column(Float, default=0.0)
    total_gross_amount:   Mapped[float] = mapped_column(Float, default=0.0)
    entries_missing_pan:  Mapped[int]   = mapped_column(Integer, default=0)
    section_breakdown:    Mapped[Optional[dict]] = mapped_column(JSON)

    # Relationships
    period: Mapped["FiscalPeriod"] = relationship(back_populates="tds_position")

    def __repr__(self) -> str:
        return (
            f"<PeriodTDSPosition period={self.period_id!r} "
            f"deducted={self.total_tds_deducted} pending={self.total_tds_pending}>"
        )
