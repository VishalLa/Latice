from __future__ import annotations

from typing import List
from pydantic import Field

from .base import SchemaBase


class AI1to1Match(SchemaBase):
    ledger_id:  str   = Field(..., description="Unique Ledger ID")
    bank_id:    int   = Field(..., description="Bank Statement Row Index")
    confidence: float = Field(..., ge=0.0, le=1.0, description="0.0 to 1.0")
    reasoning:  str   = Field(..., description="Concise semantic/date/amount match explanation")


class AIWindowOutput(SchemaBase):
    matches: List[AI1to1Match]


class AILedgerCandidate(SchemaBase):
    ledger_id: str


class AIManyToOneMatch(SchemaBase):
    bank_id:    int                     = Field(..., description="Single matched bank row index")
    ledger_ids: List[AILedgerCandidate] = Field(..., description="Ledger entries summing to bank amount")
    confidence: float                   = Field(..., ge=0.0, le=1.0)
    reasoning:  str


class AIManyToOneOutput(SchemaBase):
    matches: List[AIManyToOneMatch]


class DraftAccountSuggestion(SchemaBase):
    counter_account: str = Field(
        ..., description="The non-bank ledger account for this entry, e.g. "
        "'Bank Charges A/c', 'Interest Received A/c', or a vendor/party name + ' A/c'"
    )
    entry_narrative: str = Field(
        ..., description="One-sentence plain-English explanation of what this "
        "transaction likely represents"
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
