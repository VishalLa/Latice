from __future__ import annotations

import uuid
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
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Bill Scanning ─────────────────────────────────────────────────────────────

class ScannedBill(Base):
    """
    A bill / invoice parsed from an image by OCR.
    One bill generates at most one JournalEntry (||--o|).
    """
    __tablename__ = "scanned_bill"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    source_file:    Mapped[Optional[str]]   = mapped_column(String(255))
    vendor_name:    Mapped[Optional[str]]   = mapped_column(String(255))
    vendor_gstin:   Mapped[Optional[str]]   = mapped_column(String(15))
    invoice_number: Mapped[Optional[str]]   = mapped_column(String(100))
    invoice_date:   Mapped[Optional[date]]  = mapped_column(Date)
    bill_type:      Mapped[Optional[str]]   = mapped_column(String(50))
    buyer_name:     Mapped[Optional[str]]   = mapped_column(String(255))
    buyer_gstin:    Mapped[Optional[str]]   = mapped_column(String(15))
    place_of_supply: Mapped[Optional[str]]  = mapped_column(String(100))

    subtotal:       Mapped[float] = mapped_column(Float, default=0.0)
    taxable_amount: Mapped[float] = mapped_column(Float, default=0.0)
    cgst_rate:      Mapped[float] = mapped_column(Float, default=0.0)
    cgst_amount:    Mapped[float] = mapped_column(Float, default=0.0)
    sgst_rate:      Mapped[float] = mapped_column(Float, default=0.0)
    sgst_amount:    Mapped[float] = mapped_column(Float, default=0.0)
    igst_rate:      Mapped[float] = mapped_column(Float, default=0.0)
    igst_amount:    Mapped[float] = mapped_column(Float, default=0.0)
    cess_amount:    Mapped[float] = mapped_column(Float, default=0.0)
    other_charges:  Mapped[float] = mapped_column(Float, default=0.0)
    discount:       Mapped[float] = mapped_column(Float, default=0.0)
    round_off:      Mapped[float] = mapped_column(Float, default=0.0)
    total_tax:      Mapped[float] = mapped_column(Float, default=0.0)
    grand_total:    Mapped[float] = mapped_column(Float, default=0.0)

    amount_in_words: Mapped[Optional[str]] = mapped_column(Text)
    payment_mode:    Mapped[Optional[str]] = mapped_column(String(50))
    return_type:     Mapped[Optional[str]] = mapped_column(String(50))
    direction:       Mapped[Optional[str]] = mapped_column(String(10))  # "input" | "output"

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), default=datetime.utcnow
    )

    # Relationships
    line_items:    Mapped[List["BillLineItem"]] = relationship(
        back_populates="bill", cascade="all, delete-orphan"
    )
    journal_entry: Mapped[Optional["JournalEntry"]] = relationship(
        back_populates="bill", uselist=False
    )

    def __repr__(self) -> str:
        return f"<ScannedBill {self.invoice_number!r} vendor={self.vendor_name!r}>"


class BillLineItem(Base):
    """Individual line item belonging to a ScannedBill."""
    __tablename__ = "bill_line_item"

    id:          Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    bill_id:     Mapped[str] = mapped_column(ForeignKey("scanned_bill.id"), nullable=False)

    description: Mapped[Optional[str]]   = mapped_column(Text)
    hsn_sac:     Mapped[Optional[str]]   = mapped_column(String(20))
    quantity:    Mapped[Optional[float]] = mapped_column(Float)
    unit:        Mapped[Optional[str]]   = mapped_column(String(20))
    rate:        Mapped[float]           = mapped_column(Float, default=0.0)
    amount:      Mapped[float]           = mapped_column(Float, default=0.0)
    sort_order:  Mapped[int]             = mapped_column(Integer, default=0)

    # Relationships
    bill: Mapped["ScannedBill"] = relationship(back_populates="line_items")

    def __repr__(self) -> str:
        return f"<BillLineItem {self.description!r} amount={self.amount}>"


# ── Chart of Accounts ─────────────────────────────────────────────────────────

