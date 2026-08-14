from __future__ import annotations

import re
import itertools
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.prompts import ChatPromptTemplate

from schema import BankStatement, LedgerFormat, DraftAccountSuggestion

from .helper import (
    _amounts_equal,
    _days_between,
    _safe_str,
    text_similarity
)
from .ai_utils import (
    _directions_compatible,
    _get_direction_amount,
    _safe_parse_json,
    get_shared_llm
)
from .fuzzy_match import FuzzyMatcher

TIMING_WINDOW_DAYS: int = 45
SPLIT_TOLERANCE: float = 2.0
SPLIT_MAX_COMBINATION_SIZE: int = 8
SPLIT_POOL_PREFILTER_THRESHOLD: int = 50
SPLIT_PROBE_MAX_SIZE: int = 4
NARRATION_SIMILARITY_THRESHOLD: float = 0.6
EXISTENCE_AMOUNT_TOLERANCE: float = 1.0

CLASSIFY_TIMING = "TIMING_CANDIDATE"
CLASSIFY_SPLIT = "SPLIT_CANDIDATE"
CLASSIFY_MISSING = "MISSING_ENTRY_CANDIDATE"
CLASSIFY_UNCLASSIFIABLE = "UNCLASSIFIABLE"

_WORD_RE = re.compile(r"[a-zA-Z]{3,}")


def _identity_remove(pool: List[Any], item: Any) -> None:
    for i, p in enumerate(pool):
        if p is item:
            del pool[i]
            return


def _identity_contains(pool: list[Any], item: Any) -> bool:
    return any(p is item for p in pool)


def _narration_is_draftable(narration: str) -> bool:
    return len(_WORD_RE.findall(narration or "")) >= 1


def _cheap_subset_probe(
    target: float,
    pool: List[Tuple[Any, float]],
    tol: float,
    max_size: int = SPLIT_PROBE_MAX_SIZE,
    text_of=None,
    target_text: str = "",
) -> bool:
    if target <= 0 or not pool:
        return False
        
    pool = FuzzyMatcher._closest_candidates(
        pool, target, FuzzyMatcher.MAX_COMBINATION_POOL_SIZE,
        counterpart_text=target_text, text_of=text_of,
    )

    cap = min(max_size, len(pool)) + 1
    for r in range(2, cap):
        for combo in itertools.combinations(pool, r):
            if _amounts_equal(target, sum(x[1] for x in combo), tol):
                return True
    return False


def _classify_one(
    item: Any,
    is_bank: bool,
    counterpart_pool: List[Any],
    same_side: bool,
    default_tol: float,
) -> Dict[str, Any]:
    amt, direction = _get_direction_amount(item)
    text = _safe_str(item, "narration") if is_bank else _safe_str(item, "account_name")

    if amt <= 0:
        return {"label": CLASSIFY_UNCLASSIFIABLE, "reason": "zero or invalid amount"}

    comparable: List[Tuple[Any, float]] = []
    for cp in counterpart_pool:
        cp_amt, cp_dir = _get_direction_amount(cp)

        if cp_amt <= 0 or not _directions_compatible(
            cp_dir if not is_bank else direction,
            direction if not is_bank else cp_dir,
            same_side,
        ):
            continue
        comparable.append((cp, cp_amt))

    existence_hit = next(
        (cp for cp, cp_amt in comparable if _amounts_equal(amt, cp_amt, default_tol)),
        None,
    )

    def cp_text(cp: Any) -> str:
        return _safe_str(cp, "account_name") if is_bank else _safe_str(cp, "narration")

    best_sim, best_sim_cp = 0.0, None
    if not is_bank:
        for cp, _ in comparable:
            sim = text_similarity(cp_text(cp), text)
            if sim > best_sim:
                best_sim, best_sim_cp = sim, cp

    probe_pool = [(cp, cp_amt) for cp, cp_amt in comparable if cp is not existence_hit]
    split_hit = _cheap_subset_probe(
        amt, probe_pool, SPLIT_TOLERANCE,
        text_of=cp_text, target_text=text,
    )

    evidence = {
        "amount": amt,
        "direction": direction,
        "existence_signal": existence_hit is not None,
        "split_signal": split_hit,
        "narration_signal": best_sim >= NARRATION_SIMILARITY_THRESHOLD,
        "best_narration_similarity": round(best_sim, 3),
        "best_narration_match": (
            (_safe_str(best_sim_cp, "ledger_id") if is_bank else str(getattr(best_sim_cp, "row_index", None)))
            if best_sim_cp is not None else None
        ),
    }

    if split_hit:
        label = CLASSIFY_SPLIT
    elif existence_hit is not None:
        label = CLASSIFY_TIMING
    elif is_bank and _narration_is_draftable(text):
        label = CLASSIFY_MISSING
    elif not is_bank and best_sim >= NARRATION_SIMILARITY_THRESHOLD:
        label = CLASSIFY_MISSING
    else:
        label = CLASSIFY_UNCLASSIFIABLE

    return {"label": label, "evidence": evidence}


