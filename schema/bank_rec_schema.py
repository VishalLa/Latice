from __future__ import annotations

from enum import Enum
from typing import List, Optional, Set, ClassVar, Any
from pydantic import Field, field_validator, model_validator
from .base import SchemaBase


class LedgerSource(str, Enum):
    """
    Discriminates between the two paths that produce a LedgerFormat record.

    AUTO   – record was derived automatically from scanned bills via build_ledger()
             / to_journal_entries().  The ledger was never a user-supplied file.
    MANUAL – record was loaded from a CSV that the user uploaded directly.
    """
    AUTO   = "auto"    # Path A: bill scan → journal entry → ledger
    MANUAL = "manual"  # Path B: user uploaded ledger CSV


class RecognizedPattern(SchemaBase):
    ledger_signature: str
    bank_signature:   str
    match_phase:      str   # "exact" | "fuzzy" | "ai"
    adjustment_type:  Optional[str]
    times_seen:       int = 1

    def to_dict(self) -> dict:
        return {
            "ledger_signature": self.ledger_signature,
            "bank_signature":   self.bank_signature,
            "match_phase":      self.match_phase,
            "adjustment_type":  self.adjustment_type,
            "times_seen":       self.times_seen,
        }

    @staticmethod
    def from_dict(d: dict) -> "RecognizedPattern":
        return RecognizedPattern(
            ledger_signature=d.get("ledger_signature", ""),
            bank_signature=d.get("bank_signature", ""),
            match_phase=d.get("match_phase", "fuzzy"),
            adjustment_type=d.get("adjustment_type"),
            times_seen=int(d.get("times_seen", 1)),
        )


class MatchedPair(SchemaBase):
    gl_record:    LedgerFormat
    bank_record:  BankStatement
    gl_amount:    float
    bank_amount:  float
    orientation:  str   # "same_side" or "opposite_side"
    amount_type:  str   # e.g. "debit-debit", "credit-debit", ...


class SameSideDetection(SchemaBase):
    same_side:    bool
    confident:    bool
    sample_size:  int
    reason:       str
    matched_pairs: List[MatchedPair] = Field(default_factory=list)


class BankTemplate(SchemaBase):
    """Column-mapping descriptor for a specific bank's CSV export format."""
    bank_name:        str
    version:          str
    date_column:      str
    date_format:      str
    narration_column: str

    file_type:        str          = "csv"
    skip_rows:        int          = 0
    encoding:         str          = "utf-8"
    debit_column:     Optional[str] = None
    credit_column:    Optional[str] = None
    txn_id_column:    Optional[str] = None
    balance_column:   Optional[str] = None
    type_column:      Optional[str] = None
    amount_column:    Optional[str] = None
    fingerprint:      Set[str]      = Field(default_factory=set)

    def to_dict(self) -> dict:
        return self.model_dump()


class BankStatement(SchemaBase):
    """One row parsed from a bank CSV export."""
    row_index:        int
    bank_name:        str
    template_version: str

    date:             Optional[str]   = None   # ISO YYYY-MM-DD
    date_raw:         Optional[str]   = None
    narration:        str             = ""
    debit_amount:     float           = 0.0    # Money leaving the account
    credit_amount:    float           = 0.0    # Money entering the account
    balance:          Optional[float] = None
    txn_id:           Optional[str]   = None
    parse_warnings:   List[str]       = Field(default_factory=list)
    run_id:           Optional[str]   = None

    @field_validator("debit_amount", "credit_amount", mode="before")
    @classmethod
    def _clean_currency_fields(cls, v: Any) -> float:
        """Coerces empty strings or None to 0.0 before float validation."""
        if v in (None, ""):
            return 0,0
        return float(v)

    @field_validator("balance", mode="before")
    @classmethod
    def _clean_balance(cls, v: Any) -> Optional[float]:
        """Coerces empty strings to None before float validation."""
        if v in (None, ""):
            return None
        return float(v)


