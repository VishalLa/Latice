from typing import List, Optional, Tuple

from .fuzzy_match import fuzzy_matcher
from .exact_match import exact_match
from .ai_matcher import ai_matcher
from .confidence import annotate_match_confidence
from .same_side_detect import detect_same_side
from .memory_match import memory_matcher, MatchMemory
from .residual_reconciler import reconcile_residuals

from langchain_ollama import ChatOllama

TOLERANCES = {
    "EXACT":              0.0,
    "ROUNDING_DIFFERENCE": 2.0,
    "TIMING_DIFFERENCE":   2.0,   
    "AI_MATCHER":          5.0,
    "TRANSPOSITION":       0.0,   
    "DEFAULT":             1.0,
}
LOW_QUALITY_CONFIDENCE_BAR: float = 0.5

def _quality_summary(all_matches: list) -> dict:
    if not all_matches:
        return {
            "average_confidence":           None,
            "low_confidence_match_count":    0,
            "low_confidence_match_pct":      0.0,
            "by_adjustment_type":            {},
        }

    confidences = [m.get("confidence_numeric", 0.0) for m in all_matches]
    avg = sum(confidences) / len(confidences)

    low_count = sum(1 for c in confidences if c < LOW_QUALITY_CONFIDENCE_BAR)
    low_pct   = round(100.0 * low_count / len(all_matches), 1)

    by_type: dict = {}
    for m in all_matches:
        label = m.get("adjustment_type") or m.get("match_phase") or "Unlabeled"
        bucket = by_type.setdefault(label, {"count": 0, "confidence_sum": 0.0})
        bucket["count"] += 1
        bucket["confidence_sum"] += m.get("confidence_numeric", 0.0)

    by_adjustment_type = {
        label: {
            "count":              v["count"],
            "average_confidence": round(v["confidence_sum"] / v["count"], 3),
        }
        for label, v in by_type.items()
    }

    return {
        "average_confidence":        round(avg, 3),
        "low_confidence_match_count": low_count,
        "low_confidence_match_pct":   low_pct,
        "by_adjustment_type":         by_adjustment_type,
    }