def classify_residuals(
    residual_ledger: List[LedgerFormat],
    residual_bank: List[BankStatement],
    same_side: bool = True,
    default_tol: float = EXISTENCE_AMOUNT_TOLERANCE,
) -> Dict[str, List[Dict[str, Any]]]:

    bank_out = []
    for b in residual_bank:
        c = _classify_one(b, True, residual_ledger, same_side, default_tol)
        bank_out.append({"item": b, **c})

    ledger_out = []
    for gl in residual_ledger:
        c = _classify_one(gl, False, residual_bank, same_side, default_tol)
        ledger_out.append({"item": gl, **c})

    return {"bank": bank_out, "ledger": ledger_out}


def _resolve_timing_candidates(
    timing_bank: List[Dict[str, Any]],
    timing_ledger: List[Dict[str, Any]],
    same_side: bool,
    tol: float = EXISTENCE_AMOUNT_TOLERANCE,
) -> Tuple[List[dict], List[Dict[str, Any]], List[Dict[str, Any]]]:

    bank_pool = list(timing_bank)
    ledger_pool = list(timing_ledger)
    matches: List[dict] = []

    for gl_entry in list(ledger_pool):
        gl = gl_entry["item"]
        gl_amt, gl_dir = _get_direction_amount(gl)
        gl_date = _safe_str(gl, "transaction_date")
        if gl_amt <= 0 or not gl_date:
            continue

        candidates = []
        for bank_entry in bank_pool:
            bank = bank_entry["item"]
            bk_amt, bk_dir = _get_direction_amount(bank)
            bk_date = _safe_str(bank, "date")
            if bk_amt <= 0 or not bk_date:
                continue
            if not _amounts_equal(gl_amt, bk_amt, tol):
                continue
            if not _directions_compatible(gl_dir, bk_dir, same_side):
                continue
            diff = _days_between(gl_date, bk_date)
            if diff is None or not (0 <= abs(diff) <= TIMING_WINDOW_DAYS):
                continue
            candidates.append((bank_entry, diff))

        if not candidates:
            continue

        candidates.sort(key=lambda x: abs(x[1]))
        bank_entry, diff = candidates[0]
        ambiguous = len(candidates) > 1
        n_candidates = len(candidates)

        bank = bank_entry["item"]
        matches.append({
            "ledger_id": _safe_str(gl, "ledger_id"),
            "bank_id": getattr(bank, "row_index", None),
            "amount": gl_amt,
            "date_gap_days": diff,
            "adjustment_type": "Timing Difference (wide window)",
            "confidence_score": "Medium" if ambiguous else "High",
            "ambiguous": ambiguous,
            "details": (
                f"Exact amount ({gl_amt:.2f}) matched outside the standard "
                f"window; {diff} day(s) apart, within the {TIMING_WINDOW_DAYS}-day "
                f"residual-pool timing pass." + (
                    f" {n_candidates} candidates matched the same amount and "
                    "direction in this window - closest date was picked, but "
                    "flagged for review rather than auto-accepted." if ambiguous else ""
                )
            ),
            "match_phase": "residual_timing",
        })
        _identity_remove(ledger_pool, gl_entry)
        _identity_remove(bank_pool, bank_entry)

    return matches, ledger_pool, bank_pool