class Account(Base):
    """
    A single ledger account in the Chart of Accounts.
    Maps to the Account dataclass in unified_schema.py.
    """
    __tablename__ = "account"

    id:       Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    name:     Mapped[str]           = mapped_column(String(255), nullable=False, unique=True)
    group:    Mapped[str]           = mapped_column(String(100), nullable=False)
    gst_code: Mapped[Optional[str]] = mapped_column(String(10))  # "CGST" | "SGST" | "IGST" | "NET"

    # Relationships
    entry_lines:    Mapped[List["EntryLine"]]    = relationship(back_populates="account")
    ledger_account: Mapped[Optional["LedgerAccount"]] = relationship(
        back_populates="account", uselist=False
    )

    def __repr__(self) -> str:
        return f"<Account {self.name!r} [{self.group}]>"


# ── Journal ───────────────────────────────────────────────────────────────────

class JournalEntry(Base):
    """
    Double-entry journal record generated from a ScannedBill.
    One entry holds multiple EntryLines (debits + credits).
    """
    __tablename__ = "journal_entry"

    id:      Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    bill_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("scanned_bill.id"), nullable=True
    )

    entry_id:       Mapped[str]           = mapped_column(String(20), unique=True, nullable=False)
    date:           Mapped[date]          = mapped_column(Date, nullable=False)
    voucher_type:   Mapped[str]           = mapped_column(String(100), default="Journal Voucher")
    narration:      Mapped[Optional[str]] = mapped_column(Text)
    source_file:    Mapped[Optional[str]] = mapped_column(String(255))
    invoice_number: Mapped[Optional[str]] = mapped_column(String(100))
    vendor_name:    Mapped[Optional[str]] = mapped_column(String(255))
    direction:      Mapped[Optional[str]] = mapped_column(String(10))  # "input" | "output"
    total_amount:   Mapped[float]         = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), default=datetime.utcnow
    )

    # Relationships
    bill:           Mapped[Optional["ScannedBill"]]  = relationship(back_populates="journal_entry")
    lines:          Mapped[List["EntryLine"]]         = relationship(
        back_populates="journal_entry", cascade="all, delete-orphan",
        order_by="EntryLine.sort_order",
    )
    ledger_postings: Mapped[List["LedgerPosting"]]   = relationship(back_populates="journal_entry")
    ledger_record:   Mapped[Optional["LedgerRecord"]] = relationship(
        back_populates="journal_entry", uselist=False
    )

    def __repr__(self) -> str:
        return f"<JournalEntry {self.entry_id!r} date={self.date} amount={self.total_amount}>"


class EntryLine(Base):
    """
    One debit or credit line within a JournalEntry.
    Many lines belong to one entry; each line posts to one Account.
    """
    __tablename__ = "entry_line"

    id:               Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    journal_entry_id: Mapped[str] = mapped_column(
        ForeignKey("journal_entry.id"), nullable=False
    )
    account_id:       Mapped[str] = mapped_column(
        ForeignKey("account.id"), nullable=False
    )

    dr_cr:      Mapped[str]           = mapped_column(String(2), nullable=False)  # "Dr" | "Cr"
    amount:     Mapped[float]         = mapped_column(Float, nullable=False)
    narration:  Mapped[Optional[str]] = mapped_column(Text)
    sort_order: Mapped[int]           = mapped_column(Integer, default=0)

    # Relationships
    journal_entry: Mapped["JournalEntry"] = relationship(back_populates="lines")
    account:       Mapped["Account"]      = relationship(back_populates="entry_lines")

    def __repr__(self) -> str:
        return f"<EntryLine {self.dr_cr} {self.amount} → account_id={self.account_id!r}>"


# ── General Ledger ────────────────────────────────────────────────────────────

class LedgerAccount(Base):
    """
    Running totals for one Account — the ledger folio.
    One Account has at most one LedgerAccount (||--o|).
    """
    __tablename__ = "ledger_account"

    id:         Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("account.id"), nullable=False, unique=True
    )

    total_debits:    Mapped[float] = mapped_column(Float, default=0.0)
    total_credits:   Mapped[float] = mapped_column(Float, default=0.0)
    closing_balance: Mapped[float] = mapped_column(Float, default=0.0)
    balance_side:    Mapped[str]   = mapped_column(String(2), default="Dr")  # "Dr" | "Cr"

    # Relationships
    account:  Mapped["Account"]           = relationship(back_populates="ledger_account")
    postings: Mapped[List["LedgerPosting"]] = relationship(
        back_populates="ledger_account", cascade="all, delete-orphan",
        order_by="LedgerPosting.date",
    )

    def __repr__(self) -> str:
        return (
            f"<LedgerAccount account_id={self.account_id!r} "
            f"balance={self.closing_balance} {self.balance_side}>"
        )


