from __future__ import annotations

import re
import itertools
from datetime import date as _date
from difflib import SequenceMatcher
from typing import Callable, List, Optional
from schema import BankStatement, LedgerFormat


_AMOUNT_TOL = 0.05
_STOPWORDS = {
    "the", "inc", "ltd", "llc", "co", "corp", "corporation", "company",
    "payment", "ach", "trfr", "transfer", "ref", "upi", "neft", "rtgs",
    "imps", "dr", "cr", "no",
}
_NSF_RE      = re.compile(r"\b(nsf|bounced?|returned?|dishonou?red)\b", re.I)
_INTEREST_RE = re.compile(r"\b(interest|apy|intt?)\b", re.I)


def _amounts_equal(a: float, b: float) -> bool:
    return abs(a - b) <= _AMOUNT_TOL


def _to_date(iso_str: Optional[str]) -> Optional[_date]:
    if not iso_str:
        return None
    try:
        y, m, d = (int(p) for p in iso_str.split("-"))
        return _date(y, m, d)
    except Exception:
        return None


def _days_between(
    ledger_date_iso: Optional[str],
    bank_date_iso: Optional[str],
) -> Optional[int]:
    """Returns bank_date - ledger_date in days. None if either is unparseable."""
    ld, bd = _to_date(ledger_date_iso), _to_date(bank_date_iso)
    if ld is None or bd is None:
        return None
    return (bd - ld).days


def _normalize_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> set:
    return {t for t in _normalize_text(s).split() if t and t not in _STOPWORDS}


def _acronym(s: str) -> str:
    return "".join(w[0] for w in _normalize_text(s).split() if w)


def text_similarity(account_name: Optional[str], narration: Optional[str]) -> float:
    """0.0–1.0 score: max of sequence ratio, Jaccard token overlap, acronym check."""
    if not account_name or not narration:
        return 0.0
    ratio   = SequenceMatcher(None, _normalize_text(account_name),
                               _normalize_text(narration)).ratio()
    a_tok, n_tok = _tokens(account_name), _tokens(narration)
    jaccard = len(a_tok & n_tok) / len(a_tok | n_tok) if (a_tok and n_tok) else 0.0
    acr     = _acronym(account_name)
    acr_score = 0.85 if len(acr) >= 2 and acr in n_tok else 0.0
    return max(ratio, jaccard, acr_score)


def _digit_multiset(amount: float) -> str:
    return "".join(sorted(str(int(round(abs(amount) * 100)))))


def is_transposition(amount_a: float, amount_b: float) -> bool:
    """True if a and b contain the same decimal digits but in a different order."""
    return (not _amounts_equal(amount_a, amount_b) and
            _digit_multiset(amount_a) == _digit_multiset(amount_b))