def reconcile(
    ledger_result: dict,
    bank_result: dict,
    all_warnings: list,
    same_side: bool = True,
    auto_detect_same_side: bool = True,
    memory:  Optional["MatchMemory"] = None,
    llm: Optional[ChatOllama] = None,
    enable_residual_reconciliation: bool = True
) -> dict:
    gl_records   = ledger_result["records"]
    bank_records = bank_result["records"]

    if auto_detect_same_side:
        detection = detect_same_side(gl_records, bank_records)
        if detection.confident and detection.same_side != same_side:
            all_warnings.append(
                f"same_side is set to {same_side}, but inspecting the first "
                f"{detection.sample_size} records suggests the ledger/bank "
                f"pair looks like same_side={detection.same_side} "
                f"({detection.reason}). If matches look wrong, try passing "
                f"same_side={detection.same_side} explicitly."
            )

    exact_match_result = exact_match(
        gl_records=gl_records,
        bank_records=bank_records,
        same_side=same_side,
        amount_tol=TOLERANCES.get("EXACT", 0.0)
    )

    fuzzy_match_result = fuzzy_matcher(
        pending_ledger=exact_match_result.get("PENDING_FUZZY_LEDGER", []),
        pending_bank=exact_match_result.get("PENDING_FUZZY_BANK", []),
        tolerances=TOLERANCES,
        same_side=same_side
    )

    residual_ledger = fuzzy_match_result["UNRECONCILED_ITEMS"]["ledger"]
    residual_bank   = fuzzy_match_result["UNRECONCILED_ITEMS"]["bank"]

    memory_matches: List[dict] = []

    if memory is not None and residual_ledger and residual_bank:
        memory, residual_ledger, residual_bank = memory_matcher(
            residual_bank=residual_bank,
            residual_ledger=residual_ledger,
            memory=memory,
            same_side=same_side,
            amount_tol=TOLERANCES.get("TIMING_DIFFERENCE", 2.0)
        )

    ai_input_payload = {
        "UNRECONCILED_LEDGER": residual_ledger,
        "UNRECONCILED_BANK":   residual_bank,
        "MATCHED":             [],
    }

    ai_pipeline_output = ai_matcher(
        result=ai_input_payload,
        tol=TOLERANCES.get("AI_MATCHER", 5.0),
        same_side=same_side,
        llm=llm
    )
    ai_matcher_result = ai_pipeline_output["FINAL_RESULT"]

    combined_ai_matches = (
        ai_result.get("AI_MATCHES",      []) +
        ai_result.get("AI_MANY_MATCHES", [])
    )

    ai_audit_queue = ai_result.get("AUDIT_QUEUE", [])

    final_unreconciled = {
        "ledger": ai_result.get("FINAL_RESIDUALS_LEDGER", []),
        "bank":   ai_result.get("FINAL_RESIDUALS_BANK",   []),
    }

    ai_skipped = ai_result.get("ai_skipped", False)
    if ai_skipped:
        all_warnings.append(
            f"AI matcher unavailable and was skipped: "
            f"{ai_result.get('ai_skip_reason', 'unknown error')}. "
            f"Residual records require manual review."
        )

    residual_result: Optional[dict] = None
    if enable_residual_reconciliation and (final_unreconciled["ledger"] or final_unreconciled["bank"]):
        residual_result = reconcile_residuals(
            unreconciled_ledger=final_unreconciled["ledger"],
            unreconciled_bank=final_unreconciled["bank"],
            same_side=same_side,
            llm=llm,
        )
        if residual_result["stats"].get("ai_skipped"):
            all_warnings.append(
                f"Residual journal-entry drafting fell back to heuristic rules: "
                f"{residual_result['stats'].get('ai_skip_reason', 'AI layer unavailable')}."
            )
        # Shrink the final unreconciled pool to whatever steps 2-5 didn't resolve.
        final_unreconciled = {
            "ledger": residual_result["still_unreconciled_ledger"],
            "bank":   residual_result["still_unreconciled_bank"],
        }

    exact_matches_annotated = [
        annotate_match_confidence({**m, "match_phase": "exact"})
        for m in exact_match_result.get("EXACT_MATCHES", [])
    ]
    fuzzy_matches_annotated = [
        annotate_match_confidence({**m, "match_phase": "fuzzy"})
        for m in fuzzy_match_result.get("FUZZY_MATCHES", [])
    ]
    ai_matches_annotated = [
        annotate_match_confidence({**m, "match_phase": "ai"})
        for m in combined_ai_matches
    ]
    ai_audit_queue_annotated = [
        annotate_match_confidence({**m, "match_phase": "ai_audit_queue"})
        for m in ai_audit_queue
    ]

    memory_matches_annotated = [
        annotate_match_confidence(m) for m in memory_matches
    ]

    all_matches_for_quality = (
        exact_matches_annotated + fuzzy_matches_annotated +
        ai_matches_annotated + memory_matches_annotated
    )
    quality = _quality_summary(all_matches_for_quality)

    memory_stats: Optional[dict] = None
    if memory is not None:
        gl_by_id = {
            str(getattr(gl, "ledger_id", "")): getattr(gl, "account_name", "")
            for gl in gl_records
        }
        bank_by_id = {
            str(getattr(b, "row_index", "")): getattr(b, "narration", "")
            for b in bank_records
        }

        memory.record_matches_from_records(
            exact_match_result.get("EXACT_MATCHES", []), gl_by_id, bank_by_id, "exact"
        )
        memory.record_matches_from_records(
            fuzzy_match_result.get("FUZZY_MATCHES", []), gl_by_id, bank_by_id, "fuzzy"
        )
        memory.record_matches_from_records(
            combined_ai_matches, gl_by_id, bank_by_id, "ai"
        )
        memory.record_matches_from_records(
            memory_matches, gl_by_id, bank_by_id, "memory"
        )
        memory_stats = memory.stats()

    return {
        "bank_name":        bank_result.get("bank_name"),
        "template_version": bank_result.get("template_version"),
        "summary": {
            "ledger_records":      len(gl_records),
            "bank_records":        len(bank_records),
            "exact_matches":       len(exact_match_result.get("EXACT_MATCHES", [])),
            "fuzzy_matches":       len(fuzzy_match_result.get("FUZZY_MATCHES", [])),
            "memory_matches":      len(memory_matches_annotated),
            "ai_matches":          len(combined_ai_matches),
            "ai_audit_queue":      len(ai_audit_queue),
            "unreconciled_ledger": len(final_unreconciled["ledger"]),
            "unreconciled_bank":   len(final_unreconciled["bank"]),
            "ai_skipped":          ai_skipped,
            "match_quality":       quality,
            "memory":              memory_stats,
            "residual_reconciliation": (
                residual_result["stats"] if residual_result else None
            ),
        },
        "EXACT_MATCHES":      exact_matches_annotated,
        "FUZZY_MATCHES":      fuzzy_matches_annotated,
        "MEMORY_MATCHES":     memory_matches_annotated,
        "AI_MATCHES":         ai_matches_annotated,
        "AI_AGENT":           ai_matches_annotated,
        "AI_AUDIT_QUEUE":     ai_audit_queue_annotated,  # low-confidence → human review
        "RESIDUAL_TIMING_MATCHES":    (residual_result or {}).get("timing_matches", []),
        "RESIDUAL_SPLIT_MATCHES":     (residual_result or {}).get("split_matches", []),
        "SUGGESTED_JOURNAL_ENTRIES":  (residual_result or {}).get("suggested_journal_entries", []),
        "HUMAN_REVIEW_QUEUE":         (residual_result or {}).get("human_review_queue", []),
        "UNRECONCILED_ITEMS": final_unreconciled,
        "IGNORED_METADATA":   fuzzy_match_result.get("IGNORED_METADATA",   []),
        "AUDIT_INVESTIGATION": fuzzy_match_result.get("AUDIT_INVESTIGATION", []),
        "warnings":           all_warnings,
    }