class LedgerPosting(Base):
    """
    One row in a ledger folio — a single movement on a LedgerAccount
    sourced from a JournalEntry.
    """
    __tablename__ = "ledger_posting"

    id:                Mapped[str]  = mapped_column(String(36), primary_key=True, default=_uuid)
    ledger_account_id: Mapped[str]  = mapped_column(
        ForeignKey("ledger_account.id"), nullable=False
    )
    journal_entry_id:  Mapped[str]  = mapped_column(
        ForeignKey("journal_entry.id"), nullable=False
    )

    date:         Mapped[date]          = mapped_column(Date, nullable=False)
    particulars:  Mapped[Optional[str]] = mapped_column(Text)
    voucher_type: Mapped[Optional[str]] = mapped_column(String(100))
    dr_amount:    Mapped[float]         = mapped_column(Float, default=0.0)
    cr_amount:    Mapped[float]         = mapped_column(Float, default=0.0)
    balance:      Mapped[float]         = mapped_column(Float, default=0.0)
    balance_side: Mapped[str]           = mapped_column(String(2), default="Dr")

    # Relationships
    ledger_account: Mapped["LedgerAccount"] = relationship(back_populates="postings")
    journal_entry:  Mapped["JournalEntry"]  = relationship(back_populates="ledger_postings")

    def __repr__(self) -> str:
        return (
            f"<LedgerPosting date={self.date} dr={self.dr_amount} "
            f"cr={self.cr_amount} bal={self.balance} {self.balance_side}>"
        )


# ── Bank Reconciliation — Input ───────────────────────────────────────────────

class BankTemplate(Base):
    """
    Column-mapping descriptor for a specific bank's CSV/XLSX export format.
    One template can parse many BankStatements.
    """
    __tablename__ = "bank_template"
    __table_args__ = (UniqueConstraint("bank_name", "version", name="uq_bank_version"),)

    id:      Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    bank_name:        Mapped[str]           = mapped_column(String(100), nullable=False)
    version:          Mapped[str]           = mapped_column(String(20), default="v1")
    file_type:        Mapped[str]           = mapped_column(String(10), default="csv")
    skip_rows:        Mapped[int]           = mapped_column(Integer, default=0)
    encoding:         Mapped[str]           = mapped_column(String(20), default="utf-8")
    date_column:      Mapped[str]           = mapped_column(String(50), nullable=False)
    date_format:      Mapped[str]           = mapped_column(String(50), nullable=False)
    narration_column: Mapped[str]           = mapped_column(String(50), nullable=False)
    debit_column:     Mapped[Optional[str]] = mapped_column(String(50))
    credit_column:    Mapped[Optional[str]] = mapped_column(String(50))
    txn_id_column:    Mapped[Optional[str]] = mapped_column(String(50))
    balance_column:   Mapped[Optional[str]] = mapped_column(String(50))
    type_column:      Mapped[Optional[str]] = mapped_column(String(50))
    amount_column:    Mapped[Optional[str]] = mapped_column(String(50))
    fingerprint:      Mapped[Optional[dict]] = mapped_column(JSON)  # Set[str] of header fingerprints

    # Relationships
    bank_statements:      Mapped[List["BankStatement"]]      = relationship(back_populates="template")
    reconciliation_runs:  Mapped[List["ReconciliationRun"]]  = relationship(back_populates="template")

    def __repr__(self) -> str:
        return f"<BankTemplate {self.bank_name!r} {self.version}>"


