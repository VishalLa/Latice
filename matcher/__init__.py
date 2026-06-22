from .fuzzy_match import fuzzy_matcher
from .exact_match import exact_matcher
from .ai_matcher import ai_agent_match


def reconcile(
    ledger_result: dict, 
    bank_result: dict,
    all_warnings: list
) -> dict: 
    
    gl_records   = ledger_result["records"]     # List[LedgerFormat]
    bank_records = bank_result["records"]        # List[BankStatement]


    exact_match_result = exact_matcher(gl_records, bank_records)

    fuzzy_match_result = fuzzy_matcher(
        exact_match_result["PENDING_FUZZY_LEDGER"],
        exact_match_result["PENDING_FUZZY_BANK"],
    )

    ai_result = ai_agent_match(
        fuzzy_match_result["UNRECONCILED_ITEMS"]["ledger"],   # List[LedgerFormat]
        fuzzy_match_result["UNRECONCILED_ITEMS"]["bank"],     # List[BankStatement]
    )

    # Merge fuzzy's low-confidence queue with new AI semantic matches
    combined_ai_queue = fuzzy_match_result["AI_AGENT"] + ai_result["NEW_AI_MATCHES"]

    # leftovers (FINAL_ arrays already exclude AI-matched items)
    final_unreconciled = {
        "ledger": ai_result["FINAL_UNRECONCILED_LEDGER"],
        "bank":   ai_result["FINAL_UNRECONCILED_BANK"],
    }


    # Build and return the final pipeline result

    return {
        "bank_name":        bank_result.get("bank_name"),
        "template_version": bank_result.get("template_version"),
        "summary": {
            "ledger_records":      len(gl_records),
            "bank_records":        len(bank_records),
            "exact_matches":       len(exact_match_result["EXACT_MATCHES"]),
            "fuzzy_matches":       len(fuzzy_match_result["FUZZY_MATCHES"]),
            "ai_matches":          len(ai_result["NEW_AI_MATCHES"]),
            "ai_agent_queue":      len(combined_ai_queue),          
            "unreconciled_ledger": len(final_unreconciled["ledger"]),
            "unreconciled_bank":   len(final_unreconciled["bank"]),
        },
        "EXACT_MATCHES":     exact_match_result["EXACT_MATCHES"],
        "FUZZY_MATCHES":     fuzzy_match_result["FUZZY_MATCHES"],
        "AI_MATCHES":        ai_result["NEW_AI_MATCHES"],   
        "AI_AGENT":          combined_ai_queue,             
        "UNRECONCILED_ITEMS": final_unreconciled,           
        "warnings":          all_warnings,
    }


