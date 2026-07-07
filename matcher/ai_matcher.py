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
from core.config import settings

CONFIDENCE_THRESHOLD: float = 0.75

CANDIDATE_DATE_WINDOW_DAYS: int = 10

MAX_CANDIDATES: int = 12

WINDOW_OVERLAP_DAYS: int = 7

class AI1to1Match(BaseModel):
    ledger_id:  str   = Field(..., description="Unique Ledger ID")
    bank_id:    int   = Field(..., description="Bank Statement Row Index")
    confidence: float = Field(..., ge=0.0, le=1.0, description="0.0 to 1.0")
    reasoning:  str   = Field(..., description="Concise semantic/date/amount match explanation")

class AIWindowOutput(BaseModel):
    matches: List[AI1to1Match]

class AILedgerCandidate(BaseModel):
    ledger_id: str

class AIManyToOneMatch(BaseModel):
    bank_id:    int                     = Field(..., description="Single matched bank row index")
    ledger_ids: List[AILedgerCandidate] = Field(..., description="Ledger entries summing to bank amount")
    confidence: float                   = Field(..., ge=0.0, le=1.0)
    reasoning:  str

class AIManyToOneOutput(BaseModel):
    matches: List[AIManyToOneMatch]

def _prepare_llm() -> ChatOllama:
    return ChatOllama(
        model=settings.OLLAMA_NAME,
        temperature=0.0,
        num_ctx=8192,
        repeat_penalty=1.1,
        base_url=settings.OLLAMA_URL,
    )

_SHARED_LLM: Optional[ChatOllama] = None

def get_shared_llm() -> ChatOllama:
    global _SHARED_LLM
    if _SHARED_LLM is None:
        _SHARED_LLM = _prepare_llm()
    return _SHARED_LLM


def reset_shared_llm() -> None:
    global _SHARED_LLM
    _SHARED_LLM = None


def _safe_parse_json(llm_output: Any, schema: type[BaseModel]) -> Optional[Any]:
    text = llm_output.content if hasattr(llm_output, "content") else str(llm_output)

    parser = JsonOutputParser(pydantic_object=schema)
    try:
        parsed = parser.invoke(text)
        if isinstance(parsed, dict):
            return schema.model_validate(parsed)
        return parsed
    except Exception:
        cleaned = re.sub(r"```(?:json)?\s*", "", text).rstrip(".")
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
        found   = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if found:
            cleaned = found.group(0)
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return schema.model_validate(data)
            return data
        except Exception:
            return None


def _make_parser(schema: type[BaseModel]):
    def _parse(llm_output):
        return _safe_parse_json(llm_output, schema)
    return _parse


def _format_record_for_prompt(rec: Dict[str, Any], is_bank: bool) -> str:
    dr_key = "debit"     if is_bank else "debit_amount"
    cr_key = "credit"    if is_bank else "credit_amount"
    id_key = "row_index" if is_bank else "ledger_id"
    dt_key = "date"      if is_bank else "transaction_date"
    nm_key = "narration" if is_bank else "account_name"

    return (
        f"- {rec.get(id_key, 'N/A')} | "
        f"{rec.get(dt_key, 'N/A')} | "
        f"Dr:{rec.get(dr_key, 0.0):.2f} Cr:{rec.get(cr_key, 0.0):.2f} | "
        f"NARR: {rec.get(nm_key, 'N/A')}"
    )


def _get_gl_direction_amount(gl: LedgerFormat) -> Tuple[float, str]:
    if gl.debit_amount and gl.debit_amount > 0:
        return gl.debit_amount, "debit"
    if gl.credit_amount and gl.credit_amount > 0:
        return gl.credit_amount, "credit"
    return 0.0, "unknown"


def _get_bank_direction_amount(bank: BankStatement) -> Tuple[float, str]:
    if bank.debit and bank.debit > 0:
        return bank.debit, "debit"
    if bank.credit and bank.credit > 0:
        return bank.credit, "credit"
    return 0.0, "unknown"