class BankStatement(Base):
    """One row parsed from a bank CSV export using a BankTemplate."""
    __tablename__ = "bank_statement"

    id:          Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    template_id: Mapped[str] = mapped_column(ForeignKey("bank_template.id"), nullable=False)

    row_index:      Mapped[int]            = mapped_column(Integer, nullable=False)
    date:           Mapped[Optional[date]] = mapped_column(Date)
    date_raw:       Mapped[Optional[str]]  = mapped_column(String(50))
    narration:      Mapped[str]            = mapped_column(Text, default="")
    debit:          Mapped[float]          = mapped_column(Float, default=0.0)
    credit:         Mapped[float]          = mapped_column(Float, default=0.0)
    balance:        Mapped[Optional[float]] = mapped_column(Float)
    txn_id:         Mapped[Optional[str]]  = mapped_column(String(100))
    parse_warnings: Mapped[Optional[list]] = mapped_column(JSON)

    # Relationships
    template:      Mapped["BankTemplate"]        = relationship(back_populates="bank_statements")
    match_results: Mapped[List["MatchResult"]]   = relationship(back_populates="bank_statement")

    def __repr__(self) -> str:
        return (
            f"<BankStatement row={self.row_index} date={self.date} "
            f"debit={self.debit} credit={self.credit}>"
        )


# ── Bank Reconciliation — Ledger Input ────────────────────────────────────────

class LedgerRecord(Base):
    """
    One reconcilable row of the company ledger.
    Either auto-derived from a JournalEntry (source='auto')
    or loaded from a user-uploaded CSV (source='manual').
    """
    __tablename__ = "ledger_record"

    id:               Mapped[str]          = mapped_column(String(36), primary_key=True, default=_uuid)
    journal_entry_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("journal_entry.id"), nullable=True  # NULL for manual records
    )

    ledger_id:            Mapped[str]            = mapped_column(String(20), nullable=False, unique=True)
    source:               Mapped[str]            = mapped_column(String(10), nullable=False)  # "auto" | "manual"
    account_name:         Mapped[str]            = mapped_column(String(255), nullable=False)
    account_number:       Mapped[Optional[str]]  = mapped_column(String(50))
    transaction_date:     Mapped[Optional[date]] = mapped_column(Date)
    transaction_date_raw: Mapped[Optional[str]]  = mapped_column(String(50))
    debit_amount:         Mapped[float]          = mapped_column(Float, default=0.0)
    credit_amount:        Mapped[float]          = mapped_column(Float, default=0.0)
    reference_id:         Mapped[Optional[str]]  = mapped_column(String(100))
    voucher_type:         Mapped[Optional[str]]  = mapped_column(String(100))
    vendor_name:          Mapped[Optional[str]]  = mapped_column(String(255))
    parse_warnings:       Mapped[Optional[list]] = mapped_column(JSON)

    # Relationships
    journal_entry: Mapped[Optional["JournalEntry"]] = relationship(back_populates="ledger_record")
    match_results: Mapped[List["MatchResult"]]       = relationship(back_populates="ledger_record")

    def __repr__(self) -> str:
        return (
            f"<LedgerRecord {self.ledger_id!r} source={self.source!r} "
            f"account={self.account_name!r}>"
        )


# ── Reconciliation Run & Results ──────────────────────────────────────────────

class ReconciliationRun(Base):
    """
    One complete run of the reconciliation pipeline (all three phases).
    Stores summary counters and links to every MatchResult it produced.
    """
    __tablename__ = "reconciliation_run"

    id:          Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    template_id: Mapped[str] = mapped_column(ForeignKey("bank_template.id"), nullable=False)

    ledger_source:    Mapped[Optional[str]] = mapped_column(String(10))   # "auto" | "manual"
    bank_name:        Mapped[Optional[str]] = mapped_column(String(100))
    template_version: Mapped[Optional[str]] = mapped_column(String(20))
    bank_csv_path:    Mapped[Optional[str]] = mapped_column(String(500))
    ledger_csv_path:  Mapped[Optional[str]] = mapped_column(String(500))

    ledger_records:      Mapped[int] = mapped_column(Integer, default=0)
    bank_records:        Mapped[int] = mapped_column(Integer, default=0)
    exact_matches:       Mapped[int] = mapped_column(Integer, default=0)
    fuzzy_matches:       Mapped[int] = mapped_column(Integer, default=0)
    ai_matches:          Mapped[int] = mapped_column(Integer, default=0)
    unreconciled_ledger: Mapped[int] = mapped_column(Integer, default=0)
    unreconciled_bank:   Mapped[int] = mapped_column(Integer, default=0)

    run_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), default=datetime.utcnow
    )

    # Relationships
    template:      Mapped["BankTemplate"]       = relationship(back_populates="reconciliation_runs")
    match_results: Mapped[List["MatchResult"]]  = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    gst_summary:   Mapped[Optional["GSTSummary"]] = relationship(
        back_populates="run", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<ReconciliationRun id={self.id!r} bank={self.bank_name!r} "
            f"exact={self.exact_matches} fuzzy={self.fuzzy_matches} "
            f"ai={self.ai_matches}>"
        )