def _subset_sum_search(
    target: float,
    pool: List[Tuple[Any, float]],
    tol: float,
    max_size: int,
) -> Tuple[Optional[Tuple[Tuple[Any, float], ...]], bool]:
    cap = min(max_size, len(pool)) + 1
    first = None
    
    for r in range(2, cap):
        for combo in itertools.combinations(pool, r):
            if _amounts_equal(target, sum(x[1] for x in combo), tol):
                if first is None:
                    first = combo
                else:
                    return first, True

    return (first, False) if first is not None else (None, False)


def _resolve_split_candidates(
    split_bank_targets: List[Dict[str, Any]],
    split_ledger_targets: List[Dict[str, Any]],
    ledger_candidate_pool: List[Dict[str, Any]],
    bank_candidate_pool: List[Dict[str, Any]],
    same_side: bool,
) -> Tuple[List[dict], List[Dict[str, Any]], List[Dict[str, Any]]]:

    bank_pool = list(bank_candidate_pool)
    ledger_pool = list(ledger_candidate_pool)
    bank_targets = [e for e in split_bank_targets if _identity_contains(bank_pool, e)]
    ledger_targets = [e for e in split_ledger_targets if _identity_contains(ledger_pool, e)]
    matches: List[dict] = []

    def _prefilter(pool: List[Dict[str, Any]], target: float, target_dir: str) -> List[Tuple[Any, float]]:
        pairs = []

        for entry in pool:
            item = entry["item"]
            amt, item_dir = _get_direction_amount(item)
            if amt <= 0:
                continue

            if not _directions_compatible(target_dir, item_dir, same_side):
                continue

            pairs.append((entry, amt))

        if len(pairs) > SPLIT_POOL_PREFILTER_THRESHOLD:
            pairs = [(e, a) for e, a in pairs if a <= target]
        return pairs

    for bank_entry in list(bank_targets):
        if not _identity_contains(bank_pool, bank_entry):
            continue

        bank = bank_entry["item"]
        b_amt, b_dir = _get_direction_amount(bank)
        if b_amt <= 0:
            continue

        candidates = _prefilter(ledger_pool, b_amt, b_dir)
        candidates = FuzzyMatcher._closest_candidates(
            candidates, b_amt, FuzzyMatcher.MAX_COMBINATION_POOL_SIZE,
            counterpart_text=_safe_str(bank, "narration"),
            text_of=lambda e: _safe_str(e["item"], "account_name"),
        )

        found, ambiguous = _subset_sum_search(b_amt, candidates, SPLIT_TOLERANCE, SPLIT_MAX_COMBINATION_SIZE)
        if not found:
            continue

        gl_entries = [x[0] for x in found]
        gl_components = [
            {"ledger_id": _safe_str(entry["item"], "ledger_id"), "amount": comp_amt}
            for entry, comp_amt in found
        ]

        matches.append({
            "bank_id": getattr(bank, "row_index", None),
            "ledger_id": " & ".join(c["ledger_id"] for c in gl_components),
            "ledger_components": gl_components,
            "amount": b_amt,
            "adjustment_type": f"Split Match ({len(gl_entries)} ledger items)",
            "confidence_score": "Medium" if ambiguous else "High",
            "ambiguous": ambiguous,
            "details": (
                f"Bank amount {b_amt:.2f} = sum of {len(gl_entries)} residual "
                f"ledger entries." + (" Multiple valid combinations exist - "
                "flagged for review rather than auto-accepted." if ambiguous else "")
            ),
            "match_phase": "residual_split",
        })
        _identity_remove(bank_pool, bank_entry)

        for e in gl_entries:
            _identity_remove(ledger_pool, e)

    for gl_entry in list(ledger_targets):
        if not _identity_contains(ledger_pool, gl_entry):
            continue

        gl = gl_entry["item"]
        g_amt, g_dir = _get_direction_amount(gl)
        if g_amt <= 0:
            continue

        candidates = _prefilter(bank_pool, g_amt, g_dir)
        candidates = FuzzyMatcher._closest_candidates(
            candidates, g_amt, FuzzyMatcher.MAX_COMBINATION_POOL_SIZE,
            counterpart_text=_safe_str(gl, "account_name"),
            text_of=lambda e: _safe_str(e["item"], "narration"),
        )

        found, ambiguous = _subset_sum_search(g_amt, candidates, SPLIT_TOLERANCE, SPLIT_MAX_COMBINATION_SIZE)
        if not found:
            continue

        bank_entries = [x[0] for x in found]
        bank_components = [
            {"bank_id": getattr(entry["item"], "row_index", None), "amount": comp_amt}
            for entry, comp_amt in found
        ]

        matches.append({
            "ledger_id": _safe_str(gl, "ledger_id"),
            "bank_id": " & ".join(str(c["bank_id"]) for c in bank_components),
            "bank_components": bank_components,
            "amount": g_amt,
            "adjustment_type": f"Split Match ({len(bank_entries)} bank items)",
            "confidence_score": "Medium" if ambiguous else "High",
            "ambiguous": ambiguous,
            "details": (
                f"Ledger amount {g_amt:.2f} = sum of {len(bank_entries)} residual "
                f"bank entries." + (" Multiple valid combinations exist - "
                "flagged for review rather than auto-accepted." if ambiguous else "")
            ),
            "match_phase": "residual_split",
        })
        _identity_remove(ledger_pool, gl_entry)

        for e in bank_entries:
            _identity_remove(bank_pool, e)

    return matches, ledger_pool, bank_pool


