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

    itc_carry_forward: Mapped[float]      = mapped_column(Float, default=0.0)
    net_profit:        Mapped[float]      = mapped_column(Float, default=0.0)
    books_closed:      Mapped[bool]       = mapped_column(Boolean, default=False)
    notes:             Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=datetime.utcnow)

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

    __tablename__ = "period_gst_position"

    id:        Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    period_id: Mapped[str] = mapped_column(ForeignKey("fiscal_period.id"), nullable=False, unique=True)

    output_cgst: Mapped[float] = mapped_column(Float, default=0.0)
    output_sgst: Mapped[float] = mapped_column(Float, default=0.0)
    output_igst: Mapped[float] = mapped_column(Float, default=0.0)
    output_cess: Mapped[float] = mapped_column(Float, default=0.0)

    input_cgst: Mapped[float] = mapped_column(Float, default=0.0)
    input_sgst: Mapped[float] = mapped_column(Float, default=0.0)
    input_igst: Mapped[float] = mapped_column(Float, default=0.0)
    input_cess: Mapped[float] = mapped_column(Float, default=0.0)

    cgst_itc_used: Mapped[float] = mapped_column(Float, default=0.0)
    sgst_itc_used: Mapped[float] = mapped_column(Float, default=0.0)
    igst_itc_used: Mapped[float] = mapped_column(Float, default=0.0)

    net_cgst_payable:  Mapped[float] = mapped_column(Float, default=0.0)
    net_sgst_payable:  Mapped[float] = mapped_column(Float, default=0.0)
    net_igst_payable:  Mapped[float] = mapped_column(Float, default=0.0)
    net_total_payable: Mapped[float] = mapped_column(Float, default=0.0)

    itc_carry_forward:       Mapped[float] = mapped_column(Float, default=0.0)
    itc_carry_cgst:          Mapped[float] = mapped_column(Float, default=0.0)
    itc_carry_sgst:          Mapped[float] = mapped_column(Float, default=0.0)
    itc_carry_igst:          Mapped[float] = mapped_column(Float, default=0.0)

    monthly_output_cgst:     Mapped[float] = mapped_column(Float, default=0.0)
    monthly_output_sgst:     Mapped[float] = mapped_column(Float, default=0.0)
    monthly_output_igst:     Mapped[float] = mapped_column(Float, default=0.0)
    monthly_input_cgst:      Mapped[float] = mapped_column(Float, default=0.0)
    monthly_input_sgst:      Mapped[float] = mapped_column(Float, default=0.0)
    monthly_input_igst:      Mapped[float] = mapped_column(Float, default=0.0)
    monthly_net_payable:     Mapped[float] = mapped_column(Float, default=0.0)

    is_filed:     Mapped[bool]          = mapped_column(Boolean, default=False)
    filed_at:     Mapped[Optional[datetime]] = mapped_column(DateTime)
    challan_ref:  Mapped[Optional[str]] = mapped_column(String(100))

    period: Mapped["FiscalPeriod"] = relationship(back_populates="gst_position")

    def __repr__(self) -> str:
        return (
            f"<PeriodGSTPosition period={self.period_id!r} "
            f"payable={self.net_total_payable} carry={self.itc_carry_forward}>"
        )


class PeriodTDSPosition(Base):

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