class MatchResult(Base):
    """
    One matched pair (or unmatched item) produced by a ReconciliationRun.
    match_type distinguishes exact / fuzzy / ai / unreconciled.
    bank_stmt_id and ledger_record_id can both be NULL for ledger-only
    or bank-only adjustments (zero-sum pairs, interest income, etc.).
    """
    __tablename__ = "match_result"

    id:               Mapped[str]          = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id:           Mapped[str]          = mapped_column(ForeignKey("reconciliation_run.id"), nullable=False)
    ledger_record_id: Mapped[Optional[str]] = mapped_column(ForeignKey("ledger_record.id"))
    bank_stmt_id:     Mapped[Optional[str]] = mapped_column(ForeignKey("bank_statement.id"))

    match_type:      Mapped[str]           = mapped_column(String(20), nullable=False)  # "exact" | "fuzzy" | "ai" | "ai_queue"
    adjustment_type: Mapped[Optional[str]] = mapped_column(String(100))  # e.g. "Deposit in Transit"
    confidence_score: Mapped[Optional[str]] = mapped_column(String(10))  # "High" | "Medium" | "Low"
    matched_amount:  Mapped[Optional[float]] = mapped_column(Float)
    matched_date:    Mapped[Optional[date]]  = mapped_column(Date)
    details:         Mapped[Optional[str]]   = mapped_column(Text)

    # Relationships
    run:            Mapped["ReconciliationRun"]   = relationship(back_populates="match_results")
    ledger_record:  Mapped[Optional["LedgerRecord"]] = relationship(back_populates="match_results")
    bank_statement: Mapped[Optional["BankStatement"]] = relationship(back_populates="match_results")

    def __repr__(self) -> str:
        return (
            f"<MatchResult type={self.match_type!r} conf={self.confidence_score!r} "
            f"amount={self.matched_amount}>"
        )


# ── GST ───────────────────────────────────────────────────────────────────────

class GSTSummary(Base):
    """
    GST liability / ITC summary for the period covered by a ReconciliationRun.
    Mirrors the GSTR-3B format: output tax, ITC, net payable, carry-forward.
    One run has at most one GSTSummary (||--o|).
    """
    __tablename__ = "gst_summary"

    id:     Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliation_run.id"), nullable=False, unique=True
    )

    period_label: Mapped[Optional[str]] = mapped_column(String(100))

    # Output tax collected
    output_cgst: Mapped[float] = mapped_column(Float, default=0.0)
    output_sgst: Mapped[float] = mapped_column(Float, default=0.0)
    output_igst: Mapped[float] = mapped_column(Float, default=0.0)
    output_cess: Mapped[float] = mapped_column(Float, default=0.0)

    # Input tax credit available
    input_cgst: Mapped[float] = mapped_column(Float, default=0.0)
    input_sgst: Mapped[float] = mapped_column(Float, default=0.0)
    input_igst: Mapped[float] = mapped_column(Float, default=0.0)
    input_cess: Mapped[float] = mapped_column(Float, default=0.0)

    # ITC actually utilised (after cross-utilisation rules)
    cgst_itc_used: Mapped[float] = mapped_column(Float, default=0.0)
    sgst_itc_used: Mapped[float] = mapped_column(Float, default=0.0)
    igst_itc_used: Mapped[float] = mapped_column(Float, default=0.0)

    # Net payable after ITC set-off
    net_cgst_payable:  Mapped[float] = mapped_column(Float, default=0.0)
    net_sgst_payable:  Mapped[float] = mapped_column(Float, default=0.0)
    net_igst_payable:  Mapped[float] = mapped_column(Float, default=0.0)
    net_total_payable: Mapped[float] = mapped_column(Float, default=0.0)
    itc_carry_forward: Mapped[float] = mapped_column(Float, default=0.0)

    # Relationships
    run: Mapped["ReconciliationRun"] = relationship(back_populates="gst_summary")

    def __repr__(self) -> str:
        return (
            f"<GSTSummary period={self.period_label!r} "
            f"payable={self.net_total_payable} carry_fwd={self.itc_carry_forward}>"
        )
    