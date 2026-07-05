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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from .base import Base


class LedgerSource(str, enum.Enum):
    AUTO = "auto"      
    MANUAL = "manual"  


class LedgerFormatModel(Base):

    __tablename__ = "ledger_format"
    __table_args__ = (
        UniqueConstraint("ledger_id", name="uq_ledger_format_ledger_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ledger_id: Mapped[str] = mapped_column(String(32), nullable=False)

    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_number: Mapped[Optional[str]] = mapped_column(String(64))

    transaction_date: Mapped[Optional[date_]] = mapped_column(Date)
    transaction_date_raw: Mapped[Optional[str]] = mapped_column(String(64))

    debit_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    credit_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    reference_id: Mapped[Optional[str]] = mapped_column(String(128))

    parse_warnings: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("reconciliation_run.id"))

    source: Mapped[LedgerSource] = mapped_column(
        SAEnum(LedgerSource, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=LedgerSource.MANUAL,
    )

    journal_entry_id: Mapped[Optional[str]] = mapped_column(String(64))
    voucher_type: Mapped[Optional[str]] = mapped_column(String(64))
    vendor_name: Mapped[Optional[str]] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    run: Mapped[Optional["ReconciliationRunModel"]] = relationship(back_populates="ledger_format_records")
    match_results: Mapped[List["MatchResultModel"]] = relationship(back_populates="ledger_format")

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

    __tablename__ = "bank_statement"
    __table_args__ = (
        UniqueConstraint("row_index", "bank_name", "template_version",
                          name="uq_bank_statement_row"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    row_index: Mapped[int] = mapped_column(Integer, nullable=False)

    bank_name: Mapped[str] = mapped_column(String(128), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)

    date: Mapped[Optional[date_]] = mapped_column(Date)          
    date_raw: Mapped[Optional[str]] = mapped_column(String(64))

    narration: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    debit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)   
    credit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  
    balance: Mapped[Optional[float]] = mapped_column(Float)

    txn_id: Mapped[Optional[str]] = mapped_column(String(128))

    parse_warnings: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    run: Mapped[Optional["ReconciliationRunModel"]] = relationship(back_populates="bank_statements")
    match_results: Mapped[List["MatchResultModel"]] = relationship(back_populates="bank_statement")
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("reconciliation_run.id"))


class ReconciliationRunModel(Base):

    __tablename__ = "reconciliation_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[Optional[int]] = mapped_column(Integer)

    celery_task_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)

    ledger_source: Mapped[Optional[str]] = mapped_column(String(16))
    bank_name: Mapped[Optional[str]] = mapped_column(String(128))
    template_version: Mapped[Optional[str]] = mapped_column(String(32))
    bank_csv_path: Mapped[Optional[str]] = mapped_column(String(512))
    ledger_csv_path: Mapped[Optional[str]] = mapped_column(String(512))

    ledger_records: Mapped[int] = mapped_column(Integer, default=0)
    bank_records: Mapped[int] = mapped_column(Integer, default=0)
    exact_matches: Mapped[int] = mapped_column(Integer, default=0)
    fuzzy_matches: Mapped[int] = mapped_column(Integer, default=0)
    ai_matches: Mapped[int] = mapped_column(Integer, default=0)
    unreconciled_ledger: Mapped[int] = mapped_column(Integer, default=0)
    unreconciled_bank: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[Optional[str]] = mapped_column(String(16), default="processing")
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    run_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id"))
    user: Mapped[Optional["User"]] = relationship(back_populates="reconciliation_runs")

    match_results: Mapped[List["MatchResultModel"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    bank_statements: Mapped[List["BankStatementModel"]] = relationship(back_populates="run")
    ledger_format_records: Mapped[List["LedgerFormatModel"]] = relationship(back_populates="run")


class MatchResultModel(Base):

    __tablename__ = "match_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_run.id"), nullable=False)
    ledger_format_id: Mapped[Optional[int]] = mapped_column(ForeignKey("ledger_format.id"))
    bank_statement_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bank_statement.id"))

    match_type: Mapped[str] = mapped_column(String(32), nullable=False)
    adjustment_type: Mapped[Optional[str]] = mapped_column(String(128))
    confidence_score: Mapped[Optional[str]] = mapped_column(String(32))
    matched_amount: Mapped[Optional[float]] = mapped_column(Float)
    matched_date: Mapped[Optional[date_]] = mapped_column(Date)
    details: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    run: Mapped["ReconciliationRunModel"] = relationship(back_populates="match_results")
    ledger_format: Mapped[Optional["LedgerFormatModel"]] = relationship(back_populates="match_results")
    bank_statement: Mapped[Optional["BankStatementModel"]] = relationship(back_populates="match_results")


class IgnoredMetadataRecordModel(Base):

    __tablename__ = "ignored_metadata_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    source: Mapped[str] = mapped_column(String(16), nullable=False)    
    row_ref: Mapped[str] = mapped_column(String(64), nullable=False)    
    narration: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    reason: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="Zero-amount metadata / header row - excluded from reconciliation.",
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
    