import json
import re
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import asdict

from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from pydantic import BaseModel, Field
from schema.bank_renc_schema import BankStatement, LedgerFormat


# DEFINITIONS & PYDANTIC SCHEMAS (JSON ENFORCEMENT)

class AI1to1Match(BaseModel):
    ledger_id: str = Field(..., description="Unique Ledger ID")
    bank_id: int = Field(..., description="Bank Statement Row Index")
    confidence: float = Field(..., ge=0.0, le=1.0, description="0.0 to 1.0")
    reasoning: str = Field(..., description="Concise semantic/date/amount match explanation")


class AIWindowOutput(BaseModel):
    matches: List[AI1to1Match]


class AILedgerCandidate(BaseModel):
    ledger_id: str


class AIManyToOneMatch(BaseModel):
    bank_id: int = Field(..., description="Single matched bank row index")
    ledger_ids: List[AILedgerCandidate] = Field(..., description="Combination of ledger entries summing to bank amount")
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str


class AIManyToOneOutput(BaseModel):
    matches: List[AIManyToOneMatch]


# PHI-3 MINI OPTIMIZATION HELPERS
def _prepare_llm() -> ChatOllama:
    """Initialize Phi-3-mini with temperature=0 for deterministic, reproducible outputs."""
    return ChatOllama(
        model="phi3",
        temperature=0.0,
        num_ctx=8192,
        repeat_penalty=1.1,
        base_url="http://127.0.0.1:11434",
    )


def _safe_parse_json(llm_output: str, schema: type[BaseModel]) -> Optional[Any]:
    """Robustly extract JSON from LLM output, handling markdown fences & trailing commas."""
    # Accept either a raw string or a LangChain AIMessage object
    text = llm_output.content if hasattr(llm_output, "content") else str(llm_output)

    parser = JsonOutputParser(pydantic_object=schema)
    try:
        parsed = parser.invoke(text)
        # JsonOutputParser returns a plain dict; validate into the Pydantic model
        if isinstance(parsed, dict):
            return schema.model_validate(parsed)
        return parsed
    except Exception:
        cleaned = re.sub(r"```(?:json)?\s*", "", text).rstrip(".")
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
        # Strip any trailing non-JSON text after the closing brace/bracket
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return schema.model_validate(data)
            return data
        except Exception:
            return None


def _make_parser(schema: type[BaseModel]):
    """Return a single-argument callable suitable for use in a LangChain pipe ( | )."""
    def _parse(llm_output):
        return _safe_parse_json(llm_output, schema)
    return _parse


def _format_record_for_prompt(rec: Dict[str, Any], is_bank: bool) -> str:
    """Condense a Pydantic/dataclass row into a concise prompt string."""
    amt_col = "debit" if is_bank else "debit_amount"
    amt_val = rec.get(amt_col, 0.0)
    bal_col = "credit" if is_bank else "credit_amount"
    bal_val = rec.get(bal_col, 0.0)
    
    return (
        f"- {rec.get('ledger_id' if not is_bank else 'row_index')} | "
        f"{rec.get('transaction_date' if not is_bank else 'date')} | "
        f"Dr:{amt_val:.2f} Cr:{bal_val:.2f} | "
        f"NARR: {rec.get('account_name' if not is_bank else 'narration', 'N/A')}"
    )



