from __future__ import annotations

from datetime import date as date_, datetime
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

class BillModel(Base):

    __tablename__ = "bill"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)

    source_file: Mapped[Optional[str]] = mapped_column(String(512))
    invoice_number: Mapped[Optional[str]] = mapped_column(String(128))
    vendor_name: Mapped[Optional[str]] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="input")  # "input" | "output"
    bill_date: Mapped[Optional[date_]] = mapped_column(Date)

    raw_extracted_data: Mapped[Optional[dict]] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="bills")
    journal_entries: Mapped[List["JournalEntryModel"]] = relationship(back_populates="bill")

    def __repr__(self) -> str:
        return f"<Bill {self.invoice_number!r} vendor={self.vendor_name!r} status={self.status}>"

class JournalEntryModel(Base):

    __tablename__ = "journal_entry"
    __table_args__ = (
        UniqueConstraint("entry_id", name="uq_journal_entry_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(String(16), nullable=False)  # JournalEntry.entry_id business key
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)
    bill_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bill.id"))

    source_reconciliation_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("reconciliation_run.id")
    )

    date: Mapped[date_] = mapped_column(Date, nullable=False)
    voucher_type: Mapped[str] = mapped_column(String(64), nullable=False, default="Journal Voucher")
    narration: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    source_file: Mapped[Optional[str]] = mapped_column(String(512))
    invoice_number: Mapped[Optional[str]] = mapped_column(String(128))
    vendor_name: Mapped[Optional[str]] = mapped_column(String(255))
    direction: Mapped[Optional[str]] = mapped_column(String(16))  # "input" | "output"

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="journal_entries")
    bill: Mapped[Optional["BillModel"]] = relationship(back_populates="journal_entries")
    lines: Mapped[List["JournalLineModel"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="JournalLineModel.id"
    )
    tds_entries: Mapped[List["TDSEntryModel"]] = relationship(back_populates="journal_entry")

    @property
    def total_amount(self) -> float:
        return round(sum(l.amount for l in self.lines if l.dr_cr == "Dr"), 2)

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "date": self.date.isoformat() if self.date else None,
            "voucher_type": self.voucher_type,
            "narration": self.narration,
            "invoice_number": self.invoice_number,
            "vendor_name": self.vendor_name,
            "direction": self.direction,
            "total_amount": self.total_amount,
            "source_reconciliation_run_id": self.source_reconciliation_run_id,
            "lines": [l.to_dict() for l in self.lines],
        }

    def __repr__(self) -> str:
        return f"<JournalEntry {self.entry_id!r} {self.voucher_type!r}>"

class JournalLineModel(Base):

    __tablename__ = "journal_line"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entry.id"), nullable=False)

    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_group: Mapped[str] = mapped_column(String(100), nullable=False)
    dr_cr: Mapped[str] = mapped_column(String(2), nullable=False)  # "Dr" | "Cr"
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    narration: Mapped[Optional[str]] = mapped_column(String(512))

    entry: Mapped["JournalEntryModel"] = relationship(back_populates="lines")

    def to_dict(self) -> dict:
        return {
            "account_name": self.account_name,
            "account_group": self.account_group,
            "dr_cr": self.dr_cr,
            "amount": self.amount,
            "narration": self.narration,
        }

