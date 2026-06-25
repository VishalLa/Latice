from __future__ import annotations

import re
import itertools
from datetime import date as _date
from difflib import SequenceMatcher
from typing import Any, Callable, List, Optional
from schema import BankStatement, LedgerFormat, IgnoredMetadataRecord, AuditInvestigationItem


_STOPWORDS = {
    "the", "inc", "ltd", "llc", "co", "corp", "corporation", "company",
    "payment", "ach", "trfr", "transfer", "ref", "upi", "neft", "rtgs",
    "imps", "dr", "cr", "no",
}

_NSF_RE      = re.compile(r"\b(nsf|bounced?|returned?|dishonou?red)\b",     re.I)
_INTEREST_RE = re.compile(r"\b(interest|apy|intt?)\b",                       re.I)

#  Ghost reversals stay in pool; audit flag only.
# Wider than Strategy 7 (_NSF_RE) to capture bank-side reversal language.
_REVERSAL_RE = re.compile(r"\b(reversal|return|bounced|reject|dup|duplicate)\b", re.I)

# Indian banking narration noise patterns stripped before text comparison.
# Order matters: strip prefixes first, then UTR/ref numbers, then IFSC codes.
_INDIAN_PREFIX_RE = re.compile(
    r"""
    (?:
        # Payment rail prefixes with trailing slash or hyphen
        \b(?:UPI|NEFT|IMPS|RTGS|NACH|ACH|ECS|NACH)\s*[/\-]?
        |
        # UTR / reference numbers  (12–22 hex / numeric chars after known labels)
        \b(?:UTR|REF|REFNO|TXNID|TXN|TRNREF)\s*[:\-]?\s*[A-Z0-9]{8,22}\b
        |
        # Bare UTR format: 22-digit numeric string (NEFT/RTGS UTR is exactly 22 chars)
        \b[0-9]{12,22}\b
        |
        # IFSC code: 4 alpha + 0 + 6 alphanumeric  (e.g. HDFC0001234, ICICI0001234)
        \b[A-Z]{4}0[A-Z0-9]{6}\b
        |
        # Common Indian bank name prefixes that appear in narrations
        \b(?:HDFC|ICICI|AXIS|SBI|PNB|BOB|KOTAK|YES|IDBI|INDUS|FEDERAL)\s*BANK\b
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _amounts_equal(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _to_date(iso_str: Optional[str]) -> Optional[_date]:
    if not iso_str:
        return None
    try:
        y, m, d = (int(p) for p in str(iso_str)[:10].split("-"))
        return _date(y, m, d)
    except Exception:
        return None


def _days_between(
    ledger_date_iso: Optional[str],
    bank_date_iso:   Optional[str],
) -> Optional[int]:
    """Returns bank_date − ledger_date in days. None if either is unparseable."""
    ld, bd = _to_date(ledger_date_iso), _to_date(bank_date_iso)
    if ld is None or bd is None:
        return None
    return (bd - ld).days


def _normalize_text(s: str) -> str:
    """
    Lowercase, strip punctuation.
    Indian banking narration noise (UPI/, NEFT/, UTR numbers, IFSC codes)
    is removed BEFORE the standard normalisation so text_similarity only sees
    the actual vendor / payee name.
    """
    # Step 1 — strip Indian payment-rail noise
    s = _INDIAN_PREFIX_RE.sub(" ", s)
    # Step 2 — standard normalisation
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> set:
    return {t for t in _normalize_text(s).split() if t and t not in _STOPWORDS}


def _acronym(s: str) -> str:
    return "".join(w[0] for w in _normalize_text(s).split() if w)


def text_similarity(account_name: Optional[str], narration: Optional[str]) -> float:
    """0.0–1.0: max of sequence ratio, Jaccard token overlap, acronym check.
    both inputs go through _normalize_text which now strips Indian banking
    noise before comparison, so 'UPI/HDFC0001234/Rahul Traders' cleanly compares
    against ledger account_name 'Rahul Traders'.
    """
    if not account_name or not narration:
        return 0.0
    ratio   = SequenceMatcher(
        None, _normalize_text(account_name), _normalize_text(narration)
    ).ratio()
    a_tok, n_tok = _tokens(account_name), _tokens(narration)
    jaccard  = len(a_tok & n_tok) / len(a_tok | n_tok) if (a_tok and n_tok) else 0.0
    acr      = _acronym(account_name)
    acr_score = 0.85 if len(acr) >= 2 and acr in n_tok else 0.0
    return max(ratio, jaccard, acr_score)


def _digit_multiset(amount: float) -> str:
    return "".join(sorted(str(int(round(abs(amount) * 100)))))


def is_transposition(amount_a: float, amount_b: float, tol: float) -> bool:
    return (
        not _amounts_equal(amount_a, amount_b, tol)
        and _digit_multiset(amount_a) == _digit_multiset(amount_b)
    )


def _safe_amount(obj: Any, attr: str, default: float = 0.0) -> float:
    try:
        val = getattr(obj, attr, default)
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _safe_str(obj: Any, attr: str, default: str = "") -> str:
    try:
        val = getattr(obj, attr, default)
        return str(val) if val is not None else default
    except Exception:
        return default


def _get_amt(rec: Any) -> float:
    """Safely extracts absolute monetary value regardless of object shape."""
    if isinstance(rec, dict):
        return float(rec.get("debit", rec.get("debit_amount",
               rec.get("credit", rec.get("credit_amount", 0.0)))))
    for attr in ("debit", "debit_amount", "credit", "credit_amount"):
        val = getattr(rec, attr, None)
        if val:
            return float(val)
    return 0.0


def _get_date(rec: Any) -> Optional[_date]:
    """Extracts a standardised date object from bank or ledger record."""
    d_str = (
        rec.get("date", rec.get("transaction_date"))
        if isinstance(rec, dict)
        else getattr(rec, "date", getattr(rec, "transaction_date", None))
    )
    return _to_date(str(d_str)) if d_str else None


class FuzzyMatcher:
    """
    Stateful reconciliation engine.

    Records are consumed from ledger_pool / bank_pool as each strategy claims
    them.  Call run() once per instance.

    Tolerance dictionary (passed from __init__.py TOLERANCES):
        EXACT               – amount tolerance for exact-like comparisons
        ROUNDING_DIFFERENCE – max ₹ gap to call a rounding difference
        TIMING_DIFFERENCE   – tolerance used for deposit-in-transit / o/s checks
        TRANSPOSITION       – must be 0; digit-multiset check handles it
        AI_MATCHER          – used downstream in ai_matcher_pipeline
        DEFAULT             – fallback for strategies that don't have a specific key
    """

    MAX_COMBINATION_SIZE: int = 6   # max N for aggregated / many-to-one splits

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
            self.gl_out = lambda g: _safe_amount(g, "credit_amount")
            self.gl_in  = lambda g: _safe_amount(g, "debit_amount")

        self.bank_out: Callable[[BankStatement], float] = lambda b: _safe_amount(b, "debit")
        self.bank_in:  Callable[[BankStatement], float] = lambda b: _safe_amount(b, "credit")

    # Strategy 0a — Zero-Amount Metadata Dropping (pre-match cleanser)
    def cleanse_zero_amount_metadata(self) -> None:
        """
        Removes zero-amount rows (opening/closing balance header rows, column
        subtotal rows, etc.) from both pools before any matching begins.
        Removed records go to self.ignored_metadata_records for the audit trail.
        """
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

    # Strategy 0b — Ghost Reversal Flagging
    def flag_ghost_reversals(self) -> None:
        """
        Scans bank_pool for narrations that signal a reversal / bounce / return.
        Matching items are tagged in audit_investigation_items BUT deliberately
        left in bank_pool so Strategy 7 (NSF/Returned Items) and later
        strategies can still attempt a ledger match against them.
        """
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
                            "Item retained in matching pool — may still match a "
                            "ledger reversal entry via Strategy 7 or later."
                        ),
                    )
                )


    # Strategy 1 — Deposit in Transit
    def match_deposit_in_transit(self) -> None:
        window = 5 + self._CROSS_MONTH_BUFFER
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
                and 1 <= diff <= window
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


    # Strategy 2 — Outstanding Checks
    def match_outstanding_checks(self) -> None:
        window = 14 + self._CROSS_MONTH_BUFFER
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
                and 1 <= diff <= window
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


    # Strategy 3 — Bank Service Charges
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
                if diff is None or abs(diff) > 3 + self._CROSS_MONTH_BUFFER:
                    continue

                fee = self.bank_out(bank) - self.gl_out(gl)
                if fee <= 0:
                    continue

                fee_pct = fee / self.gl_out(gl) if self.gl_out(gl) else 0
                if fee > 500 and fee_pct > 0.15:
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
                        f"({self.gl_out(gl):.2f}) by {fee:.2f} — embedded fee; "
                        f"narration similarity={sim:.2f}; dates {diff} day(s) apart."
                    ),
                }
                if confidence == "Low":
                    match_data["adjustment_type"] = "AI Agent Review (Possible Service Charge)"
                self.fuzzy_matches.append(match_data)
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 4 — Text Similarity Match
    def match_text_similarity(self) -> None:
        """applied via text_similarity() → _normalize_text() stripping
        Indian banking prefixes (UPI/, NEFT/, UTR numbers, IFSC codes)."""
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

                # text_similarity now strips Indian routing noise before comparing
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


    # Strategy 5 — Book Error (Transposition)
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
                if diff is None or abs(diff) > 3 + self._CROSS_MONTH_BUFFER:
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
                        f"Ledger: {gl_amt:.2f}, Bank: {b_amt:.2f} — same digits, "
                        f"different order; narration similarity={sim:.2f}; "
                        f"dates {diff} day(s) apart. Recommend journal entry correction."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 6 — Rounding Differences
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

                # use dedicated self._tol_rounding (ROUNDING_DIFFERENCE key)
                diff_amt = abs(gl_amt - b_amt)
                if not (0.0 < diff_amt <= self._tol_rounding):
                    continue

                diff_days = _days_between(
                    _safe_str(gl, "transaction_date"), _safe_str(bank, "date")
                )
                if diff_days is None or abs(diff_days) > 3 + self._CROSS_MONTH_BUFFER:
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


    # Strategy 7 — NSF / Returned Items
    def match_nsf_returned_items(self) -> None:
        """
        ghost-reversal items were left in bank_pool by Strategy 0b,
        so this strategy can now match them against their corresponding ledger
        receipt entries as originally intended.
        """
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


    # Strategy 8 — Interest Income (standalone bank credit)
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


    # Strategy 9 — Discounts and Tax Withholdings
    def match_discounts_and_taxes(self) -> None:
        COMMON_RATES = [0.01, 0.02, 0.05, 0.10]
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
                if diff is None or abs(diff) > 5 + self._CROSS_MONTH_BUFFER:
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
                self.fuzzy_matches.append({
                    "ledger_id":        _safe_str(gl,   "ledger_id"),
                    "bank_id":          getattr(bank, "row_index", None),
                    "adjustment_type":  f"Discount/Withholding ({implied * 100:.0f}%)",
                    "confidence_score": "Medium",
                    "details": (
                        f"Bank received {b_amt:.2f}, {implied * 100:.0f}% less "
                        f"than Ledger ({gl_amt:.2f}). Likely early payment discount "
                        f"or tax withholding."
                    ),
                })
                self.ledger_pool.remove(gl)
                self.bank_pool.remove(bank)


    # Strategy 10 — Bank-Side Zero Sum (contra pair on bank statement)
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
                # use self._tol (DEFAULT)
                if not _amounts_equal(b1_amt, b2_amt, self._tol):
                    continue

                sim  = text_similarity(
                    _safe_str(b1, "narration"), _safe_str(b2, "narration")
                )
                # cross-month spanning window is naturally handled here
                # because no hard day cap was applied — the 90-day threshold is
                # already generous, but we keep it as-is (zero-sum pairs can span
                # months legitimately).
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


    # Strategy 11 — Ledger-Side Zero Sum (contra pair in ledger)
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
                # use self._tol (DEFAULT)
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


    # Strategy 12 — Aggregated Split Transactions (1 Ledger : N Bank)
    def match_aggregated_transactions(self) -> None:
        # extend 3-day window by _CROSS_MONTH_BUFFER
        window = 3 + self._CROSS_MONTH_BUFFER
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
                and 0 <= diff <= window
            ]

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

    # Strategy 13 — Many-to-One Aggregation (N Ledger : 1 Bank)

    def match_many_to_one_aggregation(self) -> None:
        # extend  window by _CROSS_MONTH_BUFFER
        window = 3 + self._CROSS_MONTH_BUFFER
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
                and 0 <= diff <= window
            ]

            found = None
            cap   = min(self.MAX_COMBINATION_SIZE + 1, len(valid) + 1)
            for r in range(2, cap):
                for combo in itertools.combinations(valid, r):
                    # use self._tol (DEFAULT)
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


    # Strategy 14 — One-to-Many Split Charge  (1 Ledger : 1 Bank where bank = 2×)
    def match_split_charge(self) -> None:
        """
        Matches a single bank debit (B) that is exactly 2× a single ledger
        amount (L).  Posts the pair as a match and routes the unrecorded second
        half (= L.amount) to the audit queue.

        the original code used `break` after the first match, skipping
        the rest of the pool.  Changed to `continue` so every qualifying pair
        in the dataset is captured in a single pass.
        """
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

                # use self._tol (DEFAULT)
                if not _amounts_equal(b_amt, l_amt * 2, self._tol):
                    continue

                # use cross-month buffer (30 + 7 = 37 day window)
                if abs((b_date - l_date).days) > 30 + self._CROSS_MONTH_BUFFER:
                    continue

                matched_gl = gl
                break   # at most one ledger record per bank row

            if matched_gl is None:
                continue    # was `break` — now `continue` to scan all bank rows

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

            # Route the unrecorded second half to the audit queue
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


    # Strategy 15 — Base Fee + Tax Aggregation (1 Ledger : 2 Bank rows)
    def match_base_fee_plus_tax(self) -> None:
        """
        Matches a lumped ledger expense (e.g. ₹1,392.40) against any two
        unmatched bank debits of the same direction whose combined total equals
        the ledger amount, provided both bank rows fall within a ±2 day window
        of each other.

        the original strategy assumed B1 and B2 were array-adjacent
        (bank_pool[i] and bank_pool[i+1]).  Earlier strategies delete rows,
        destroying adjacency.  This rewrite scans all remaining pairs by date
        proximity regardless of array index.
        """
        # extend 2-day adjacency window by _CROSS_MONTH_BUFFER so a
        # fee posted on Apr 30 and its GST row posted on May 1 are still paired.
        adjacency_days = 2 + self._CROSS_MONTH_BUFFER

        for gl in list(self.ledger_pool):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or not _safe_str(gl, "transaction_date"):
                continue

            # Candidate bank rows: same direction, non-zero, have a date
            candidates = [
                b for b in self.bank_pool
                if (self.bank_out(b) if is_out else self.bank_in(b)) > 0
                and _safe_str(b, "date")
            ]

            found_pair: Optional[tuple] = None

            # iterate all (i, j) pairs, not just adjacent indices
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

                    # Both rows must be within adjacency_days of each other
                    day_gap = _days_between(b1_d, b2_d)
                    if day_gap is None or abs(day_gap) > adjacency_days:
                        continue

                    # Combined total must equal the lumped ledger amount
                    # use self._tol (DEFAULT)
                    if not _amounts_equal(b1_amt + b2_amt, gl_amt, self._tol):
                        continue

                    found_pair = (b1, b1_amt, b2, b2_amt)
                    break   # inner loop: found a pair for this ledger row

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
                    f"Rows not required to be adjacent — matched by date proximity "
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
        """
        Execute all 17 strategies in strict priority order.

        Stage 0  — Pre-match cleansing / flagging
        Stage 1-9  — Standard per-record heuristics
        Stage 10-13 — Zero-sum and aggregation strategies
        Stage 14-15 — Residual specialised strategies

        Each strategy removes records it claims from ledger_pool / bank_pool
        so later strategies only see truly unmatched records.
        """

        # Stage 0: Pre-match cleansing 
        self.cleanse_zero_amount_metadata()   # 0a — drop zero-amount header rows
        self.flag_ghost_reversals()           # 0b — tag reversals; leave in pool

        # ── Stage 1-9: Standard heuristics ───────────────────────────────────
        self.match_deposit_in_transit()       # 1 — same amount, bank clears 1-5d late
        self.match_outstanding_checks()       # 2 — same amount, bank clears 1-14d late
        self.match_bank_service_charges()     # 3 — bank > ledger by small embedded fee
        self.match_text_similarity()          # 4 — amount match + narration similarity
        self.match_transposition_errors()     # 5 — same digit-multiset, different order
        self.match_rounding_differences()     # 6 — ≤ rounding_tol gap, high similarity
        self.match_nsf_returned_items()       # 7 — NSF / bounced keywords
        self.match_interest_income()          # 8 — standalone interest credit
        self.match_discounts_and_taxes()      # 9 — exact common discount/TDS rates

        # Stage 10-13: Zero-sum & aggregations 
        self.match_bank_side_zero_sum()       # 10 — contra pair on bank statement
        self.match_ledger_side_zero_sum()     # 11 — contra pair in ledger
        self.match_aggregated_transactions()  # 12 — 1 ledger = N bank rows
        self.match_many_to_one_aggregation()  # 13 — N ledger rows = 1 bank row

        # Stage 14-15: Specialised residuals 
        self.match_split_charge()             # 14 — bank = 2× ledger
        self.match_base_fee_plus_tax()        # 15 — ledger = fee + GST 

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
    """
    Wraps FuzzyMatcher.run().

    Parameters
    ----------
    pending_ledger
        PENDING_FUZZY_LEDGER output from Phase 1 exact_matcher().
    pending_bank
        PENDING_FUZZY_BANK output from Phase 1 exact_matcher().
    tolerances
        Tolerance dict from __init__.py TOLERANCES.  Keys used:
            DEFAULT, TIMING_DIFFERENCE, ROUNDING_DIFFERENCE, TRANSPOSITION.
    same_side
        True  = cashbook format (GL Debit = money out).
        False = standard double-entry GL.

    Returns
    -------
    dict with keys:
        FUZZY_MATCHES        — all matched pairs / groups
        UNRECONCILED_ITEMS   — {"ledger": [...], "bank": [...]}
        IGNORED_METADATA     — zero-amount rows dropped pre-match
        AUDIT_INVESTIGATION  — ghost reversal rows and split-charge residuals
    """
    print("[fuzzy_matcher] starting")
    return FuzzyMatcher(
        pending_ledger,
        pending_bank,
        same_side  = same_side,
        tolerances = tolerances,
    ).run()