_DRAFT_PROMPT_SYSTEM = (
    "You are an accountant drafting a journal entry for a bank transaction that "
    "has no matching ledger entry. You are given the bank narration only - you "
    "do not decide the amount or direction, only which account to post against "
    "the bank account, and a short plain-English narrative.\n"
    "Prefer standard account names (Bank Charges A/c, Interest Received A/c, "
    "Interest Paid A/c, Suspense A/c) when the narration matches a generic "
    "bank-fee or interest pattern. Use a vendor/party name from the narration "
    "when one is clearly present (e.g. a UPI credit naming a person or business).\n"
    "If the narration is too garbled or generic to infer anything reliable, "
    "propose 'Suspense A/c' and confidence below 0.4.\n"
    "OUTPUT FORMAT: Strict JSON ONLY. No markdown.\n"
    'Schema: {{"counter_account": "string", "entry_narrative": "string", "confidence": 0.0-1.0}}'
)


def _heuristic_draft_fallback(narration: str) -> DraftAccountSuggestion:
    n = (narration or "").upper()
    if "CHARGE" in n or "FEE" in n or "AMC" in n:
        return DraftAccountSuggestion(
            counter_account="Bank Charges A/c",
            entry_narrative="Likely a bank fee or service charge.",
            confidence=0.55,
        )

    if "INT" in n and ("CREDIT" in n or "CR" in n):
        return DraftAccountSuggestion(
            counter_account="Interest Received A/c",
            entry_narrative="Likely interest credited by the bank.",
            confidence=0.55,
        )

    return DraftAccountSuggestion(
        counter_account="Suspense A/c",
        entry_narrative="Narration did not match a recognizable pattern; needs review.",
        confidence=0.25,
    )


