from __future__ import annotations

import re
import itertools
from datetime import date as _date
from typing import Any, Callable, List, Optional

from schema import BankStatement, LedgerFormat, IgnoredMetadataRecord, AuditInvestigationItem
from .helper import (
    _NSF_RE,
    _INTEREST_RE,
    _REVERSAL_RE,
    
    _safe_amount,
    _safe_str,
    _get_amt,
    _get_date,
    _amounts_equal,
    _to_date,
    _days_between,
    _crosses_month_boundary,

    extract_utr,
    text_similarity,
    is_transposition,
)


class FuzzyMatcher:
    MAX_COMBINATION_SIZE: int = 6   
    MAX_COMBINATION_POOL_SIZE: int = 15

    _CROSS_MONTH_BUFFER: int = 15

    def __init__(
        self,
        pending_ledger: List[LedgerFormat],
        pending_bank:   List[BankStatement],
        same_side:      bool = True,
        tolerances:     dict = None,
    ) -> None:
        self.ledger_pool:   List[LedgerFormat]  = list(pending_ledger)
        self.bank_pool:     List[BankStatement] = list(pending_bank)
        self.fuzzy_matches: List[dict]          = []

        self.ignored_metadata_records:  List[IgnoredMetadataRecord]  = []
        self.audit_investigation_items: List[AuditInvestigationItem] = []

        self.tolerances: dict = tolerances if tolerances is not None else {}
        self._tol:       float = float(self.tolerances.get("DEFAULT", 1.0))

        # Per-strategy tolerance shortcuts (derived once for efficiency)
        self._tol_timing:      float = float(self.tolerances.get("TIMING_DIFFERENCE",   2.0))
        self._tol_rounding:    float = float(self.tolerances.get("ROUNDING_DIFFERENCE", 2.0))
        self._tol_transpos:    float = float(self.tolerances.get("TRANSPOSITION",        0.0))
        self._tol_exact:       float = float(self.tolerances.get("EXACT",                0.0))

        # Direction accessors 
        if same_side:
            self.gl_out: Callable[[LedgerFormat],  float] = lambda g: _safe_amount(g, "debit_amount")
            self.gl_in:  Callable[[LedgerFormat],  float] = lambda g: _safe_amount(g, "credit_amount")
        else:
            self.gl_out: Callable[[LedgerFormat],  float] = lambda g: _safe_amount(g, "credit_amount")
            self.gl_in:  Callable[[LedgerFormat],  float]  = lambda g: _safe_amount(g, "debit_amount")

        self.bank_out: Callable[[BankStatement], float] = lambda b: _safe_amount(b, "debit_amount")
        self.bank_in:  Callable[[BankStatement], float] = lambda b: _safe_amount(b, "credit_amount")


    def _effective_window(
        self,
        base_window:  int,
        date_iso_a:   Optional[str],
        date_iso_b:   Optional[str],
    ) -> int:
        if _crosses_month_boundary(date_iso_a, date_iso_b):
            return base_window + self._CROSS_MONTH_BUFFER
        return base_window


    @staticmethod
    def _closest_candidates(
        valid:          List[tuple],
        target:         float,
        max_pool:       int,
        counterpart_text: str = "",
        text_of:        Optional[Callable[[Any], str]] = None,
    ) -> List[tuple]:
        if len(valid) <= max_pool or target <= 0:
            return valid

        max_k = 6  

        def amount_distance(amount: float) -> float:
            return min(abs(amount - target / k) for k in range(2, max_k + 1))

        def sim_score(record: Any) -> float:
            if not text_of or not counterpart_text:
                return 0.0
            return text_similarity(counterpart_text, text_of(record))

        max_dist = max((amount_distance(x[1]) for x in valid), default=1.0) or 1.0

        def combined_score(item: tuple) -> float:
            record, amount = item
            sim = sim_score(record)
            dist_norm = amount_distance(amount) / max_dist
            return (sim * 2.0) + (1.0 - dist_norm)

        ranked = sorted(valid, key=combined_score, reverse=True)
        return ranked[:max_pool]


    def cleanse_zero_amount_metadata(self) -> None:
        for bk in list(self.bank_pool):
            if abs(_get_amt(bk)) <= 0.001:
                self.ignored_metadata_records.append(
                    IgnoredMetadataRecord(
                        source    = "bank",
                        row_ref   = str(getattr(bk, "row_index", "N/A")),
                        narration = getattr(bk, "narration", "OPENING/CLOSING BALANCE"),
                    )
                )
                self.bank_pool.remove(bk)

        for gl in list(self.ledger_pool):
            if abs(_get_amt(gl)) <= 0.001:
                self.ignored_metadata_records.append(
                    IgnoredMetadataRecord(
                        source    = "ledger",
                        row_ref   = str(getattr(gl, "ledger_id", "N/A")),
                        narration = getattr(gl, "account_name", "Header/Metadata Row"),
                    )
                )
                self.ledger_pool.remove(gl)


    def flag_ghost_reversals(self) -> None:
        for bk in list(self.bank_pool):
            narration = getattr(bk, "narration", "") or ""
            if _REVERSAL_RE.search(narration):
                self.audit_investigation_items.append(
                    AuditInvestigationItem(
                        bank_row_index = getattr(bk, "row_index", -1),
                        narration      = narration,
                        amount         = _get_amt(bk),
                        direction      = "credit" if getattr(bk, "credit", 0) else "debit",
                        flag_reason    = "Banking Reversal or Duplicate Error detected.",
                        action_required= (
                            "Requires manual General Ledger journal adjustment. "
                            "Item retained in matching pool - may still match a "
                            "ledger reversal entry via Strategy 7 or later."
                        ),
                    )
                )


    # Strategy 1 - Deposit in Transit
    def match_deposit_in_transit(self) -> None:
        base_window = 5
        for gl in list(self.ledger_pool):
            if self.gl_in(gl) <= 0 or not _safe_str(gl, "transaction_date"):
                continue
            candidates = [
                (diff, bank)
                for bank in self.bank_pool
                if self.bank_in(bank) > 0
                and _safe_str(bank, "date")
                and _amounts_equal(self.gl_in(gl), self.bank_in(bank), self._tol_timing)
                and (diff := _days_between(
                    _safe_str(gl, "transaction_date"),
                    _safe_str(bank, "date"),
                )) is not None
                and 1 <= diff <= self._effective_window(
                    base_window,
                    _safe_str(gl, "transaction_date"),
                    _safe_str(bank, "date"),
                )
            ]
            if candidates:
                diff, bank = min(candidates, key=lambda c: c[0])
                self.fuzzy_matches.append({
                    "ledger_id":        _safe_str(gl,   "ledger_id"),
                    "bank_id":          getattr(bank, "row_index", None),
                    "adjustment_type":  "Deposit in Transit",
                    "confidence_score": "High" if diff <= 3 else "Medium",
                    "details": (
                        f"Amount matches ({self.gl_in(gl):.2f}); "
                        f"bank cleared {diff} day(s) after ledger date."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 2 - Outstanding Checks
    def match_outstanding_checks(self) -> None:
        base_window = 14
        for gl in list(self.ledger_pool):
            if self.gl_out(gl) <= 0 or not _safe_str(gl, "transaction_date"):
                continue
            candidates = [
                (diff, bank)
                for bank in self.bank_pool
                if self.bank_out(bank) > 0
                and _safe_str(bank, "date")
                and _amounts_equal(self.gl_out(gl), self.bank_out(bank), self._tol_timing)
                and (diff := _days_between(
                    _safe_str(gl, "transaction_date"),
                    _safe_str(bank, "date"),
                )) is not None
                and 1 <= diff <= self._effective_window(
                    base_window,
                    _safe_str(gl, "transaction_date"),
                    _safe_str(bank, "date"),
                )
            ]
            if candidates:
                diff, bank = min(candidates, key=lambda c: c[0])
                self.fuzzy_matches.append({
                    "ledger_id":        _safe_str(gl,   "ledger_id"),
                    "bank_id":          getattr(bank, "row_index", None),
                    "adjustment_type":  "Outstanding Check",
                    "confidence_score": "High" if diff <= 7 else "Medium",
                    "details": (
                        f"Amount matches ({self.gl_out(gl):.2f}); "
                        f"payment cleared {diff} day(s) after ledger date."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 3 - Bank Service Charges
    def match_bank_service_charges(self) -> None:
        for gl in list(self.ledger_pool):
            if self.gl_out(gl) <= 0 or not _safe_str(gl, "transaction_date"):
                continue

            best = None
            for bank in self.bank_pool:
                if self.bank_out(bank) <= 0 or not _safe_str(bank, "date"):
                    continue

                diff = _days_between(
                    _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
                window = self._effective_window(
                    3, _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
                if diff is None or abs(diff) > window:
                    continue

                fee = self.bank_out(bank) - self.gl_out(gl)
                if fee <= 0:
                    continue

                fee_pct = fee / self.gl_out(gl) if self.gl_out(gl) else 0
                _MAX_PLAUSIBLE_FEE = 2500.0
                if fee > _MAX_PLAUSIBLE_FEE and fee_pct > 0.15:
                    continue

                sim = text_similarity(
                    _safe_str(gl, "account_name"), _safe_str(bank, "narration")
                )
                if sim < 0.15:
                    continue

                score = sim - abs(diff) * 0.01
                if best is None or score > best[0]:
                    best = (score, bank, diff, fee, sim)

            if best:
                _, bank, diff, fee, sim = best
                confidence = "High" if sim >= 0.5 else ("Medium" if sim >= 0.25 else "Low")
                match_data: dict = {
                    "ledger_id":        _safe_str(gl,   "ledger_id"),
                    "bank_id":          getattr(bank, "row_index", None),
                    "adjustment_type":  "Bank Service Charge",
                    "confidence_score": confidence,
                    "details": (
                        f"Bank debit ({self.bank_out(bank):.2f}) exceeds ledger "
                        f"({self.gl_out(gl):.2f}) by {fee:.2f} - embedded fee; "
                        f"narration similarity={sim:.2f}; dates {diff} day(s) apart."
                    ),
                }
                if confidence == "Low":
                    match_data["adjustment_type"] = "AI Agent Review (Possible Service Charge)"
                self.fuzzy_matches.append(match_data)
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 4 - Text Similarity Match
    def match_text_similarity(self) -> None:
        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0:
                continue

            best = None
            for bank in self.bank_pool:
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                if b_amt <= 0 or not _amounts_equal(gl_amt, b_amt, self._tol):
                    continue

                ref_gl = _safe_str(gl,   "reference_id")
                ref_bk = _safe_str(bank, "txn_id")
                if ref_gl and ref_bk and ref_gl != ref_bk:
                    continue

                sim = text_similarity(
                    _safe_str(gl, "account_name"), _safe_str(bank, "narration")
                )
                if sim < 0.3:
                    continue

                if best is None or sim > best[0]:
                    best = (sim, bank)

            if best:
                sim, bank = best
                confidence = "High" if sim >= 0.6 else ("Medium" if sim >= 0.3 else "Low")
                match_data: dict = {
                    "ledger_id":        _safe_str(gl,   "ledger_id"),
                    "bank_id":          getattr(bank, "row_index", None),
                    "adjustment_type":  "Text Similarity Match",
                    "confidence_score": confidence,
                    "details": (
                        f"Amount matches ({gl_amt:.2f}); reference absent on ≥1 side; "
                        f"account/narration similarity={sim:.2f}."
                    ),
                }
                if confidence == "Low":
                    match_data["adjustment_type"] = "AI Agent Review (Text Similarity)"
                self.fuzzy_matches.append(match_data)
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 5 - Book Error (Transposition)
    def match_transposition_errors(self) -> None:
        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or not _safe_str(gl, "transaction_date"):
                continue

            best = None
            for bank in self.bank_pool:
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                if b_amt <= 0 or not _safe_str(bank, "date"):
                    continue

                # use self._tol_transpos (TRANSPOSITION key → 0.0)
                if not is_transposition(gl_amt, b_amt, self._tol_transpos):
                    continue

                diff = _days_between(
                    _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
                window = self._effective_window(
                    3, _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
                if diff is None or abs(diff) > window:
                    continue

                sim = text_similarity(
                    _safe_str(gl, "account_name"), _safe_str(bank, "narration")
                )
                if sim < 0.3:
                    continue

                score = sim - abs(diff) * 0.01
                if best is None or score > best[0]:
                    best = (score, bank, diff, sim, b_amt)

            if best:
                _, bank, diff, sim, b_amt = best
                confidence = "High" if (sim >= 0.5 and diff == 0) else "Medium"
                self.fuzzy_matches.append({
                    "ledger_id":        _safe_str(gl,   "ledger_id"),
                    "bank_id":          getattr(bank, "row_index", None),
                    "adjustment_type":  "Book Error (Transposition)",
                    "confidence_score": confidence,
                    "details": (
                        f"Ledger: {gl_amt:.2f}, Bank: {b_amt:.2f} - same digits, "
                        f"different order; narration similarity={sim:.2f}; "
                        f"dates {diff} day(s) apart. Recommend journal entry correction."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 6 - Rounding Differences
    def match_rounding_differences(self) -> None:
        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or not _safe_str(gl, "transaction_date"):
                continue

            best = None
            for bank in self.bank_pool:
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                if b_amt <= 0 or not _safe_str(bank, "date"):
                    continue

                diff_amt = abs(gl_amt - b_amt)
                if not (0.0 < diff_amt <= self._tol_rounding):
                    continue

                diff_days = _days_between(
                    _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
                window = self._effective_window(
                    3, _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
                if diff_days is None or abs(diff_days) > window:
                    continue

                sim = text_similarity(
                    _safe_str(gl, "account_name"), _safe_str(bank, "narration")
                )
                if sim < 0.5:
                    continue

                if best is None or sim > best[0]:
                    best = (sim, bank, b_amt, diff_amt)

            if best:
                sim, bank, b_amt, diff_amt = best
                self.fuzzy_matches.append({
                    "ledger_id":        _safe_str(gl,   "ledger_id"),
                    "bank_id":          getattr(bank, "row_index", None),
                    "adjustment_type":  "Rounding Difference",
                    "confidence_score": "High",
                    "details": (
                        f"Ledger ({gl_amt:.2f}) and Bank ({b_amt:.2f}) differ by "
                        f"{diff_amt:.2f}. Narration similarity={sim:.2f}. "
                        f"Recommend writing off difference."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 7 - NSF / Returned Items
    def match_nsf_returned_items(self) -> None:
        for bank in list(self.bank_pool):
            if self.bank_out(bank) <= 0 or not _safe_str(bank, "date"):
                continue
            if not _NSF_RE.search(_safe_str(bank, "narration")):
                continue

            candidates = []
            for gl in self.ledger_pool:
                if self.gl_in(gl) <= 0 or not _safe_str(gl, "transaction_date"):
                    continue
                if not _amounts_equal(self.gl_in(gl), self.bank_out(bank), self._tol):
                    continue
                diff = _days_between(
                    _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
                if diff is None or diff < 0:
                    continue
                candidates.append((diff, gl))

            if candidates:
                diff, gl = min(candidates, key=lambda c: c[0])
                self.fuzzy_matches.append({
                    "ledger_id":        _safe_str(gl,   "ledger_id"),
                    "bank_id":          getattr(bank, "row_index", None),
                    "adjustment_type":  "NSF / Returned Item",
                    "confidence_score": "High",
                    "details": (
                        f"Bank narration indicates returned item "
                        f"('{_safe_str(bank, 'narration')}'); "
                        f"matches ledger receipt of {self.gl_in(gl):.2f} recorded "
                        f"{diff} day(s) earlier. Requires a reversing journal entry."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 8 - Interest Income (standalone bank credit)
    def match_interest_income(self) -> None:
        for bank in list(self.bank_pool):
            if self.bank_in(bank) <= 0:
                continue
            if not _INTEREST_RE.search(_safe_str(bank, "narration")):
                continue
            self.fuzzy_matches.append({
                "ledger_id":        None,
                "bank_id":          getattr(bank, "row_index", None),
                "adjustment_type":  "Interest Income (Book Adjustment)",
                "confidence_score": "Medium",
                "details": (
                    f"Standalone bank credit with interest narration "
                    f"('{_safe_str(bank, 'narration')}'); "
                    f"add {self.bank_in(bank):.2f} to book balance via journal entry."
                ),
            })
            self.bank_pool.remove(bank)


    # Strategy 9 - Discounts and Tax Withholdings
    def match_discounts_and_taxes(self) -> None:
        COMMON_RATES = [0.01, 0.02, 0.05, 0.075, 0.10, 0.20]
        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or not _safe_str(gl, "transaction_date"):
                continue

            best = None
            for bank in self.bank_pool:
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                if b_amt <= 0 or not _safe_str(bank, "date") or b_amt >= gl_amt:
                    continue

                implied = (gl_amt - b_amt) / gl_amt
                if not any(abs(implied - r) < 0.001 for r in COMMON_RATES):
                    continue

                diff = _days_between(
                    _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
                window = self._effective_window(
                    5, _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
                if diff is None or abs(diff) > window:
                    continue

                sim = text_similarity(
                    _safe_str(gl, "account_name"), _safe_str(bank, "narration")
                )
                if sim < 0.4:
                    continue

                if best is None or sim > best[0]:
                    best = (sim, bank, b_amt, implied)

            if best:
                sim, bank, b_amt, implied = best
                implied_pct = implied * 100
                pct_label = (
                    f"{implied_pct:.0f}%" if implied_pct == int(implied_pct)
                    else f"{implied_pct:.1f}%"
                )
                self.fuzzy_matches.append({
                    "ledger_id":        _safe_str(gl,   "ledger_id"),
                    "bank_id":          getattr(bank, "row_index", None),
                    "adjustment_type":  f"Discount/Withholding ({pct_label})",
                    "confidence_score": "Medium",
                    "details": (
                        f"Bank received {b_amt:.2f}, {pct_label} less "
                        f"than Ledger ({gl_amt:.2f}). Likely early payment discount "
                        f"or tax withholding."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 10 - Bank-Side Zero Sum (contra pair on bank statement)
    def match_bank_side_zero_sum(self) -> None:
        used:    set         = set()
        matched: List[tuple] = []

        for i, b1 in enumerate(self.bank_pool):
            if i in used:
                continue
            b1_out, b1_in = self.bank_out(b1), self.bank_in(b1)
            b1_amt    = b1_out if b1_out > 0 else b1_in
            b1_is_out = b1_out > 0
            if b1_amt <= 0:
                continue

            best = None
            for j, b2 in enumerate(self.bank_pool):
                if j <= i or j in used:
                    continue
                b2_is_out = self.bank_out(b2) > 0
                if b1_is_out == b2_is_out:        # must be opposite directions
                    continue
                b2_amt = self.bank_out(b2) if b2_is_out else self.bank_in(b2)
                if not _amounts_equal(b1_amt, b2_amt, self._tol):
                    continue

                sim  = text_similarity(
                    _safe_str(b1, "narration"), _safe_str(b2, "narration")
                )
                diff = abs(_days_between(
                    _safe_str(b1, "date"), _safe_str(b2, "date")
                ) or 0)
                if sim >= 0.6 or (sim >= 0.3 and diff <= 90):
                    score = sim - diff * 0.001
                    if best is None or score > best[0]:
                        best = (score, j, b2, sim, diff)

            if best:
                _, j, b2, sim, diff = best
                used.add(i)
                used.add(j)
                matched.append((b1, b2, sim, diff))

        for b1, b2, sim, diff in matched:
            b1_amt = self.bank_out(b1) if self.bank_out(b1) > 0 else self.bank_in(b1)
            reason = (
                "Bank Error/Reversal"
                if sim >= 0.6 and diff <= 14
                else "Temporary Advance/Refund"
            )
            self.fuzzy_matches.append({
                "ledger_id":        None,
                "bank_id":          (
                    f"{getattr(b1, 'row_index', '')} & "
                    f"{getattr(b2, 'row_index', '')}"
                ),
                "adjustment_type":  f"Bank-Side Zero-Sum ({reason})",
                "confidence_score": "High" if sim >= 0.7 else "Medium",
                "details": (
                    f"Net-zero bank entries of {b1_amt:.2f} cancel out. "
                    f"Spans {diff} day(s). Narration similarity={sim:.2f}."
                ),
            })
            if b1 in self.bank_pool:
                self.bank_pool.remove(b1)
            if b2 in self.bank_pool:
                self.bank_pool.remove(b2)


    # Strategy 11 - Ledger-Side Zero Sum (contra pair in ledger)
    def match_ledger_side_zero_sum(self) -> None:
        used:    set         = set()
        matched: List[tuple] = []

        for i, gl1 in enumerate(self.ledger_pool):
            if i in used:
                continue
            gl1_out   = self.gl_out(gl1)
            gl1_in    = self.gl_in(gl1)
            gl1_amt   = gl1_out if gl1_out > 0 else gl1_in
            gl1_is_out = gl1_out > 0
            if gl1_amt <= 0:
                continue

            best = None
            for j, gl2 in enumerate(self.ledger_pool):
                if j <= i or j in used:
                    continue
                gl2_is_out = self.gl_out(gl2) > 0
                if gl1_is_out == gl2_is_out:
                    continue
                gl2_amt = self.gl_out(gl2) if gl2_is_out else self.gl_in(gl2)
                if not _amounts_equal(gl1_amt, gl2_amt, self._tol):
                    continue

                sim  = text_similarity(
                    _safe_str(gl1, "account_name"), _safe_str(gl2, "account_name")
                )
                diff = abs(_days_between(
                    _safe_str(gl1, "transaction_date"),
                    _safe_str(gl2, "transaction_date"),
                ) or 0)
                if sim >= 0.6 or (sim >= 0.3 and diff <= 90):
                    score = sim - diff * 0.001
                    if best is None or score > best[0]:
                        best = (score, j, gl2, sim, diff)

            if best:
                _, j, gl2, sim, diff = best
                used.add(i)
                used.add(j)
                matched.append((gl1, gl2, sim, diff))

        for gl1, gl2, sim, diff in matched:
            gl1_amt = self.gl_out(gl1) if self.gl_out(gl1) > 0 else self.gl_in(gl1)
            reason  = (
                "Ledger Reversal"
                if sim >= 0.6 and diff <= 14
                else "Unbanked Advance/Refund"
            )
            self.fuzzy_matches.append({
                "ledger_id":        (
                    f"{_safe_str(gl1, 'ledger_id')} & "
                    f"{_safe_str(gl2, 'ledger_id')}"
                ),
                "bank_id":          None,
                "adjustment_type":  f"Ledger-Side Zero-Sum ({reason})",
                "confidence_score": "High" if sim >= 0.7 else "Medium",
                "details": (
                    f"Net-zero ledger entries of {gl1_amt:.2f} cancel out. "
                    f"Spans {diff} day(s). Account name similarity={sim:.2f}."
                ),
            })
            if gl1 in self.ledger_pool:
                self.ledger_pool.remove(gl1)
            if gl2 in self.ledger_pool:
                self.ledger_pool.remove(gl2)


    # Strategy 12 - Aggregated Split Transactions (1 Ledger : N Bank)
    def match_aggregated_transactions(self) -> None:
        base_window = 3
        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or not _safe_str(gl, "transaction_date"):
                continue

            valid = [
                (b, self.bank_out(b) if is_out else self.bank_in(b))
                for b in self.bank_pool
                if (self.bank_out(b) if is_out else self.bank_in(b)) > 0
                and _safe_str(b, "date")
                and (diff := _days_between(
                    _safe_str(gl, "transaction_date"), _safe_str(b, "date")
                )) is not None
                and 0 <= diff <= self._effective_window(
                    base_window, _safe_str(gl, "transaction_date"), _safe_str(b, "date")
                )
            ]
            valid = self._closest_candidates(
                valid, gl_amt, self.MAX_COMBINATION_POOL_SIZE,
                counterpart_text=_safe_str(gl, "account_name"),
                text_of=lambda b: _safe_str(b, "narration"),
            )

            found = None
            cap   = min(self.MAX_COMBINATION_SIZE + 1, len(valid) + 1)
            for r in range(2, cap):
                for combo in itertools.combinations(valid, r):
                    # use self._tol (DEFAULT)
                    if _amounts_equal(gl_amt, sum(x[1] for x in combo), self._tol):
                        if max(
                            text_similarity(
                                _safe_str(gl, "account_name"),
                                _safe_str(x[0], "narration"),
                            )
                            for x in combo
                        ) > 0.15:
                            found = combo
                            break
                if found:
                    break

            if found:
                banks   = [x[0] for x in found]
                id_str  = " & ".join(str(getattr(b, "row_index", "?")) for b in banks)
                amt_str = " + ".join(f"{x[1]:.2f}" for x in found)
                self.fuzzy_matches.append({
                    "ledger_id":        _safe_str(gl, "ledger_id"),
                    "bank_id":          id_str,
                    "adjustment_type":  f"Aggregated Split Transaction ({len(banks)} items)",
                    "confidence_score": "High",
                    "details": (
                        f"Ledger ({gl_amt:.2f}) = sum of {len(banks)} bank entries "
                        f"({amt_str})."
                    ),
                })
                self.ledger_pool.remove(gl)
                for b in banks:
                    if b in self.bank_pool:
                        self.bank_pool.remove(b)


    # Strategy 13 - Many-to-One Aggregation (N Ledger : 1 Bank)
    def match_many_to_one_aggregation(self) -> None:
        base_window = 3
        for bank in list(self.bank_pool):
            is_out = self.bank_out(bank) > 0
            b_amt  = self.bank_out(bank) if is_out else self.bank_in(bank)
            if b_amt <= 0 or not _safe_str(bank, "date"):
                continue

            valid = [
                (gl, self.gl_out(gl) if is_out else self.gl_in(gl))
                for gl in self.ledger_pool
                if (self.gl_out(gl) if is_out else self.gl_in(gl)) > 0
                and _safe_str(gl, "transaction_date")
                and (diff := _days_between(
                    _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )) is not None
                and 0 <= diff <= self._effective_window(
                    base_window, _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
            ]
            valid = self._closest_candidates(
                valid, b_amt, self.MAX_COMBINATION_POOL_SIZE,
                counterpart_text=_safe_str(bank, "narration"),
                text_of=lambda gl: _safe_str(gl, "account_name"),
            )

            found = None
            cap   = min(self.MAX_COMBINATION_SIZE + 1, len(valid) + 1)
            for r in range(2, cap):
                for combo in itertools.combinations(valid, r):
                    if _amounts_equal(b_amt, sum(x[1] for x in combo), self._tol):
                        if max(
                            text_similarity(
                                _safe_str(x[0], "account_name"),
                                _safe_str(bank, "narration"),
                            )
                            for x in combo
                        ) > 0.15:
                            found = combo
                            break
                if found:
                    break

            if found:
                gls     = [x[0] for x in found]
                id_str  = " & ".join(_safe_str(gl, "ledger_id") for gl in gls)
                amt_str = " + ".join(f"{x[1]:.2f}" for x in found)
                self.fuzzy_matches.append({
                    "ledger_id":        id_str,
                    "bank_id":          getattr(bank, "row_index", None),
                    "adjustment_type":  f"Many-to-One Aggregation ({len(gls)} items)",
                    "confidence_score": "High",
                    "details": (
                        f"Bank ({b_amt:.2f}) = sum of {len(gls)} ledger entries "
                        f"({amt_str})."
                    ),
                })
                self.bank_pool.remove(bank)
                for gl in gls:
                    if gl in self.ledger_pool:
                        self.ledger_pool.remove(gl)


    # Strategy 14 - One-to-Many Split Charge  (1 Ledger : 1 Bank where bank = 2×)
    def match_split_charge(self) -> None:
        for bk in list(self.bank_pool):
            b_amt  = _get_amt(bk)
            b_date = _get_date(bk)
            if b_amt <= 0 or b_date is None:
                continue

            matched_gl = None
            for gl in list(self.ledger_pool):
                l_amt  = _get_amt(gl)
                l_date = _get_date(gl)
                if l_amt <= 0 or l_date is None:
                    continue

                if not _amounts_equal(b_amt, l_amt * 2, self._tol):
                    continue

                base_window = 30
                window = base_window + self._CROSS_MONTH_BUFFER if (
                    (b_date.year, b_date.month) != (l_date.year, l_date.month)
                ) else base_window
                if abs((b_date - l_date).days) > window:
                    continue

                matched_gl = gl
                break   

            if matched_gl is None:
                continue    

            l_amt = _get_amt(matched_gl)

            self.fuzzy_matches.append({
                "ledger_id":        getattr(matched_gl, "ledger_id", "N/A"),
                "bank_id":          getattr(bk, "row_index", "N/A"),
                "adjustment_type":  "Split Charge Match (1st Half)",
                "confidence_score": "High",
                "details": (
                    f"Bank Row [{getattr(bk, 'row_index', '?')}] "
                    f"({_get_date(bk)}, ₹{b_amt:.2f}, "
                    f"'{getattr(bk, 'narration', 'N/A')}') "
                    f"is exactly 2× Ledger [{getattr(matched_gl, 'ledger_id', '?')}] "
                    f"({_get_date(matched_gl)}, ₹{l_amt:.2f}, "
                    f"'{getattr(matched_gl, 'account_name', 'N/A')}')."
                ),
            })

            if bk in self.bank_pool:
                self.bank_pool.remove(bk)
            if matched_gl in self.ledger_pool:
                self.ledger_pool.remove(matched_gl)
            self.audit_investigation_items.append(
                AuditInvestigationItem(
                    bank_row_index  = getattr(bk, "row_index", -1),
                    narration       = (
                        f"{getattr(bk, 'narration', 'Bank Charge')} "
                        f"(Unrecorded 2nd Half)"
                    ),
                    amount          = l_amt,
                    direction       = "debit",
                    flag_reason     = "Bank deducted exactly 2× the recorded ledger expense.",
                    action_required = (
                        f"Post adjusting GL expense journal entry for ₹{l_amt:.2f}."
                    ),
                )
            )


    # Strategy 15 - Base Fee + Tax Aggregation (1 Ledger : 2 Bank rows)
    def match_base_fee_plus_tax(self) -> None:
        base_adjacency_days = 2

        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or not _safe_str(gl, "transaction_date"):
                continue

            candidates = [
                b for b in self.bank_pool
                if (self.bank_out(b) if is_out else self.bank_in(b)) > 0
                and _safe_str(b, "date")
            ]

            found_pair: Optional[tuple] = None

            for i in range(len(candidates)):
                if found_pair:
                    break
                b1     = candidates[i]
                b1_amt = self.bank_out(b1) if is_out else self.bank_in(b1)
                b1_d   = _safe_str(b1, "date")

                for j in range(len(candidates)):
                    if i == j:
                        continue
                    b2     = candidates[j]
                    b2_amt = self.bank_out(b2) if is_out else self.bank_in(b2)
                    b2_d   = _safe_str(b2, "date")

                    day_gap = _days_between(b1_d, b2_d)
                    adjacency_days = self._effective_window(
                        base_adjacency_days, b1_d, b2_d
                    )
                    if day_gap is None or abs(day_gap) > adjacency_days:
                        continue

                    if not _amounts_equal(b1_amt + b2_amt, gl_amt, self._tol):
                        continue

                    found_pair = (b1, b1_amt, b2, b2_amt)
                    break   

            if found_pair is None:
                continue

            b1, b1_amt, b2, b2_amt = found_pair
            self.fuzzy_matches.append({
                "ledger_id":        _safe_str(gl, "ledger_id"),
                "bank_id":          (
                    f"{getattr(b1, 'row_index', 'N/A')} & "
                    f"{getattr(b2, 'row_index', 'N/A')}"
                ),
                "adjustment_type":  "Base Fee + Tax Lumped GL Match",
                "confidence_score": "High",
                "details": (
                    f"Lumped ledger (₹{gl_amt:.2f}) matched bank fee "
                    f"(₹{b1_amt:.2f}) + tax/GST (₹{b2_amt:.2f}). "
                    f"Rows not required to be adjacent - matched by date proximity "
                    f"(≤{adjacency_days} day(s) apart)."
                ),
            })
            self.ledger_pool.remove(gl)
            if b1 in self.bank_pool:
                self.bank_pool.remove(b1)
            if b2 in self.bank_pool:
                self.bank_pool.remove(b2)


    # Orchestrator
    def run(self) -> dict:

        # Stage 0: Pre-match cleansing 
        self.cleanse_zero_amount_metadata()  
        self.flag_ghost_reversals()           

        # Stage 1-9: Standard heuristics
        self.match_deposit_in_transit()       
        self.match_outstanding_checks()     
        self.match_bank_service_charges()     
        self.match_text_similarity()          
        self.match_transposition_errors()     
        self.match_rounding_differences()     
        self.match_nsf_returned_items()       
        self.match_interest_income()          
        self.match_discounts_and_taxes()      

        # Stage 10-13: Zero-sum & aggregations 
        self.match_bank_side_zero_sum()       
        self.match_ledger_side_zero_sum()    
        self.match_aggregated_transactions()  
        self.match_many_to_one_aggregation()  

        # Stage 14-15: Specialised residuals 
        self.match_split_charge()             
        self.match_base_fee_plus_tax()        

        return {
            "FUZZY_MATCHES": self.fuzzy_matches,
            "UNRECONCILED_ITEMS": {
                "ledger": self.ledger_pool,
                "bank":   self.bank_pool,
            },
            "IGNORED_METADATA":    self.ignored_metadata_records,
            "AUDIT_INVESTIGATION": self.audit_investigation_items,
        }


def fuzzy_matcher(
    pending_ledger: List[LedgerFormat],
    pending_bank:   List[BankStatement],
    tolerances:     dict = None,
    same_side:      bool = True,
) -> dict:
    print("[fuzzy_matcher] starting")
    return FuzzyMatcher(
        pending_ledger,
        pending_bank,
        same_side  = same_side,
        tolerances = tolerances,
    ).run()
    