class TDSEntryModel(Base):

    __tablename__ = "tds_entry"
    __table_args__ = (
        UniqueConstraint("entry_id", name="uq_tds_entry_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(String(16), nullable=False)  # TDSEntry.entry_id business key
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)
    journal_entry_id: Mapped[Optional[int]] = mapped_column(ForeignKey("journal_entry.id"))

    date: Mapped[date_] = mapped_column(Date, nullable=False)
    section_code: Mapped[str] = mapped_column(String(16), nullable=False)

    deductee_name: Mapped[str] = mapped_column(String(255), nullable=False)
    deductee_pan: Mapped[Optional[str]] = mapped_column(String(16))
    deductee_type: Mapped[str] = mapped_column(String(32), nullable=False, default="Unknown")
    deductee_gstin: Mapped[Optional[str]] = mapped_column(String(16))

    gross_amount: Mapped[float] = mapped_column(Float, nullable=False)
    tds_base: Mapped[float] = mapped_column(Float, nullable=False)
    tds_rate: Mapped[float] = mapped_column(Float, nullable=False)
    tds_amount: Mapped[float] = mapped_column(Float, nullable=False)
    net_payment: Mapped[float] = mapped_column(Float, nullable=False)

    invoice_number: Mapped[Optional[str]] = mapped_column(String(128))
    rate_enhanced_206aa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    deposit_date: Mapped[Optional[date_]] = mapped_column(Date)
    challan_bsr_code: Mapped[Optional[str]] = mapped_column(String(16))
    challan_serial: Mapped[Optional[str]] = mapped_column(String(32))
    challan_date: Mapped[Optional[date_]] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="tds_entries")
    journal_entry: Mapped[Optional["JournalEntryModel"]] = relationship(back_populates="tds_entries")

    @property
    def is_deposited(self) -> bool:
        return self.deposit_date is not None

    @property
    def status(self) -> str:
        return "Deposited" if self.is_deposited else "Deducted"

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "date": self.date.isoformat() if self.date else None,
            "section_code": self.section_code,
            "deductee_name": self.deductee_name,
            "deductee_pan": self.deductee_pan or "PANNOTAVBL",
            "deductee_type": self.deductee_type,
            "gross_amount": self.gross_amount,
            "tds_base": self.tds_base,
            "tds_rate": self.tds_rate,
            "tds_amount": self.tds_amount,
            "net_payment": self.net_payment,
            "invoice_number": self.invoice_number,
            "rate_enhanced_206aa": self.rate_enhanced_206aa,
            "status": self.status,
            "deposit_date": self.deposit_date.isoformat() if self.deposit_date else None,
            "challan_bsr_code": self.challan_bsr_code,
            "challan_serial": self.challan_serial,
        }

    def __repr__(self) -> str:
        return f"<TDSEntry {self.entry_id!r} section={self.section_code} amount={self.tds_amount}>"

class TDSAggregateModel(Base):

    __tablename__ = "tds_aggregate"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "deductee_key", "section_code", "financial_year",
            name="uq_tds_aggregate_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)

    deductee_key: Mapped[str] = mapped_column(String(255), nullable=False)  # deductee_name.lower().strip()
    section_code: Mapped[str] = mapped_column(String(16), nullable=False)
    financial_year: Mapped[str] = mapped_column(String(10), nullable=False)  # "2025-26"

    running_gross: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<TDSAggregate {self.deductee_key!r} {self.section_code} "
            f"FY{self.financial_year} running={self.running_gross}>"
        )


class GSTR1RecordModel(Base):

    __tablename__ = "gstr1_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)

    period_label: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # "Apr-2025"
    table_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # "b2b" | "b2c_large" | "nil_rated" | "hsn_summary"

    invoice_number: Mapped[Optional[str]] = mapped_column(String(128))
    invoice_date: Mapped[Optional[date_]] = mapped_column(Date)
    invoice_value: Mapped[Optional[float]] = mapped_column(Float)

    recipient_gstin: Mapped[Optional[str]] = mapped_column(String(16))
    recipient_name: Mapped[Optional[str]] = mapped_column(String(255))
    place_of_supply: Mapped[Optional[str]] = mapped_column(String(64))

    hsn_code: Mapped[Optional[str]] = mapped_column(String(16))
    description: Mapped[Optional[str]] = mapped_column(String(255))
    tax_rate: Mapped[Optional[float]] = mapped_column(Float)

    taxable_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    igst: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cgst: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sgst: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cess: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    raw: Mapped[Optional[dict]] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="gstr1_records")

    def to_dict(self) -> dict:
        return {
            "period_label": self.period_label,
            "table_type": self.table_type,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date.isoformat() if self.invoice_date else None,
            "invoice_value": self.invoice_value,
            "recipient_gstin": self.recipient_gstin,
            "recipient_name": self.recipient_name,
            "place_of_supply": self.place_of_supply,
            "hsn_code": self.hsn_code,
            "description": self.description,
            "tax_rate": self.tax_rate,
            "taxable_value": self.taxable_value,
            "igst": self.igst,
            "cgst": self.cgst,
            "sgst": self.sgst,
            "cess": self.cess,
        }

    def __repr__(self) -> str:
        return f"<GSTR1Record {self.table_type} period={self.period_label!r}>"