def _generate_journal_drafts(
    missing_bank: List[Dict[str, Any]],
    llm=None,
) -> Tuple[List[dict], List[Dict[str, Any]], bool, Optional[str]]:

    if not missing_bank:
        return [], [], False, None

    using_shared = llm is None
    if using_shared:
        llm = get_shared_llm()

    llm_available = True
    skip_reason: Optional[str] = None
    try:
        llm.invoke("ping")
    except Exception as conn_err:
        llm_available = False
        skip_reason = str(conn_err)

    drafts: List[dict] = []
    still_unresolved: List[Dict[str, Any]] = []

    prompt_template = None
    if llm_available:
        prompt_template = ChatPromptTemplate.from_messages([
            ("system", _DRAFT_PROMPT_SYSTEM),
            ("human", "BANK NARRATION: {narration}\nAMOUNT: {amount:.2f}\nDIRECTION: {direction}"),
        ])

    for entry in missing_bank:
        bank = entry["item"]
        amt, direction = _get_direction_amount(bank)
        narration = _safe_str(bank, "narration")

        suggestion: Optional[DraftAccountSuggestion] = None
        source = "llm"
        if llm_available and prompt_template is not None:
            try:
                chain = prompt_template | llm
                raw = chain.invoke({"narration": narration, "amount": amt, "direction": direction})
                suggestion = _safe_parse_json(raw, DraftAccountSuggestion)
            except Exception:
                suggestion = None

        if suggestion is None:
            suggestion = _heuristic_draft_fallback(narration)
            source = "heuristic_fallback"

        if direction == "debit":
            debit_account, credit_account = suggestion.counter_account, "Bank A/c"
        else:
            debit_account, credit_account = "Bank A/c", suggestion.counter_account

        drafts.append({
            "bank_id": getattr(bank, "row_index", None),
            "date": _safe_str(bank, "date"),
            "amount": amt,
            "narration": narration,
            "debit_account": debit_account,
            "credit_account": credit_account,
            "entry_narrative": suggestion.entry_narrative,
            "confidence": round(suggestion.confidence, 2),
            "source": source,
            "status": "pending_review",
        })

    return drafts, still_unresolved, not llm_available, skip_reason


def _closest_context(
    item: Any,
    is_bank: bool,
    counterpart_pool: List[Any],
    same_side: bool,
    top_n: int = 3,
) -> List[dict]:
    amt, direction = _get_direction_amount(item)
    if is_bank:
        text = _safe_str(item, "narration")
        date_ = _safe_str(item, "date")
    else:
        text = _safe_str(item, "account_name")
        date_ = _safe_str(item, "transaction_date")

    scored = []
    for cp in counterpart_pool:
        cp_amt, cp_dir = _get_direction_amount(cp)
        if is_bank:
            cp_text = _safe_str(cp, "account_name")
            cp_date = _safe_str(cp, "transaction_date")
        else:
            cp_text = _safe_str(cp, "narration")
            cp_date = _safe_str(cp, "date")

        amt_diff = abs(amt - cp_amt)
        day_gap = _days_between(date_, cp_date) if is_bank else _days_between(cp_date, date_)
        sim = text_similarity(text, cp_text) if is_bank else text_similarity(cp_text, text)
        score = amt_diff - (sim * 50) + (abs(day_gap) if day_gap is not None else 999) * 0.1
        scored.append((score, cp, amt_diff, day_gap, sim))

    scored.sort(key=lambda x: x[0])
    context = []
    for _, cp, amt_diff, day_gap, sim in scored[:top_n]:
        context.append({
            "candidate_id": (
                _safe_str(cp, "ledger_id") if is_bank else str(getattr(cp, "row_index", None))
            ),
            "amount_difference": round(amt_diff, 2),
            "date_gap_days": day_gap,
            "text_similarity": round(sim, 2),
        })
    return context


def _suggested_action(label: str, evidence: dict) -> str:
    if label == CLASSIFY_TIMING:
        return "Amount matches something on the other side outside the timing window - verify if this is a delayed clearance."
    if label == CLASSIFY_SPLIT:
        return "A combination of entries may sum to this amount - verify if this payment was split."
    if label == CLASSIFY_MISSING:
        return "No amount evidence on the other side - check if this needs to be recorded manually."
    return "No strong signal found on any check - check for a rounding adjustment or a data-quality issue (wrong date/amount/OCR error)."