class FuzzyMatcher:
    """
    Stateful reconciliation engine. Records are consumed from ledger_pool and
    bank_pool as each strategy claims them. Calling run() once executes all
    13 strategies in the documented priority order and returns the final dict.
    Do not call run() more than once on the same instance.
    """

    MAX_COMBINATION_SIZE: int = 6   # max N for aggregated / many-to-one splits

    def __init__(
        self,
        pending_ledger: List[LedgerFormat],
        pending_bank:   List[BankStatement],
        same_side:      bool = True,
    ):
        self.ledger_pool:   List[LedgerFormat]  = list(pending_ledger)
        self.bank_pool:     List[BankStatement] = list(pending_bank)
        self.fuzzy_matches: List[dict]          = []
        self.ai_agent:      List[dict]          = []

        # Direction accessors (see module docstring)
        if same_side:
            self.gl_out:   Callable[[LedgerFormat],  float] = lambda g: g.debit_amount
            self.gl_in:    Callable[[LedgerFormat],  float] = lambda g: g.credit_amount
        else:
            self.gl_out = lambda g: g.credit_amount
            self.gl_in  = lambda g: g.debit_amount

        self.bank_out: Callable[[BankStatement], float] = lambda b: b.debit
        self.bank_in:  Callable[[BankStatement], float] = lambda b: b.credit

    
    # Strategy 1 -- Deposit in Transit
    def match_deposit_in_transit(self) -> None:
        for gl in list(self.ledger_pool):
            if self.gl_in(gl) <= 0 or not gl.transaction_date:
                continue
            candidates = [
                (diff, bank)
                for bank in self.bank_pool
                if self.bank_in(bank) > 0
                and bank.date
                and _amounts_equal(self.gl_in(gl), self.bank_in(bank))
                and (diff := _days_between(gl.transaction_date, bank.date)) is not None
                and 1 <= diff <= 5
            ]
            if candidates:
                diff, bank = min(candidates, key=lambda c: c[0])
                self.fuzzy_matches.append({
                    "ledger_id":        gl.ledger_id,
                    "bank_id":          bank.row_index,
                    "adjustment_type":  "Deposit in Transit",
                    "confidence_score": "High" if diff <= 3 else "Medium",
                    "details": (
                        f"Amount matches exactly ({self.gl_in(gl):.2f}); "
                        f"bank cleared {diff} day(s) after ledger date."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 2 -- Outstanding Checks
    def match_outstanding_checks(self) -> None:
        for gl in list(self.ledger_pool):
            if self.gl_out(gl) <= 0 or not gl.transaction_date:
                continue

            candidates = [
                (diff, bank)
                for bank in self.bank_pool
                    if (
                        self.bank_out(bank) > 0
                        and bank.date
                        and _amounts_equal(self.gl_out(gl), self.bank_out(bank))
                        and (diff := _days_between(gl.transaction_date, bank.date)) is not None
                        and 1 <= diff <= 14
                    )
            ]

            if candidates:
                diff, bank = min(candidates, key=lambda c: c[0])
                self.fuzzy_matches.append({
                    "ledger_id":        gl.ledger_id,
                    "bank_id":          bank.row_index,
                    "adjustment_type":  "Outstanding Check",
                    "confidence_score": "High" if diff <= 7 else "Medium",
                    "details": (
                        f"Amount matches exactly ({self.gl_out(gl):.2f}); "
                        f"payment cleared {diff} day(s) after ledger date."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 3 -- Bank Service Charges
    def match_bank_service_charges(self) -> None:
        for gl in list(self.ledger_pool):
            if self.gl_out(gl) <= 0 or not gl.transaction_date:
                continue

            best = None
            for bank in self.bank_pool:
                if self.bank_out(bank) <= 0 or not bank.date:
                    continue

                diff = _days_between(gl.transaction_date, bank.date)
                if diff is None or abs(diff) > 3:
                    continue

                fee = self.bank_out(bank) - self.gl_out(gl)
                if fee <= 0:
                    continue

                fee_pct = fee / self.gl_out(gl) if self.gl_out(gl) else 0
                if fee > 500 and fee_pct > 0.15:
                    continue

                sim = text_similarity(gl.account_name, bank.narration)
                if sim < 0.15:
                    continue

                score = sim - abs(diff) * 0.01
                if best is None or score > best[0]:
                    best = (score, bank, diff, fee, sim)

            if best:
                _, bank, diff, fee, sim = best
                confidence = "High" if sim >= 0.5 else ("Medium" if sim >= 0.25 else "Low")
                match_data: dict = {
                    "ledger_id":        gl.ledger_id,
                    "bank_id":          bank.row_index,
                    "adjustment_type":  "Bank Service Charge",
                    "confidence_score": confidence,
                    "details": (
                        f"Bank debit ({self.bank_out(bank):.2f}) exceeds ledger "
                        f"amount ({self.gl_out(gl):.2f}) by {fee:.2f} -- embedded fee; "
                        f"narration similarity={sim:.2f}; dates {diff} day(s) apart."
                    ),
                }
                if confidence == "Low":
                    match_data["adjustment_type"] = "AI Agent Review (Possible Service Charge)"
                    self.ai_agent.append(match_data)
                else:
                    self.fuzzy_matches.append(match_data)
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 4 -- Text Similarity Match
    def match_text_similarity(self) -> None:
        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0:
                continue

            best = None
            for bank in self.bank_pool:
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                if b_amt <= 0 or not _amounts_equal(gl_amt, b_amt):
                    continue

                ref_gl, ref_bk = gl.reference_id, bank.txn_id
                if ref_gl and ref_bk and ref_gl != ref_bk:
                    continue

                sim = text_similarity(gl.account_name, bank.narration)
                if sim < 0.3:
                    continue

                if best is None or sim > best[0]:
                    best = (sim, bank)

            if best:
                sim, bank = best
                confidence = "High" if sim >= 0.6 else ("Medium" if sim >= 0.3 else "Low")
                match_data = {
                    "ledger_id":        gl.ledger_id,
                    "bank_id":          bank.row_index,
                    "adjustment_type":  "Text Similarity Match",
                    "confidence_score": confidence,
                    "details": (
                        f"Amount matches ({gl_amt:.2f}); reference absent on ≥1 side; "
                        f"account/narration similarity={sim:.2f}."
                    ),
                }
                if confidence == "Low":
                    match_data["adjustment_type"] = "AI Agent Review (Text Similarity)"
                    self.ai_agent.append(match_data)
                else:
                    self.fuzzy_matches.append(match_data)
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 5 -- Book Error (Transposition)
    def match_transposition_errors(self) -> None:
        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or not gl.transaction_date:
                continue

            best = None
            for bank in self.bank_pool:
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                if b_amt <= 0 or not bank.date:
                    continue

                if not is_transposition(gl_amt, b_amt):
                    continue

                diff = _days_between(gl.transaction_date, bank.date)
                if diff is None or abs(diff) > 3:
                    continue

                sim = text_similarity(gl.account_name, bank.narration)
                if sim < 0.3:
                    continue

                score = sim - abs(diff) * 0.01
                if best is None or score > best[0]:
                    best = (score, bank, diff, sim, b_amt)

            if best:
                _, bank, diff, sim, b_amt = best
                confidence = "High" if (sim >= 0.5 and diff == 0) else "Medium"
                self.fuzzy_matches.append({
                    "ledger_id":        gl.ledger_id,
                    "bank_id":          bank.row_index,
                    "adjustment_type":  "Book Error (Transposition)",
                    "confidence_score": confidence,
                    "details": (
                        f"Ledger: {gl_amt:.2f}, Bank: {b_amt:.2f} -- same digits, "
                        f"different order; narration similarity={sim:.2f}; "
                        f"dates {diff} day(s) apart. Recommend journal entry correction."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 6 -- Rounding Differences
    def match_rounding_differences(self) -> None:
        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or not gl.transaction_date:
                continue

            best = None
            for bank in self.bank_pool:
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                if b_amt <= 0 or not bank.date:
                    continue

                diff_amt = abs(gl_amt - b_amt)
                if not (0.0 < diff_amt <= 0.05):
                    continue

                diff_days = _days_between(gl.transaction_date, bank.date)
                if diff_days is None or abs(diff_days) > 3:
                    continue

                sim = text_similarity(gl.account_name, bank.narration)
                if sim < 0.5:
                    continue

                if best is None or sim > best[0]:
                    best = (sim, bank, b_amt, diff_amt)

            if best:
                sim, bank, b_amt, diff_amt = best
                self.fuzzy_matches.append({
                    "ledger_id":        gl.ledger_id,
                    "bank_id":          bank.row_index,
                    "adjustment_type":  "Rounding Difference",
                    "confidence_score": "High",
                    "details": (
                        f"Ledger ({gl_amt:.2f}) and Bank ({b_amt:.2f}) differ by "
                        f"just {diff_amt:.2f}. Narration similarity={sim:.2f}. "
                        f"Recommend writing off difference."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)

    # Strategy 7 -- NSF / Returned Items
    def match_nsf_returned_items(self) -> None:
        for bank in list(self.bank_pool):
            if self.bank_out(bank) <= 0 or not bank.date:
                continue

            if not _NSF_RE.search(bank.narration or ""):
                continue

            candidates = []
            for gl in self.ledger_pool:
                if self.gl_in(gl) <= 0 or not gl.transaction_date:
                    continue
                
                if not _amounts_equal(self.gl_in(gl), self.bank_out(bank)):
                    continue

                diff = _days_between(gl.transaction_date, bank.date)
                if diff is None or diff < 0:
                    continue

                candidates.append((diff, gl))

            if candidates:
                diff, gl = min(candidates, key=lambda c: c[0])
                self.fuzzy_matches.append({
                    "ledger_id":        gl.ledger_id,
                    "bank_id":          bank.row_index,
                    "adjustment_type":  "NSF / Returned Item",
                    "confidence_score": "High",
                    "details": (
                        f"Bank narration indicates a returned item ('{bank.narration}'); "
                        f"matches a ledger receipt of {self.gl_in(gl):.2f} recorded "
                        f"{diff} day(s) earlier. Requires a reversing journal entry."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 8 -- Interest Income (standalone bank credit)
    def match_interest_income(self) -> None:
        for bank in list(self.bank_pool):
            if self.bank_in(bank) <= 0:
                continue

            if not _INTEREST_RE.search(bank.narration or ""):
                continue

            self.fuzzy_matches.append({
                "ledger_id":        None,
                "bank_id":          bank.row_index,
                "adjustment_type":  "Interest Income (Book Adjustment)",
                "confidence_score": "Medium",
                "details": (
                    f"Standalone bank credit with interest narration "
                    f"('{bank.narration}'); add {self.bank_in(bank):.2f} to "
                    f"book balance via journal entry."
                ),
            })
            self.bank_pool.remove(bank)


    # Strategy 9 -- Discounts and Tax Withholdings
    def match_discounts_and_taxes(self) -> None:
        COMMON_RATES = [0.01, 0.02, 0.05, 0.10]
        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or not gl.transaction_date:
                continue

            best = None
            for bank in self.bank_pool:
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                if b_amt <= 0 or not bank.date or b_amt >= gl_amt:
                    continue

                implied = (gl_amt - b_amt) / gl_amt
                if not any(abs(implied - r) < 0.001 for r in COMMON_RATES):
                    continue

                diff = _days_between(gl.transaction_date, bank.date)
                if diff is None or abs(diff) > 5:
                    continue

                sim = text_similarity(gl.account_name, bank.narration)
                if sim < 0.4:
                    continue

                if best is None or sim > best[0]:
                    best = (sim, bank, b_amt, implied)

            if best:
                sim, bank, b_amt, implied = best
                self.fuzzy_matches.append({
                    "ledger_id":        gl.ledger_id,
                    "bank_id":          bank.row_index,
                    "adjustment_type":  f"Discount/Withholding ({implied*100:.0f}%)",
                    "confidence_score": "Medium",
                    "details": (
                        f"Bank received {b_amt:.2f}, exactly {implied*100:.0f}% less "
                        f"than Ledger ({gl_amt:.2f}). Likely early payment discount "
                        f"or tax withholding."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 10 -- Bank-Side Zero Sum (contra pair on bank statement)
    def match_bank_side_zero_sum(self) -> None:
        used      = set()
        matched   = []

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
                if b1_is_out == b2_is_out:           # must be opposite directions
                    continue

                b2_amt = self.bank_out(b2) if b2_is_out else self.bank_in(b2)
                if not _amounts_equal(b1_amt, b2_amt):
                    continue

                sim  = text_similarity(b1.narration, b2.narration)
                diff = abs(_days_between(b1.date, b2.date) or 0)
                if sim >= 0.6 or (sim >= 0.3 and diff <= 90):
                    score = sim - diff * 0.001
                    if best is None or score > best[0]:
                        best = (score, j, b2, sim, diff)

            if best:
                _, j, b2, sim, diff = best
                used.add(i); used.add(j)
                matched.append((b1, b2, sim, diff))

        for b1, b2, sim, diff in matched:
            b1_amt  = self.bank_out(b1) if self.bank_out(b1) > 0 else self.bank_in(b1)
            reason  = ("Bank Error/Reversal"
                       if sim >= 0.6 and diff <= 14
                       else "Temporary Advance/Refund")
            self.fuzzy_matches.append({
                "ledger_id":        None,
                "bank_id":          f"{b1.row_index} & {b2.row_index}",
                "adjustment_type":  f"Bank-Side Zero-Sum ({reason})",
                "confidence_score": "High" if sim >= 0.7 else "Medium",
                "details": (
                    f"Net-zero bank entries of {b1_amt:.2f} cancel out. "
                    f"Spans {diff} day(s). Narration similarity={sim:.2f}."
                ),
            })
            if b1 in self.bank_pool: self.bank_pool.remove(b1)
            if b2 in self.bank_pool: self.bank_pool.remove(b2)

    # Strategy 11 -- Ledger-Side Zero Sum (contra pair in ledger)
    def match_ledger_side_zero_sum(self) -> None:
        used    = set()
        matched = []

        for i, gl1 in enumerate(self.ledger_pool):
            if i in used:
                continue

            gl1_out  = self.gl_out(gl1)
            gl1_in   = self.gl_in(gl1)
            gl1_amt  = gl1_out if gl1_out > 0 else gl1_in
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
                if not _amounts_equal(gl1_amt, gl2_amt):
                    continue

                sim  = text_similarity(gl1.account_name, gl2.account_name)
                diff = abs(_days_between(gl1.transaction_date, gl2.transaction_date) or 0)
                if sim >= 0.6 or (sim >= 0.3 and diff <= 90):
                    score = sim - diff * 0.001
                    if best is None or score > best[0]:
                        best = (score, j, gl2, sim, diff)

            if best:
                _, j, gl2, sim, diff = best
                used.add(i); used.add(j)
                matched.append((gl1, gl2, sim, diff))


        for gl1, gl2, sim, diff in matched:
            gl1_amt = self.gl_out(gl1) if self.gl_out(gl1) > 0 else self.gl_in(gl1)
            reason  = (
                "Ledger Reversal"
                if sim >= 0.6 and diff <= 14
                else "Unbanked Advance/Refund"
            )

            self.fuzzy_matches.append({
                "ledger_id":        f"{gl1.ledger_id} & {gl2.ledger_id}",
                "bank_id":          None,
                "adjustment_type":  f"Ledger-Side Zero-Sum ({reason})",
                "confidence_score": "High" if sim >= 0.7 else "Medium",
                "details": (
                    f"Net-zero ledger entries of {gl1_amt:.2f} cancel out. "
                    f"Spans {diff} day(s). Account name similarity={sim:.2f}."
                ),
            })

            if gl1 in self.ledger_pool: self.ledger_pool.remove(gl1)
            if gl2 in self.ledger_pool: self.ledger_pool.remove(gl2)


    # Strategy 12 -- Aggregated Split Transactions (1 Ledger : N Bank)
    def match_aggregated_transactions(self) -> None:
        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or not gl.transaction_date:
                continue

            valid = [
                (b, self.bank_out(b) if is_out else self.bank_in(b))
                for b in self.bank_pool
                if (self.bank_out(b) if is_out else self.bank_in(b)) > 0
                and b.date
                and (diff := _days_between(gl.transaction_date, b.date)) is not None
                and 0 <= diff <= 3
            ]

            found = None
            cap   = min(self.MAX_COMBINATION_SIZE + 1, len(valid) + 1)
            for r in range(2, cap):
                for combo in itertools.combinations(valid, r):
                    if _amounts_equal(gl_amt, sum(x[1] for x in combo)):
                        if max(text_similarity(gl.account_name, x[0].narration)
                               for x in combo) > 0.15:
                            found = combo
                            break
                if found:
                    break

            if found:
                banks    = [x[0] for x in found]
                id_str   = " & ".join(str(b.row_index) for b in banks)
                amt_str  = " + ".join(f"{x[1]:.2f}" for x in found)
                self.fuzzy_matches.append({
                    "ledger_id":        gl.ledger_id,
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


    # Strategy 13 -- Many-to-One Aggregation (N Ledger : 1 Bank)
    def match_many_to_one_aggregation(self) -> None:
        for bank in list(self.bank_pool):
            is_out = self.bank_out(bank) > 0
            b_amt  = self.bank_out(bank) if is_out else self.bank_in(bank)
            if b_amt <= 0 or not bank.date:
                continue

            valid = [
                (gl, self.gl_out(gl) if is_out else self.gl_in(gl))
                for gl in self.ledger_pool
                if (self.gl_out(gl) if is_out else self.gl_in(gl)) > 0
                and gl.transaction_date
                and (diff := _days_between(gl.transaction_date, bank.date)) is not None
                and 0 <= diff <= 3
            ]

            found = None
            cap   = min(self.MAX_COMBINATION_SIZE + 1, len(valid) + 1)
            for r in range(2, cap):
                for combo in itertools.combinations(valid, r):
                    if _amounts_equal(b_amt, sum(x[1] for x in combo)):
                        if max(text_similarity(x[0].account_name, bank.narration)
                               for x in combo) > 0.15:
                            found = combo
                            break
                if found:
                    break

            if found:
                gls     = [x[0] for x in found]
                id_str  = " & ".join(str(gl.ledger_id) for gl in gls)
                amt_str = " + ".join(f"{x[1]:.2f}" for x in found)
                self.fuzzy_matches.append({
                    "ledger_id":        id_str,
                    "bank_id":          bank.row_index,
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

    # Orchestrator
    def run(self) -> dict:
        """
        Execute all 13 strategies in priority order. Each strategy removes
        records it claims from ledger_pool / bank_pool so later strategies
        only see unmatched records.
        """
        self.match_deposit_in_transit()
        self.match_outstanding_checks()
        self.match_bank_service_charges()
        self.match_text_similarity()
        self.match_transposition_errors()
        self.match_rounding_differences()
        self.match_nsf_returned_items()
        self.match_interest_income()
        self.match_discounts_and_taxes()
        self.match_bank_side_zero_sum() 
        self.match_ledger_side_zero_sum()
        self.match_aggregated_transactions()
        self.match_many_to_one_aggregation()

        return {
            "FUZZY_MATCHES": self.fuzzy_matches,
            "AI_AGENT":      self.ai_agent,
            "UNRECONCILED_ITEMS": {
                "ledger": self.ledger_pool,
                "bank":   self.bank_pool,
            },
        }

def fuzzy_matcher(
    pending_ledger: List[LedgerFormat],
    pending_bank:   List[BankStatement],
    same_side:      bool = True,
) -> dict:
    """
    Wraps FuzzyMatcher.run().

    Args:
        pending_ledger: PENDING_FUZZY_LEDGER output from Phase 1 exact_match()
        pending_bank:   PENDING_FUZZY_BANK output from Phase 1 exact_match()
        same_side:      True = cashbook format (GL Debit=out, GL Credit=in).
                        False = standard double-entry GL.

    Returns:
        {"FUZZY_MATCHES": [...], "AI_AGENT": [...],
         "UNRECONCILED_ITEMS": {"ledger": [...], "bank": [...]}}
    """
    return FuzzyMatcher(pending_ledger, pending_bank, same_side=same_side).run()