class LedgerFormat(SchemaBase):
    _id_counter:          ClassVar[int] = 1

    account_name:         str

    ledger_id:            Optional[str]  = None
    account_number:       Optional[str]  = None
    transaction_date:     Optional[str]  = None    # ISO YYYY-MM-DD
    transaction_date_raw: Optional[str]  = None
    debit_amount:         float          = 0.0     # Payment out / expense
    credit_amount:        float          = 0.0     # Receipt in / income
    reference_id:         Optional[str]  = None    # Invoice / cheque / ref no.
    parse_warnings:       List[str]      = Field(default_factory=list)
    run_id:               Optional[str]  = None

    source:               LedgerSource   = LedgerSource.MANUAL
    """
    LedgerSource.AUTO   → created automatically from bill scan pipeline.
    LedgerSource.MANUAL → loaded from a user-uploaded CSV.
    """
    journal_entry_id:     Optional[str]  = None
    """
    Set only when source == AUTO.  Equals JournalEntry.entry_id so the
    originating double-entry record can be retrieved for audit.
    """

    voucher_type:         Optional[str]  = None
    """
    E.g. "Purchase Voucher", "Payment Voucher".  Populated for AUTO records
    from JournalEntry.voucher_type; may be None for MANUAL records.
    """

    vendor_name:          Optional[str]  = None
    """
    Vendor / counter-party name.  For AUTO records this comes from
    JournalEntry.vendor_name; for MANUAL records it mirrors account_name.
    """

    @model_validator(mode="after")
    def _apply_cross_field_defaults(self) -> "LedgerFormat":
        """Replaces post-init logic for cross-dependent fields and counters."""
        if not self.ledger_id:
            self.ledger_id = f"L{LedgerFormat._id_counter:04d}"
            LedgerFormat._id_counter += 1

        # Convenience default: for MANUAL records vendor_name == account_name
        if self.vendor_name is None and self.source == LedgerSource.MANUAL:
            self.vendor_name = self.account_name

        return self
    
    @field_validator("debit_amount", "credit_amount", mode="before")
    @classmethod
    def _clean_currency_fields(cls, v: Any) -> float:
        """Coerces empty strings or None to 0.0 before float validation."""
        if v in (None, ""):
            return 0.0
        return float(v)
    
    @property
    def is_credit(self) -> bool:
        """True when this is a pure credit row (money IN)."""
        return self.credit_amount > 0.0 and self.debit_amount == 0.0

    @property
    def is_debit(self) -> bool:
        """True when this is a pure debit row (money OUT)."""
        return self.debit_amount > 0.0 and self.credit_amount == 0.0

    @property
    def is_auto(self) -> bool:
        return self.source == LedgerSource.AUTO

    @property
    def is_manual(self) -> bool:
        return self.source == LedgerSource.MANUAL

    def to_dict(self) -> dict:
        return {
            "ledger_id":            self.ledger_id,
            "source":               self.source,
            "account_name":         self.account_name,
            "account_number":       self.account_number,
            "transaction_date":     self.transaction_date,
            "transaction_date_raw": self.transaction_date_raw,
            "debit_amount":         self.debit_amount,
            "credit_amount":        self.credit_amount,
            "reference_id":         self.reference_id,
            "voucher_type":         self.voucher_type,
            "vendor_name":          self.vendor_name,
            "journal_entry_id":     self.journal_entry_id,
            "parse_warnings":       self.parse_warnings,
        }


class IgnoredMetadataRecord(SchemaBase):
    """A zero-amount record silently dropped before matching begins."""
    source:     str          # "bank" | "ledger"
    row_ref:    str          # row_index (bank) or ledger_id (ledger)
    narration:  str
    reason:     str = "Zero-amount metadata / header row — excluded from reconciliation."


class AuditInvestigationItem(SchemaBase):
    """A bank row flagged for manual GL journal entry; not force-matched."""
    bank_row_index: int
    narration:      str
    amount:         float
    direction:      str      # "debit" | "credit"
    flag_reason:    str
    action_required: str = (
        "Bank Reversal detected; requires manual General Ledger journal entry."
    )


class ReconciliationRun(SchemaBase):
    """Summary object describing one pipeline run and its results.

    This mirrors the database `ReconciliationRun` model and is used when
    serializing/deserializing run metadata between tasks, APIs and report
    writers.
    """
    id:                 Optional[str] = None
    template_id:        Optional[str] = None

    ledger_source:      Optional[str] = None  # "auto" | "manual"
    bank_name:          Optional[str] = None
    template_version:   Optional[str] = None
    bank_csv_path:      Optional[str] = None
    ledger_csv_path:    Optional[str] = None

    ledger_records:     int = 0
    bank_records:       int = 0
    exact_matches:      int = 0
    fuzzy_matches:      int = 0
    ai_matches:         int = 0
    unreconciled_ledger:int = 0
    unreconciled_bank:  int = 0

    run_at:             Optional[str] = None  # ISO datetime string
    user_id:            Optional[str] = None

    match_results:      List[dict] = Field(default_factory=list)
    ledger:             List[LedgerFormat] = Field(default_factory=list)
    bank:               List[BankStatement] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "template_id":          self.template_id,
            "ledger_source":        self.ledger_source,
            "bank_name":            self.bank_name,
            "template_version":     self.template_version,
            "ledger_records":       self.ledger_records,
            "bank_records":         self.bank_records,
            "exact_matches":        self.exact_matches,
            "fuzzy_matches":        self.fuzzy_matches,
            "ai_matches":           self.ai_matches,
            "unreconciled_ledger":  self.unreconciled_ledger,
            "unreconciled_bank":    self.unreconciled_bank,
            "run_at":               self.run_at,
            "user_id":              self.user_id,
            "match_results":        self.match_results,
            "ledger":               [l.to_dict() for l in self.ledger],
            "bank":                 [b.to_dict() for b in self.bank]
        }


class MatchResult(SchemaBase):
    """Lightweight representation of a match produced by the pipeline.

    Mirrors the database `MatchResult` and is used when transferring results
    between processes or serializing to JSON for the frontend.
    """
    id:                 Optional[str] = None
    run_id:             Optional[str] = None
    ledger_record_id:   Optional[str] = None
    bank_stmt_id:       Optional[str] = None
    match_type:         str = ""
    adjustment_type:    Optional[str] = None
    confidence_score:   Optional[str] = None
    matched_amount:     Optional[float] = None
    matched_date:       Optional[str] = None
    details:            Optional[str] = None