# AI TIME-WINDOW BATCH MATCHER (1-to-1)
def ai_batch_matcher(unreconciled: Dict[str, Any], llm: ChatOllama, _AMOUNT_TOL: int) -> Tuple[Dict[str, Any], List[LedgerFormat], List[BankStatement]]:
    """Groups leftovers into rolling 30-day windows. Asks Phi-3 for semantic 1-to-1 matches."""
    
    gl_remaining = list(unreconciled["UNRECONCILED_LEDGER"])
    bk_remaining = list(unreconciled["UNRECONCILED_BANK"])
    ai_matches = []
    
    if not gl_remaining or not bk_remaining:
        return {
            "AI_MATCHES": ai_matches, 
            "MATCHED": unreconciled["MATCHED"]
        }, gl_remaining, bk_remaining

    # Create rolling 30-day windows
    dates = sorted([r.date for r in bk_remaining if r.date] + [r.transaction_date for r in gl_remaining if r.transaction_date])
    if not dates: return {"AI_MATCHES": ai_matches, "MATCHED": unreconciled["MATCHED"]}, gl_remaining, bk_remaining
    
    window_size = timedelta(days=30)
    current_start = datetime.strptime(dates[0][:10], "%Y-%m-%d")
    final_end = datetime.strptime(dates[-1][:10], "%Y-%m-%d")
    
    prompt_template = ChatPromptTemplate.from_messages([
        ("system", """You are a financial reconciliation expert. 
        Your task is to find 1-to-1 matches between ledger records and bank statement rows within the provided time window.
        MATCH CRITERIA: Amounts match within ±$0.05. Dates are within the window. Narration/account names share semantic meaning (e.g., abbreviations, synonyms, reversed order).
        OUTPUT FORMAT: Strict JSON ONLY. No markdown. No explanations outside the schema.
        Schema: {{"matches": [{{"ledger_id": "string", "bank_id": "number", "confidence": "0-1", "reasoning": "string"}}]}}"""),
        ("human", """TIME WINDOW: {start_date} to {end_date}\n\nLEDGER ENTRIES:\n{ledger_list}\n\nBANK ENTRIES:\n{bank_list}""")
    ])

    # Process windows in chunks to respect context window & rate limits
    while current_start < final_end:
        window_end = current_start + window_size
        
        gl_chunk = [r for r in gl_remaining if r.transaction_date and current_start.date() <= datetime.strptime(r.transaction_date[:10], "%Y-%m-%d").date() <= window_end.date()]
        bk_chunk = [r for r in bk_remaining if r.date and current_start.date() <= datetime.strptime(r.date[:10], "%Y-%m-%d").date() <= window_end.date()]
        
        # Limit context to ~20 records each to prevent Phi-3 degradation
        gl_ctx = [_format_record_for_prompt(asdict(r), False) for r in gl_chunk[:20]]
        bk_ctx = [_format_record_for_prompt(asdict(r), True) for r in bk_chunk[:20]]
        
        if not gl_ctx or not bk_ctx:
            current_start += window_size
            continue

        chain = prompt_template | llm
        raw = chain.invoke({
            "start_date": current_start.strftime("%Y-%m-%d"),
            "end_date": window_end.strftime("%Y-%m-%d"),
            "ledger_list": "\n".join(gl_ctx),
            "bank_list": "\n".join(bk_ctx)
        })
        result = _safe_parse_json(raw, AIWindowOutput)

        if result and hasattr(result, 'matches'):
            for m in result.matches:
                # 1. Grab the actual raw records from your remaining pools
                gl_item = next((r for r in gl_remaining if r.ledger_id == m.ledger_id), None)
                bk_item = next((r for r in bk_remaining if str(r.row_index) == str(m.bank_id)), None)

                if gl_item and bk_item:
                    # Get the absolute transaction amounts
                    gl_amt = gl_item.debit_amount if gl_item.debit_amount else gl_item.credit_amount
                    bk_amt = bk_item.debit if bk_item.debit else bk_item.credit

                    # 2. STRICT PYTHON MATH CHECK (The Bouncer)
                    diff = abs(gl_amt - bk_amt)
                    if diff <= _AMOUNT_TOL:
                        ai_matches.append(m.model_dump())
                        # Safely remove from pools
                        gl_remaining = [r for r in gl_remaining if r.ledger_id != m.ledger_id]
                        bk_remaining = [r for r in bk_remaining if str(r.row_index) != str(m.bank_id)]
                    else:
                        print(f"⚠️ REJECTED AI HALLUCINATION: [Ledger {m.ledger_id}] and [Bank {m.bank_id}] differed by {diff:.2f} (Exceeded {_AMOUNT_TOL} limit).")

        current_start += window_size

    return {
        "AI_MATCHES": ai_matches,
        "MATCHED": unreconciled["MATCHED"]
    }, gl_remaining, bk_remaining


