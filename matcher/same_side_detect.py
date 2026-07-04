from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence
from schema.bank_renc_schema import BankStatement, LedgerFormat

def _safe_amount(obj, attr: str) -> float:
    try:
        v = getattr(obj, attr, 0.0)
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0

@dataclass
class SameSideDetection:
    same_side:   bool   
    confident:   bool   
    sample_size: int    
    reason:      str    

_CONFIDENCE_RATIO = 0.65
_MIN_MATCHED_PAIRS = 3
_SAMPLE_SIZE = 40
_AMOUNT_TOL = 0.50

def detect_same_side(
    gl_records:   Sequence[LedgerFormat],
    bank_records: Sequence[BankStatement],
) -> SameSideDetection:
    gl_sample   = list(gl_records)[:_SAMPLE_SIZE]
    bank_sample = list(bank_records)[:_SAMPLE_SIZE]

    same_side_votes = 0
    opp_side_votes  = 0

    for gl in gl_sample:
        gl_debit  = _safe_amount(gl, "debit_amount")
        gl_credit = _safe_amount(gl, "credit_amount")
        if gl_debit <= 0 and gl_credit <= 0:
            continue

        for bank in bank_sample:
            b_debit  = _safe_amount(bank, "debit")
            b_credit = _safe_amount(bank, "credit")
            if b_debit <= 0 and b_credit <= 0:
                continue

            if gl_debit > 0 and abs(gl_debit - b_debit) <= _AMOUNT_TOL:
                same_side_votes += 1
            if gl_credit > 0 and abs(gl_credit - b_credit) <= _AMOUNT_TOL:
                same_side_votes += 1

            if gl_debit > 0 and abs(gl_debit - b_credit) <= _AMOUNT_TOL:
                opp_side_votes += 1
            if gl_credit > 0 and abs(gl_credit - b_debit) <= _AMOUNT_TOL:
                opp_side_votes += 1

    total_votes = same_side_votes + opp_side_votes

    if total_votes < _MIN_MATCHED_PAIRS:
        return SameSideDetection(
            same_side=True,
            confident=False,
            sample_size=len(gl_sample),
            reason=(
                f"only {total_votes} amount-matched pair(s) found in the "
                f"sample — too few to detect a reliable orientation"
            ),
        )

    if same_side_votes >= opp_side_votes:
        winner, winner_votes = True, same_side_votes
    else:
        winner, winner_votes = False, opp_side_votes

    ratio = winner_votes / total_votes
    confident = ratio >= _CONFIDENCE_RATIO

    return SameSideDetection(
        same_side=winner,
        confident=confident,
        sample_size=len(gl_sample),
        reason=(
            f"{winner_votes}/{total_votes} equal-amount pairs "
            f"({ratio:.0%}) aligned with same_side={winner}"
        ),
    )
