from .fuzzy_match import fuzzy_matcher
from .exact_match import exact_matcher
from .ai_matcher import ai_matcher_pipeline

TOLERANCES = {
    "EXACT":              0.0,
    "ROUNDING_DIFFERENCE": 2.0,
    "TIMING_DIFFERENCE":   2.0,   # Deposit in Transit, Outstanding Checks
    "AI_MATCHER":          5.0,
    "TRANSPOSITION":       0.0,   # Identical digit multiset — no variance
    "DEFAULT":             1.0,
}


def reconcile(
    ledger_result: dict,
    bank_result:   dict,
    all_warnings:  list,
) -> dict:

    gl_records   = ledger_result["records"]
    bank_records = bank_result["records"]

    exact_match_result = exact_matcher(gl_records, bank_records)

    fuzzy_match_result = fuzzy_matcher(
        exact_match_result.get("PENDING_FUZZY_LEDGER", []),
        exact_match_result.get("PENDING_FUZZY_BANK",   []),
        TOLERANCES,
    )

    ai_input_payload = {
        "UNRECONCILED_LEDGER": fuzzy_match_result["UNRECONCILED_ITEMS"]["ledger"],
        "UNRECONCILED_BANK":   fuzzy_match_result["UNRECONCILED_ITEMS"]["bank"],
        "MATCHED":             [],
    }

    ai_pipeline_output = ai_matcher_pipeline(
        ai_input_payload,
        _AMOUNT_TOL=TOLERANCES.get("AI_MATCHER", 5.0),
    )
    ai_result = ai_pipeline_output["FINAL_RESULT"]

    combined_ai_matches = (
        ai_result.get("AI_MATCHES",      []) +
        ai_result.get("AI_MANY_MATCHES", [])
    )

    # Low-confidence matches from both Phase A and Phase B
    ai_audit_queue = ai_result.get("AUDIT_QUEUE", [])

    final_unreconciled = {
        "ledger": ai_result.get("FINAL_RESIDUALS_LEDGER", []),
        "bank":   ai_result.get("FINAL_RESIDUALS_BANK",   []),
    }

    # Surface whether the AI layer was bypassed due to connection failure
    ai_skipped = ai_result.get("ai_skipped", False)
    if ai_skipped:
        all_warnings.append(
            f"AI matcher unavailable and was skipped: "
            f"{ai_result.get('ai_skip_reason', 'unknown error')}. "
            f"Residual records require manual review."
        )

    return {
        "bank_name":        bank_result.get("bank_name"),
        "template_version": bank_result.get("template_version"),
        "summary": {
            "ledger_records":      len(gl_records),
            "bank_records":        len(bank_records),
            "exact_matches":       len(exact_match_result.get("EXACT_MATCHES", [])),
            "fuzzy_matches":       len(fuzzy_match_result.get("FUZZY_MATCHES", [])),
            "ai_matches":          len(combined_ai_matches),
            "ai_audit_queue":      len(ai_audit_queue),
            "unreconciled_ledger": len(final_unreconciled["ledger"]),
            "unreconciled_bank":   len(final_unreconciled["bank"]),
            "ai_skipped":          ai_skipped,
        },
        "EXACT_MATCHES":      exact_match_result.get("EXACT_MATCHES", []),
        "FUZZY_MATCHES":      fuzzy_match_result.get("FUZZY_MATCHES", []),
        "AI_MATCHES":         combined_ai_matches,
        "AI_AGENT":           combined_ai_matches,
        "AI_AUDIT_QUEUE":     ai_audit_queue,        # low-confidence → human review
        "UNRECONCILED_ITEMS": final_unreconciled,
        "IGNORED_METADATA":   fuzzy_match_result.get("IGNORED_METADATA",   []),
        "AUDIT_INVESTIGATION": fuzzy_match_result.get("AUDIT_INVESTIGATION", []),
        "warnings":           all_warnings,
    }


