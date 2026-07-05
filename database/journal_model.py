from __future__ import annotations

import enum
from datetime import date as date_, datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class DrCr(str, enum.Enum):
    DEBIT = "Dr"
    CREDIT = "Cr"


class FiscalPeriodModel(Base):

    __tablename__ = "fiscal_period"
    __table_args__ = (
        UniqueConstraint("label", name="uq_fiscal_period_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    label: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "2026-03"
    start_date: Mapped[date_] = mapped_column(Date, nullable=False)
    end_date: Mapped[date_] = mapped_column(Date, nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    journal_entries: Mapped[List["JournalEntryModel"]] = relationship(back_populates="period")
    account_balances: Mapped[List["PeriodAccountBalanceModel"]] = relationship(back_populates="period")


class JournalEntryModel(Base):

    __tablename__ = "journal_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("fiscal_period.id"), nullable=False)

    entry_date: Mapped[date_] = mapped_column(Date, nullable=False)
    voucher_type: Mapped[str] = mapped_column(String(64), nullable=False, default="Journal Voucher")
    narration: Mapped[str] = mapped_column(Text, nullable=False, default="")

    is_reconciliation_entry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    source_match_result_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("match_result.id")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    period: Mapped["FiscalPeriodModel"] = relationship(back_populates="journal_entries")
    lines: Mapped[List["EntryLineModel"]] = relationship(
        back_populates="journal_entry", cascade="all, delete-orphan"
    )


class EntryLineModel(Base):

    __tablename__ = "entry_line"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entry.id"), nullable=False)

    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dr_cr: Mapped[DrCr] = mapped_column(
        SAEnum(DrCr, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    narration: Mapped[str] = mapped_column(Text, nullable=False, default="")

    journal_entry: Mapped["JournalEntryModel"] = relationship(back_populates="lines")


class PeriodAccountBalanceModel(Base):

    __tablename__ = "period_account_balance"
    __table_args__ = (
        UniqueConstraint("period_id", "account_name", name="uq_period_account"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("fiscal_period.id"), nullable=False)

    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normal_balance: Mapped[DrCr] = mapped_column(
        SAEnum(DrCr, values_callable=lambda e: [m.value for m in e]), nullable=False
    )

    opening_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    closing_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    net_movement: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    period: Mapped["FiscalPeriodModel"] = relationship(back_populates="account_balances")