def _build_review_queue(
    entries: List[Dict[str, Any]],
    is_bank: bool,
    counterpart_pool: List[Any],
    same_side: bool,
) -> List[dict]:
    queue = []
    for entry in entries:
        item = entry["item"]
        label = entry["label"]
        evidence = entry.get("evidence", {})
        queue.append({
            "side": "bank" if is_bank else "ledger",
            "id": (getattr(item, "row_index", None) if is_bank else _safe_str(item, "ledger_id")),
            "amount": evidence.get("amount", _get_direction_amount(item)[0]),
            "narration_or_account": _safe_str(item, "narration") if is_bank else _safe_str(item, "account_name"),
            "date": _safe_str(item, "date") if is_bank else _safe_str(item, "transaction_date"),
            "classification": label,
            "closest_candidates": _closest_context(item, is_bank, counterpart_pool, same_side),
            "suggested_action": _suggested_action(label, evidence),
        })
    return queue


def reconcile_residuals(
    unreconciled_ledger: List[LedgerFormat],
    unreconciled_bank: List[BankStatement],
    *,
    same_side: bool = True,
    llm=None,
) -> dict:
    print("Residual Reconciliation (Phase 4)")

    classified = classify_residuals(unreconciled_ledger, unreconciled_bank, same_side)

    timing_bank = [e for e in classified["bank"] if e["label"] == CLASSIFY_TIMING]
    timing_ledger = [e for e in classified["ledger"] if e["label"] == CLASSIFY_TIMING]
    split_bank = [e for e in classified["bank"] if e["label"] == CLASSIFY_SPLIT]
    split_ledger = [e for e in classified["ledger"] if e["label"] == CLASSIFY_SPLIT]
    missing_bank = [e for e in classified["bank"] if e["label"] == CLASSIFY_MISSING]
    missing_ledger = [e for e in classified["ledger"] if e["label"] == CLASSIFY_MISSING]
    unclass_bank = [e for e in classified["bank"] if e["label"] == CLASSIFY_UNCLASSIFIABLE]
    unclass_ledger = [e for e in classified["ledger"] if e["label"] == CLASSIFY_UNCLASSIFIABLE]

    timing_matches, timing_ledger_left, timing_bank_left = _resolve_timing_candidates(
        timing_bank, timing_ledger, same_side
    )

    ledger_pool_after_timing = timing_ledger_left + split_ledger + missing_ledger + unclass_ledger
    bank_pool_after_timing = timing_bank_left + split_bank + missing_bank + unclass_bank

    split_matches, split_ledger_left, split_bank_left = _resolve_split_candidates(
        split_bank, split_ledger,
        ledger_pool_after_timing, bank_pool_after_timing,
        same_side,
    )

    missing_bank_remaining = [e for e in split_bank_left if e["label"] == CLASSIFY_MISSING]
    non_missing_bank_remaining = [e for e in split_bank_left if e["label"] != CLASSIFY_MISSING]
    non_missing_ledger_remaining = split_ledger_left

    drafts, missing_bank_unresolved, ai_skipped, ai_skip_reason = _generate_journal_drafts(
        missing_bank_remaining, llm=llm
    )

    review_bank = non_missing_bank_remaining + missing_bank_unresolved
    review_ledger = non_missing_ledger_remaining

    human_review_queue = (
        _build_review_queue(review_bank, True, unreconciled_ledger, same_side) +
        _build_review_queue(review_ledger, False, unreconciled_bank, same_side)
    )

    stats = {
        "residual_ledger_in": len(unreconciled_ledger),
        "residual_bank_in": len(unreconciled_bank),
        "timing_resolved": len(timing_matches),
        "splits_resolved": len(split_matches),
        "drafts_generated": len(drafts),
        "still_unresolved": len(human_review_queue),
        "ai_skipped": ai_skipped,
        "ai_skip_reason": ai_skip_reason,
    }

    return {
        "timing_matches": timing_matches,
        "split_matches": split_matches,
        "suggested_journal_entries": drafts,
        "human_review_queue": human_review_queue,
        "stats": stats,
        "still_unreconciled_ledger": [e["item"] for e in review_ledger],
        "still_unreconciled_bank": [e["item"] for e in review_bank],
    }
