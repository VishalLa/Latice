from __future__ import annotations

import enum
from datetime import date as date_, datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from .base import Base


class LedgerSource(str, enum.Enum):
    AUTO = "auto"      # bill scan -> journal entry -> ledger
    MANUAL = "manual"  # user-uploaded ledger CSV


class LedgerFormatModel(Base):
    """One reconcilable row of the company's ledger (AUTO or MANUAL path)."""

    __tablename__ = "ledger_format"
    __table_args__ = (
        UniqueConstraint("ledger_id", name="uq_ledger_format_ledger_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Business key used by the matchers (e.g. "L0001"); unique but not the PK
    # so re-imports / re-runs don't collide with autoincrement.
    ledger_id: Mapped[str] = mapped_column(String(32), nullable=False)

    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_number: Mapped[Optional[str]] = mapped_column(String(64))

    transaction_date: Mapped[Optional[date_]] = mapped_column(Date)
    transaction_date_raw: Mapped[Optional[str]] = mapped_column(String(64))

    debit_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    credit_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    reference_id: Mapped[Optional[str]] = mapped_column(String(128))

    parse_warnings: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)

    source: Mapped[LedgerSource] = mapped_column(
        SAEnum(LedgerSource, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=LedgerSource.MANUAL,
    )

    journal_entry_id: Mapped[Optional[str]] = mapped_column(String(64))
    voucher_type: Mapped[Optional[str]] = mapped_column(String(64))
    vendor_name: Mapped[Optional[str]] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # ── convenience properties mirroring the dataclass's is_credit/is_debit ──
    @property
    def is_credit(self) -> bool:
        return self.credit_amount > 0.0 and self.debit_amount == 0.0

    @property
    def is_debit(self) -> bool:
        return self.debit_amount > 0.0 and self.credit_amount == 0.0

    @property
    def is_auto(self) -> bool:
        return self.source == LedgerSource.AUTO

    @property
    def is_manual(self) -> bool:
        return self.source == LedgerSource.MANUAL

    def to_dict(self) -> dict:
        return {
            "ledger_id": self.ledger_id,
            "source": self.source.value,
            "account_name": self.account_name,
            "account_number": self.account_number,
            "transaction_date": self.transaction_date.isoformat() if self.transaction_date else None,
            "transaction_date_raw": self.transaction_date_raw,
            "debit_amount": self.debit_amount,
            "credit_amount": self.credit_amount,
            "reference_id": self.reference_id,
            "voucher_type": self.voucher_type,
            "vendor_name": self.vendor_name,
            "journal_entry_id": self.journal_entry_id,
            "parse_warnings": self.parse_warnings,
        }


class BankStatementModel(Base):
    """One row parsed from a bank CSV export."""

    __tablename__ = "bank_statement"
    __table_args__ = (
        UniqueConstraint("row_index", "bank_name", "template_version",
                          name="uq_bank_statement_row"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Business key used by the matchers (bank.row_index)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)

    bank_name: Mapped[str] = mapped_column(String(128), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)

    date: Mapped[Optional[date_]] = mapped_column(Date)          # ISO YYYY-MM-DD
    date_raw: Mapped[Optional[str]] = mapped_column(String(64))

    narration: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    debit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)   # money out
    credit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # money in
    balance: Mapped[Optional[float]] = mapped_column(Float)

    txn_id: Mapped[Optional[str]] = mapped_column(String(128))

    parse_warnings: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class IgnoredMetadataRecordModel(Base):
    """A zero-amount record silently dropped before matching begins."""

    __tablename__ = "ignored_metadata_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    source: Mapped[str] = mapped_column(String(16), nullable=False)     # "bank" | "ledger"
    row_ref: Mapped[str] = mapped_column(String(64), nullable=False)    # row_index or ledger_id
    narration: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    reason: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="Zero-amount metadata / header row — excluded from reconciliation.",
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MatchPatternModel(Base):

    __tablename__ = "match_pattern"
    __table_args__ = (
        UniqueConstraint("pattern_key", name="uq_match_pattern_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    pattern_key: Mapped[str] = mapped_column(String(512), nullable=False)

    ledger_signature: Mapped[str] = mapped_column(String(255), nullable=False)
    bank_signature: Mapped[str] = mapped_column(String(255), nullable=False)

    match_phase: Mapped[str] = mapped_column(String(16), nullable=False)  # "exact"|"fuzzy"|"ai"|"memory"
    adjustment_type: Mapped[Optional[str]] = mapped_column(String(128))

    times_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict:
        return {
            "ledger_signature": self.ledger_signature,
            "bank_signature": self.bank_signature,
            "match_phase": self.match_phase,
            "adjustment_type": self.adjustment_type,
            "times_seen": self.times_seen,
        }


class AuditInvestigationItemModel(Base):
    """A bank row flagged for manual GL journal entry; not force-matched."""

    __tablename__ = "audit_investigation_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    bank_row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    narration: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)  # "debit" | "credit"
    flag_reason: Mapped[str] = mapped_column(String(255), nullable=False)
    action_required: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="Bank Reversal detected; requires manual General Ledger journal entry.",
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
