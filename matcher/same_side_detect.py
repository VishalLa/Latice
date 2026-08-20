from __future__ import annotations

from typing import List, Sequence, Tuple
from schema import BankStatement, LedgerFormat, SameSideDetection, MatchedPair


_CONFIDENCE_RATIO = 0.65
_MIN_MATCHED_PAIRS = 3
_AMOUNT_TOL = 0.50


def _safe_amount(obj: object, attr: str) -> float:
    try:
        v = getattr(obj, attr, 0.0)
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _greedy_match_pairs(
    sorted_a: List[Tuple[float, object]],
    sorted_b: List[Tuple[float, object]],
    tol: float,
) -> List[Tuple[object, object, float, float]]:
    """
    Maximum one-to-one matching between two (amount, record) lists sorted by
    amount, where a pairs with b iff |a.amount - b.amount| <= tol. Each
    record is consumed by at most one match.

    Returns list of (record_a, record_b, amount_a, amount_b) for every match.
    O(len(a) + len(b)) after sorting.
    """
    i = j = 0
    pairs: List[Tuple[object, object, float, float]] = []
    while i< len(sorted_a) and j < len(sorted_b):
        amt_a, rec_a = sorted_a[i]
        amt_b, rec_b = sorted_b[j]
        diff = amt_a - amt_b

        if abs(diff) <= tol:
            pairs.append((rec_a, rec_b, amt_a, amt_b))
            i += 1
            j += 1
        elif diff < 0:
            i += 1
        else:
            j += 1

    return pairs


def _to_matched_pairs(pairs, orientation, amount_type) -> List[MatchedPair]:
    return [
        MatchedPair(
            gl_record=gl_rec,
            bank_record=bank_rec,
            gl_amount=gl_amt,
            bank_amount=bank_amt,
            orientation=orientation,
            amount_type=amount_type,
        )
        for (gl_rec, bank_rec, gl_amt, bank_amt) in pairs
    ]


def detect_same_side(
    gl_records:     Sequence[LedgerFormat],
    bank_records:   Sequence[BankStatement]
) -> SameSideDetection:
    gl_all   = list(gl_records)
    bank_all = list(bank_records)

    gl_debits:    List[Tuple[float, LedgerFormat]]   = []
    gl_credits:   List[Tuple[float, LedgerFormat]]   = []
    bank_debits:  List[Tuple[float, BankStatement]]  = []
    bank_credits: List[Tuple[float, BankStatement]]  = []

    for gl in gl_all:
        d = _safe_amount(gl, "debit_amount")
        c = _safe_amount(gl, "credit_amount")
        if d > 0:
            gl_debits.append((d, gl))
        if c > 0:
            gl_credits.append((c, gl))

    for bank in bank_all:
        d = _safe_amount(bank, "debit_amount")
        c = _safe_amount(bank, "credit_amount")
        if d > 0:
            bank_debits.append((d, bank))
        if c > 0:
            bank_credits.append((c, bank))


    gl_debits.sort(key=lambda t: t[0])
    gl_credits.sort(key=lambda t: t[0])
    bank_debits.sort(key=lambda t: t[0])
    bank_credits.sort(key=lambda t: t[0])

    # "same side": GL debit <-> bank debit, GL credit <-> bank credit
    same_dd = _greedy_match_pairs(gl_debits,  bank_debits,  _AMOUNT_TOL)
    same_cc = _greedy_match_pairs(gl_credits, bank_credits, _AMOUNT_TOL)

    # "opposite side": GL debit <-> bank credit, GL credit <-> bank debit
    opp_dc = _greedy_match_pairs(gl_debits,  bank_credits, _AMOUNT_TOL)
    opp_cd = _greedy_match_pairs(gl_credits, bank_debits,  _AMOUNT_TOL)

    same_side_votes = len(same_dd) + len(same_cc)
    opp_side_votes  = len(opp_dc) + len(opp_cd)
    total_votes = same_side_votes + opp_side_votes

    if total_votes < _MIN_MATCHED_PAIRS:
        return SameSideDetection(
            same_side=True,
            confident=False,
            sample_size=len(gl_all),
            reason=(
                f"only {total_votes} amount-matched pair(s) found across "
                f"{len(gl_all)} GL / {len(bank_all)} bank records — too few "
                f"to detect a reliable orientation"
            ),
            matched_pairs=[],
        )

    if same_side_votes >= opp_side_votes:
        winner, winner_votes = True, same_side_votes
        winner_pairs = (
            _to_matched_pairs(same_dd, "same_side", "debit-debit")
            + _to_matched_pairs(same_cc, "same_side", "credit-credit")
        )
    else:
        winner, winner_votes = False, opp_side_votes
        winner_pairs = (
            _to_matched_pairs(opp_dc, "opposite_side", "debit-credit")
            + _to_matched_pairs(opp_cd, "opposite_side", "credit-debit")
        )

    ratio = winner_votes / total_votes
    confident = ratio >= _CONFIDENCE_RATIO

    return SameSideDetection(
        same_side=winner,
        confident=confident,
        sample_size=len(gl_all),
        reason=(
            f"{winner_votes}/{total_votes} equal-amount pairs "
            f"({ratio:.0%}) aligned with same_side={winner}"
        ),
        matched_pairs=winner_pairs,
    )