# AI ONE-TO-MANY RESIDUAL MATCHER
def ai_residual_matcher(unreconciled_ledger: List[LedgerFormat], unreconciled_bank: List[BankStatement], llm: ChatOllama) -> Dict[str, Any]:
    """Iterates unmatched bank entries. Finds combos of ledger entries that sum to the bank amount."""
    
    ai_many_matches = []
    final_gl_left = list(unreconciled_ledger)
    
    if not unreconciled_bank: return {"AI_MANY_MATCHES": ai_many_matches}, final_gl_left

    prompt_template = ChatPromptTemplate.from_messages([
        ("system", """You are a financial reconciliation expert. 
        A single bank transaction corresponds to MULTIPLE ledger invoices/payments.
        TASK: Identify which ledger entries sum exactly to the bank amount (±$0.05 tolerance). Use narration semantics and date proximity as clues.
        OUTPUT FORMAT: Strict JSON ONLY. No markdown.
        Schema: {{"matches": [{{"bank_id": "number", "ledger_ids": [{{"ledger_id": "string"}}], "confidence": "0-1", "reasoning": "string"}}]}}"""),
        ("human", """BANK ENTRY:\n{bank_info}\n\nAVAILABLE LEDGER CANDIDATES (±5 days / closest amounts):\n{ledger_candidates}""")
    ])

    for bank in unreconciled_bank:
        # Gather candidate ledgers (limit to 10-12 to fit context)
        candidates = []
        for gl in final_gl_left[:15]:
            # Simple heuristic for relevance: amount proximity or date proximity
            if bank.date and gl.transaction_date:
                candidates.append(gl)
        
        if not candidates: continue
        
        ctx_ledger = "\n".join([_format_record_for_prompt(asdict(g), False) for g in candidates])
        ctx_bank = _format_record_for_prompt(asdict(bank), True)
        
        chain = prompt_template | llm
        raw = chain.invoke({"bank_info": ctx_bank, "ledger_candidates": ctx_ledger})
        result = _safe_parse_json(raw, AIManyToOneOutput)

        if result and hasattr(result, 'matches'):
            for m in result.matches:
                ai_many_matches.append(m.model_dump())
                matched_ids = [lid.ledger_id for lid in m.ledger_ids]
                final_gl_left = [r for r in final_gl_left if r.ledger_id not in matched_ids]

    return {
        "AI_MANY_MATCHES": ai_many_matches}, final_gl_left


# MAIN AI PIPELINE ORCHESTRATOR
def ai_matcher_pipeline(result: dict, _AMOUNT_TOL: int) -> Dict[str, Any]:
    """Executes the full waterfall reconciliation pipeline."""
    llm = _prepare_llm()
    
    print("🤖 AI Time-Window Batch Matcher (1-to-1 Semantic)...")
    ai_batch_matcher_res, gl_rem, bk_rem = ai_batch_matcher(result, llm, _AMOUNT_TOL)
    
    if not bk_rem:
        print("✅ All remaining records reconciled via AI Batch.")
        return {"FINAL_RESULT": {**ai_batch_matcher_res, "FINAL_RESIDUALS_LEDGER": gl_rem, "FINAL_RESIDUALS_BANK": []}}

    print("🔍 AI One-to-Many Residual Matcher...")
    ai_residual_matcher_res, final_gl_left = ai_residual_matcher(gl_rem, bk_rem, llm)
    
    return {
        "FINAL_RESULT": {
            **ai_batch_matcher_res,
            **ai_residual_matcher_res,
            "FINAL_RESIDUALS_LEDGER": final_gl_left,
            "FINAL_RESIDUALS_BANK": bk_rem,
        }
    }