def _directions_compatible(gl_dir: str, bk_dir: str, same_side: bool = True) -> bool:
    if "unknown" in (gl_dir, bk_dir):
        return True  # can't determine; amount check is the arbiter
    return gl_dir == bk_dir if same_side else gl_dir != bk_dir


def _filter_candidates_for_bank(
    bank: BankStatement,
    ledger_pool: List[LedgerFormat],
    date_window: int,
    tol: float,
) -> List[LedgerFormat]:
    bk_amt, _ = _get_bank_direction_amount(bank)
    if not bank.date:
        return []

    try:
        bk_dt = datetime.strptime(bank.date[:10], "%Y-%m-%d").date()
    except ValueError:
        return []

    scored: List[Tuple[int, LedgerFormat]] = []
    for gl in ledger_pool:
        if not gl.transaction_date:
            continue
        try:
            gl_dt = datetime.strptime(gl.transaction_date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue

        day_diff = abs((bk_dt - gl_dt).days)
        if day_diff > date_window:
            continue

        gl_amt, _ = _get_gl_direction_amount(gl)
        if gl_amt <= bk_amt + tol:
            scored.append((day_diff, gl))

    scored.sort(key=lambda x: x[0])
    return [gl for _, gl in scored[:MAX_CANDIDATES]]

# 1-to-1 Math Bouncer 

def _passes_1to1_bouncer(
    gl_item: LedgerFormat,
    bk_item: BankStatement,
    tol: float,
    same_side: bool = True,
) -> Tuple[bool, str]:
    gl_amt, gl_dir = _get_gl_direction_amount(gl_item)
    bk_amt, bk_dir = _get_bank_direction_amount(bk_item)

    diff = abs(gl_amt - bk_amt)
    if diff > tol:
        return False, (
            f"Amount mismatch: GL={gl_amt:.2f} Bank={bk_amt:.2f} "
            f"diff={diff:.2f} exceeds tol={tol:.2f}"
        )

    if not _directions_compatible(gl_dir, bk_dir, same_side):
        return False, (
            f"Direction mismatch: GL is {gl_dir}, Bank is {bk_dir} "
            f"(same_side={same_side})"
        )

    return True, ""

# Many-to-one Math Bouncer 

def _passes_many_to_one_bouncer(
    gl_items: List[LedgerFormat],
    bk_item: BankStatement,
    tol: float,
    same_side: bool = True,
) -> Tuple[bool, str]:
    if not gl_items:
        return False, "No ledger items resolved from suggested IDs."

    bk_amt, bk_dir = _get_bank_direction_amount(bk_item)
    gl_total = 0.0

    for gl in gl_items:
        gl_amt, gl_dir = _get_gl_direction_amount(gl)
        if not _directions_compatible(gl_dir, bk_dir, same_side):
            return False, (
                f"Direction mismatch on {gl.ledger_id}: "
                f"GL is {gl_dir}, Bank is {bk_dir}"
            )
        gl_total += gl_amt

    diff = abs(gl_total - bk_amt)
    if diff > tol:
        return False, (
            f"Sum mismatch: ΣGL={gl_total:.2f} Bank={bk_amt:.2f} "
            f"diff={diff:.2f} exceeds tol={tol:.2f}"
        )

    return True, ""

# AI Time-Window Batch Matcher (1-to-1)

def ai_batch_matcher(
    unreconciled: Dict[str, Any],
    llm: ChatOllama,
    tol: float,
    same_side: bool = True,
) -> Tuple[Dict[str, Any], List[LedgerFormat], List[BankStatement]]:
    gl_remaining: List[LedgerFormat]  = list(unreconciled["UNRECONCILED_LEDGER"])
    bk_remaining: List[BankStatement] = list(unreconciled["UNRECONCILED_BANK"])
    ai_matches:   List[dict] = []
    audit_queue:  List[dict] = []

    _empty = {
        "AI_MATCHES":  ai_matches,
        "AUDIT_QUEUE": audit_queue,
        "MATCHED":     unreconciled["MATCHED"],
    }

    if not gl_remaining or not bk_remaining:
        return _empty, gl_remaining, bk_remaining

    dates = sorted(
        [r.date for r in bk_remaining if r.date] +
        [r.transaction_date for r in gl_remaining if r.transaction_date]
    )
    if not dates:
        return _empty, gl_remaining, bk_remaining

    window_size  = timedelta(days=30)
    window_step  = window_size - timedelta(days=WINDOW_OVERLAP_DAYS)
    current_start = datetime.strptime(dates[0][:10],  "%Y-%m-%d")
    final_end     = datetime.strptime(dates[-1][:10], "%Y-%m-%d")

    prompt_template = ChatPromptTemplate.from_messages([
        ("system",
         "You are a financial reconciliation expert.\n"
         "Find 1-to-1 matches between ledger records and bank statement rows "
         "within the provided time window.\n"
         "MATCH CRITERIA:\n"
         "  - Amounts match within ±{tol} (absolute difference).\n"
         "  - Dates fall inside the stated time window.\n"
         "  - Narration/account names share semantic meaning "
         "(abbreviations, synonyms, reversed word order are acceptable).\n"
         "CONFIDENCE: 0.0 = no evidence  |  1.0 = certain. "
         "Only propose matches you rate ≥ {confidence_threshold}.\n"
         "OUTPUT FORMAT: Strict JSON ONLY. No markdown. No prose outside schema.\n"
         'Schema: {{"matches": [{{"ledger_id": "string", "bank_id": number, '
         '"confidence": 0.0-1.0, "reasoning": "string"}}]}}'),
        ("human",
         "TOLERANCE: ±{tol}\n"
         "TIME WINDOW: {start_date} to {end_date}\n\n"
         "LEDGER ENTRIES:\n{ledger_list}\n\n"
         "BANK ENTRIES:\n{bank_list}"),
    ])

    while current_start <= final_end:
        window_end = current_start + window_size

        window_mid = current_start + window_size / 2

        gl_chunk = sorted(
            (
                r for r in gl_remaining
                if r.transaction_date
                and current_start.date()
                <= datetime.strptime(r.transaction_date[:10], "%Y-%m-%d").date()
                <= window_end.date()
            ),
            key=lambda r: abs(
                (datetime.strptime(r.transaction_date[:10], "%Y-%m-%d") - window_mid).days
            ),
        )
        bk_chunk = sorted(
            (
                r for r in bk_remaining
                if r.date
                and current_start.date()
                <= datetime.strptime(r.date[:10], "%Y-%m-%d").date()
                <= window_end.date()
            ),
            key=lambda r: abs(
                (datetime.strptime(r.date[:10], "%Y-%m-%d") - window_mid).days
            ),
        )
        gl_ctx = [_format_record_for_prompt(asdict(r), False) for r in gl_chunk[:20]]
        bk_ctx = [_format_record_for_prompt(asdict(r), True)  for r in bk_chunk[:20]]

        if not gl_ctx or not bk_ctx:
            current_start += window_step
            continue

        chain = prompt_template | llm
        raw   = chain.invoke({
            "tol":                f"{tol:.2f}",
            "confidence_threshold": str(CONFIDENCE_THRESHOLD),
            "start_date":         current_start.strftime("%Y-%m-%d"),
            "end_date":           window_end.strftime("%Y-%m-%d"),
            "ledger_list":        "\n".join(gl_ctx),
            "bank_list":          "\n".join(bk_ctx),
        })
        result = _safe_parse_json(raw, AIWindowOutput)

        if result and hasattr(result, "matches"):
            for m in result.matches:
                gl_item = next((r for r in gl_remaining if r.ledger_id == m.ledger_id), None)
                bk_item = next((r for r in bk_remaining if str(r.row_index) == str(m.bank_id)), None)

                if not gl_item or not bk_item:
                    print(f" GHOST REFERENCE: Ledger '{m.ledger_id}' or "
                          f"Bank '{m.bank_id}' not found in pools - skipped.")
                    continue

                # confidence gate
                if m.confidence < CONFIDENCE_THRESHOLD:
                    audit_queue.append({
                        **m.model_dump(),
                        "flag":   "LOW_CONFIDENCE",
                        "action": "Route to human audit - confidence below threshold.",
                    })
                    print(f" LOW CONFIDENCE ({m.confidence:.0%}): "
                          f"[Ledger {m.ledger_id}] ↔ [Bank {m.bank_id}] → audit queue.")
                    continue

                # mount + directionality bouncer
                passed, reason = _passes_1to1_bouncer(gl_item, bk_item, tol, same_side)
                if not passed:
                    print(f" REJECTED HALLUCINATION (1-to-1): "
                          f"[Ledger {m.ledger_id}] ↔ [Bank {m.bank_id}] - {reason}")
                    continue

                ai_matches.append(m.model_dump())
                gl_remaining = [r for r in gl_remaining if r.ledger_id        != m.ledger_id]
                bk_remaining = [r for r in bk_remaining if str(r.row_index)   != str(m.bank_id)]

        current_start += window_step

    return {
        "AI_MATCHES":  ai_matches,
        "AUDIT_QUEUE": audit_queue,
        "MATCHED":     unreconciled["MATCHED"],
    }, gl_remaining, bk_remaining

# AI One-to-Many Residual Matcher 

def ai_residual_matcher(
    unreconciled_ledger: List[LedgerFormat],
    unreconciled_bank:   List[BankStatement],
    llm: ChatOllama,
    tol: float,
    same_side: bool = True,
) -> Tuple[Dict[str, Any], List[LedgerFormat]]:
    ai_many_matches: List[dict] = []
    audit_queue:     List[dict] = []
    final_gl_left = list(unreconciled_ledger)

    if not unreconciled_bank:
        return {"AI_MANY_MATCHES": ai_many_matches, "AUDIT_QUEUE": audit_queue}, final_gl_left

    prompt_template = ChatPromptTemplate.from_messages([
        ("system",
         "You are a financial reconciliation expert.\n"
         "A single bank transaction may correspond to MULTIPLE ledger entries.\n"
         "TASK: Find which combination of the provided ledger entries sums to "
         "the bank amount within ±{tol} tolerance.\n"
         "Use narration semantics and date proximity as secondary signals.\n"
         "Only propose matches you rate ≥ {confidence_threshold} confidence.\n"
         "OUTPUT FORMAT: Strict JSON ONLY. No markdown.\n"
         'Schema: {{"matches": [{{"bank_id": number, '
         '"ledger_ids": [{{"ledger_id": "string"}}], '
         '"confidence": 0.0-1.0, "reasoning": "string"}}]}}'),
        ("human",
         "TOLERANCE: ±{tol}\n\n"
         "BANK ENTRY:\n{bank_info}\n\n"
         "CANDIDATE LEDGER ENTRIES (pre-filtered ±{date_window}d, amount ≤ bank+tol):\n"
         "{ledger_candidates}"),
    ])

    for bank in unreconciled_bank:
        # pre-filtered, relevance-ranked candidates only
        candidates = _filter_candidates_for_bank(
            bank, final_gl_left,
            date_window=CANDIDATE_DATE_WINDOW_DAYS,
            tol=tol,
        )
        if not candidates:
            continue

        ctx_ledger = "\n".join([_format_record_for_prompt(asdict(g), False) for g in candidates])
        ctx_bank   = _format_record_for_prompt(asdict(bank), True)

        chain = prompt_template | llm
        raw   = chain.invoke({
            "tol":                f"{tol:.2f}",
            "confidence_threshold": str(CONFIDENCE_THRESHOLD),
            "date_window":        str(CANDIDATE_DATE_WINDOW_DAYS),
            "bank_info":          ctx_bank,
            "ledger_candidates":  ctx_ledger,
        })
        result = _safe_parse_json(raw, AIManyToOneOutput)

        if not result or not hasattr(result, "matches"):
            continue

        for m in result.matches:
            matched_ids = [lid.ledger_id for lid in m.ledger_ids]
            gl_items    = [r for r in final_gl_left if r.ledger_id in matched_ids]

            # confidence gate
            if m.confidence < CONFIDENCE_THRESHOLD:
                audit_queue.append({
                    **m.model_dump(),
                    "flag":   "LOW_CONFIDENCE",
                    "action": "Route to human audit - confidence below threshold.",
                })
                print(f" LOW CONFIDENCE ({m.confidence:.0%}): "
                      f"Many-to-one [Bank {m.bank_id}] → audit queue.")
                continue

            # sum arithmetic + directionality bouncer
            passed, reason = _passes_many_to_one_bouncer(gl_items, bank, tol, same_side)
            if not passed:
                print(f" REJECTED HALLUCINATION (many-to-1): "
                      f"[Bank {m.bank_id}] ← {matched_ids} - {reason}")
                continue

            ai_many_matches.append(m.model_dump())
            final_gl_left = [r for r in final_gl_left if r.ledger_id not in matched_ids]

    return {
        "AI_MANY_MATCHES": ai_many_matches,
        "AUDIT_QUEUE":     audit_queue,
    }, final_gl_left



def ai_matcher_pipeline(
    result: dict,
    _AMOUNT_TOL: float,
    same_side: bool = True,
    llm: Optional[ChatOllama] = None,
) -> Dict[str, Any]:
    gl_input = result.get("UNRECONCILED_LEDGER", [])
    bk_input = result.get("UNRECONCILED_BANK",   [])

    using_shared = llm is None
    if using_shared:
        llm = get_shared_llm()

    try:
        llm.invoke("ping")
    except Exception as conn_err:
        if using_shared:
            reset_shared_llm()
        print(
            f"\n AI LAYER UNAVAILABLE: {conn_err}\n"
            "   Skipping Phase 3 (AI matching). All remaining records are "
            "returned as UNRECONCILED for manual review.\n"
        )
        return {
            "FINAL_RESULT": {
                "AI_MATCHES":             [],
                "AI_MANY_MATCHES":        [],
                "AUDIT_QUEUE":            [],
                "FINAL_RESIDUALS_LEDGER": gl_input,
                "FINAL_RESIDUALS_BANK":   bk_input,
                "MATCHED":                result.get("MATCHED", []),
                "ai_skipped":             True,
                "ai_skip_reason":         str(conn_err),
            }
        }

    print(" AI Time-Window Batch Matcher (1-to-1 Semantic)...")
    try:
        batch_res, gl_rem, bk_rem = ai_batch_matcher(result, llm, tol=_AMOUNT_TOL, same_side=same_side)
    except Exception as e:
        print(f" AI Batch Matcher crashed: {e}. Falling through to residual matcher.")
        batch_res = {"AI_MATCHES": [], "AUDIT_QUEUE": [], "MATCHED": result.get("MATCHED", [])}
        gl_rem, bk_rem = gl_input, bk_input

    if not bk_rem:
        print(" All remaining records reconciled via AI Batch.")
        return {
            "FINAL_RESULT": {
                **batch_res,
                "AI_MANY_MATCHES":        [],
                "FINAL_RESIDUALS_LEDGER": gl_rem,
                "FINAL_RESIDUALS_BANK":   [],
            }
        }

    print(" AI One-to-Many Residual Matcher...")
    try:
        residual_res, final_gl_left = ai_residual_matcher(gl_rem, bk_rem, llm, tol=_AMOUNT_TOL, same_side=same_side)
    except Exception as e:
        print(f" AI Residual Matcher crashed: {e}. Returning pools as unreconciled.")
        residual_res  = {"AI_MANY_MATCHES": [], "AUDIT_QUEUE": []}
        final_gl_left = gl_rem

    combined_audit = (
        batch_res.get("AUDIT_QUEUE", []) +
        residual_res.get("AUDIT_QUEUE", [])
    )

    return {
        "FINAL_RESULT": {
            "AI_MATCHES":             batch_res.get("AI_MATCHES", []),
            "AI_MANY_MATCHES":        residual_res.get("AI_MANY_MATCHES", []),
            "AUDIT_QUEUE":            combined_audit,
            "MATCHED":                batch_res.get("MATCHED", []),
            "FINAL_RESIDUALS_LEDGER": final_gl_left,
            "FINAL_RESIDUALS_BANK":   bk_rem,
        }
    }
