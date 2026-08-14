from __future__ import annotations

import re
import json
from typing import List, Dict, Any, Optional, Tuple, Union

from pydantic import BaseModel
from langchain_ollama import ChatOllama
from langchain_core.output_parsers import JsonOutputParser

from schema import LedgerFormat, BankStatement
from core.config import Config

MAX_CANDIDATES: int = 12

# Shared LLM instance
_SHARED_LLM: Optional[ChatOllama] = None

def _prepare_llm(config: Config) -> ChatOllama:
    return ChatOllama(
        model=config.OLLAMA_NAME,
        temperature=0.0,
        num_ctx=8129,
        repeat_penalty=1.1,
        base_url=config.OLLAMA_URL
    )

def get_shared_llm() -> ChatOllama:
    global _SHARED_LLM
    if _SHARED_LLM is None:
        _SHARED_LLM = _prepare_llm()
    return _SHARED_LLM

def reset_shared_llm() -> None:
    global _SHARED_LLM
    _SHARED_LLM = None


# JSON parsing (LLM output is not always clean JSON)
def _safe_parse_json(
    llm_output: Any,
    schema: type[BaseModel],
) -> Optional[Any]:
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


# Prompt formatting
def _format_record_for_prompt(
    rec: Dict[str, Any],
    is_bank: bool
) -> str:
    dr_key = "debit_amount"
    cr_key = "credit_amount"
    id_key = "row_id"    if is_bank else "ledger_id"
    dt_key = "date"      if is_bank else "transaction_date"
    nu_key = "narration" if is_bank else "account_name"

    return (
        f"- {rec.get(id_key, 'N/A')} | "
        f"{rec.get(dt_key, 'N/A')} | "
        f"Dr:{rec.get(dr_key, 0.0):.2f} Cr:{rec.get(cr_key, 0.0):.2f} | "
        f"NARR: {rec.get(nm_key, 'N/A')}"
    )
    

# Direction / amount helpers
def _get_direction_amount(
    template: Union[LedgerFormat | BankStatement]
) -> Tuple[float, str]:
    if template.debit_amount and template.debit_amount > 0:
        return template.debit_amount, "debit"
    if template.credit_amount and template.credit_amount > 0:
        return template.credit_amount, "credit"
    return 0.0, "unknown"


def _directions_compatible(
    gl_dir: str,
    bk_dir: str,
    same_side: bool = True
) -> bool:
    if "unknown" in (gl_dir, bk_dir):
        return True
    return gl_dir == bk_dir if same_side else gl_dir != bk_dir


# Candidate filtering (used by the many-to-one residual matcher)
def _filter_candidates_for_bank(
    bank: BankStatement,
    ledger_pool: List[LedgerFormat],
    date_window: int,
    tol: float
) -> List[LedgerFormat]:
    bk_amt, _ = _get_direction_amount(bank)
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

        gl_amt, _ = _get_direction_amount(gl)
        if gl_amt <= bk_amt + tol:
            scored.append((day_diff, gl))

    scored.sort(key=lambda x: x[0])
    return [gl for _, gl in scored[:MAX_CANDIDATES]]


# 1-to-1 Math Bouncer
def _passes_1to1_bouncer(
    gl_item: LedgerFormat,
    bk_item: BankStatement,
    tol: float,
    same_side: bool = True
) -> Tuple[bool, str]:
    gl_amt, gl_dir = _get_direction_amount(gl_item)
    bk_amt, bk_dir = _get_direction_amount(bk_item)

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
    same_side: bool = True
) -> Tuple[bool, str]:
    if not gl_items:
        return False, "No ledger items resolved from suggested IDs."

    bk_amt, bk_dir = _get_direction_amount(bk_item)
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
