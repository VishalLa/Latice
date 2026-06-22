# TODO: Shift to local LLM instead of using gemani

from __future__ import annotations

import json
import logging
from typing import List, Literal

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from schema import LedgerFormat, BankStatement

load_dotenv()

logger = logging.getLogger(__name__)


# Pydantic schemas for structured LLM output

class SemanticMatch(BaseModel):
    """A single semantic match proposed by the LLM."""

    ledger_id: str = Field(
        description="The 'ledger_id' field of the ledger item (e.g. 'L0001')."
    )
    bank_row_index: str = Field(
        description=(
            "The 'row_index' of the bank statement item being matched. "
            "Always return as a string even if the original value is numeric."
        )
    )
    confidence: Literal["High", "Medium", "Low"] = Field(
        description=(
            "Confidence in this match. "
            "'High' = near-identical description & exact amount. "
            "'Medium' = clear semantic link with a tiny amount difference (hidden fees ≤ 2%). "
            "'Low' = plausible but uncertain."
        )
    )
    reason: str = Field(
        description="One concise sentence explaining why these two items match."
    )


class SemanticMatchList(BaseModel):
    """Wrapper so the LLM always returns a list (even when empty)."""

    matches: List[SemanticMatch] = Field(
        default_factory=list,
        description="List of semantic matches found. Empty list if none.",
    )


def _direction(debit: float, credit: float) -> str:
    """
    Derive a human-readable direction from debit/credit amounts.

    Ledger convention  : debit_amount > 0  → Outflow  (money leaving the company)
                         credit_amount > 0 → Inflow   (money entering the company)
    Bank convention    : debit > 0         → Outflow  (bank deducted from account)
                         credit > 0        → Inflow   (bank credited the account)
    """
    if credit > 0.0 and debit == 0.0:
        return "Inflow"
    if debit > 0.0 and credit == 0.0:
        return "Outflow"
    # Both non-zero (split row) – report both
    return "Mixed"


def _amount(debit: float, credit: float) -> float:
    """Return the non-zero side as the canonical amount."""
    return credit if credit > 0.0 else debit


def _minimise_ledger(items: List[LedgerFormat]) -> list[dict]:
    """Map LedgerFormat objects to minimal dicts for the LLM prompt."""
    result = []
    for item in items:
        result.append(
            {
                "id":        item.ledger_id,
                "desc":      item.account_name or "",
                "date":      item.transaction_date or item.transaction_date_raw or "",
                "amount":    _amount(item.debit_amount, item.credit_amount),
                "direction": _direction(item.debit_amount, item.credit_amount),
            }
        )
    return result


def _minimise_bank(items: List[BankStatement]) -> list[dict]:
    """Map BankStatement objects to minimal dicts for the LLM prompt."""
    result = []
    for item in items:
        result.append(
            {
                "id":        str(item.row_index),
                "desc":      item.narration or "",
                "date":      item.date or item.date_raw or "",
                "amount":    _amount(item.debit, item.credit),
                "direction": _direction(item.debit, item.credit),
            }
        )
    return result


def _build_prompt(ledger_items: list[dict], bank_items: list[dict]) -> str:
    return f"""
You are an expert accountant performing bank reconciliation.

Your task is to find **semantic matches** between unreconciled ledger entries and
unreconciled bank-statement entries that could not be matched by exact or fuzzy
string methods.

### STRICT MATCHING RULES — follow these exactly:

1. **Direction must match**: Only pair an Inflow ledger item with an Inflow bank
   item, and an Outflow ledger item with an Outflow bank item. NEVER cross directions.

2. **Amount tolerance**: The amounts must be either exactly equal OR differ by no
   more than 2% of the larger value (to account for hidden bank fees / FX rounding).
   Do NOT match items with larger amount differences.

3. **Semantic link**: The descriptions must refer to the same real-world transaction.
   For example, "Software Sub" ↔ "STRIPE * AWS" is a valid semantic match because
   AWS charges are often processed via Stripe.

4. **No hallucination**: If you are not confident there is a genuine match, do NOT
   invent one. Return an empty matches list rather than a low-quality guess.

5. **One-to-one**: Each ledger id and each bank id may appear in at most one match.

---

### LEDGER ITEMS (unreconciled):
{json.dumps(ledger_items, indent=2)}

### BANK STATEMENT ITEMS (unreconciled):
{json.dumps(bank_items, indent=2)}

---

IMPORTANT – field names in your response:
  • Use the ledger item's "id" value as `ledger_id`   (e.g. "L0001")
  • Use the bank item's "id" value as `bank_row_index` (e.g. "3")

Return your answer ONLY as a structured list of matches using the schema provided.
""".strip()


def _ledger_lookup(items: List[LedgerFormat]) -> dict[str, LedgerFormat]:
    """ledger_id → LedgerFormat"""
    return {item.ledger_id: item for item in items}


def _bank_lookup(items: List[BankStatement]) -> dict[str, BankStatement]:
    """str(row_index) → BankStatement"""
    return {str(item.row_index): item for item in items}


def ai_agent_match(
    unreconciled_ledger: List[LedgerFormat],
    unreconciled_bank: List[BankStatement],
    *,
    model_name: str = "gemini-1.5-flash",
    temperature: float = 0.0,
) -> dict:
    """
    Parameters
    ----------
    unreconciled_ledger:
        List of LedgerFormat objects remaining after Phases 1 & 2.
    unreconciled_bank:
        List of BankStatement objects remaining after Phases 1 & 2.
    model_name:
        Gemini model to use (default: gemini-1.5-flash).
    temperature:
        LLM temperature (0 = deterministic / most conservative).

    Returns
    -------
    dict with keys:
        NEW_AI_MATCHES            – list of match dicts (ledger_id, bank_row_index,
                                    confidence, reason, ledger_item, bank_item)
        FINAL_UNRECONCILED_LEDGER – LedgerFormat items still unmatched after Phase 3
        FINAL_UNRECONCILED_BANK   – BankStatement items still unmatched after Phase 3
    """

    # Short-circuit: nothing to match
    if not unreconciled_ledger or not unreconciled_bank:
        logger.info("ai_agent_match: one or both pools are empty — skipping LLM call.")
        return {
            "NEW_AI_MATCHES":            [],
            "FINAL_UNRECONCILED_LEDGER": list(unreconciled_ledger),
            "FINAL_UNRECONCILED_BANK":   list(unreconciled_bank),
        }

    # 1. Minimise data — derive direction & canonical amount from debit/credit fields
    ledger_min = _minimise_ledger(unreconciled_ledger)
    bank_min   = _minimise_bank(unreconciled_bank)

    # 2. Build structured LLM
    llm = ChatGoogleGenerativeAI(model=model_name, temperature=temperature)
    structured_llm = llm.with_structured_output(SemanticMatchList)

    # 3. Invoke
    prompt = _build_prompt(ledger_min, bank_min)
    logger.debug("ai_agent_match prompt:\n%s", prompt)

    try:
        result: SemanticMatchList = structured_llm.invoke(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM call failed: %s", exc)
        return {
            "NEW_AI_MATCHES":            [],
            "FINAL_UNRECONCILED_LEDGER": list(unreconciled_ledger),
            "FINAL_UNRECONCILED_BANK":   list(unreconciled_bank),
        }

    raw_matches: List[SemanticMatch] = result.matches or []
    logger.info("ai_agent_match: LLM returned %d candidate match(es).", len(raw_matches))

    # 4. Validate & deduplicate (guard against hallucinated ids, enforce one-to-one)
    l_lookup = _ledger_lookup(unreconciled_ledger)
    b_lookup = _bank_lookup(unreconciled_bank)

    matched_ledger_ids: set[str] = set()
    matched_bank_ids:   set[str] = set()
    accepted_matches:   list[dict] = []

    for m in raw_matches:
        lid = m.ledger_id
        bid = str(m.bank_row_index)

        if lid not in l_lookup:
            logger.warning("LLM hallucinated ledger_id '%s' — skipping.", lid)
            continue
        if bid not in b_lookup:
            logger.warning("LLM hallucinated bank_row_index '%s' — skipping.", bid)
            continue
        if lid in matched_ledger_ids or bid in matched_bank_ids:
            logger.warning(
                "Duplicate match attempt (ledger='%s', bank='%s') — skipping.", lid, bid
            )
            continue

        matched_ledger_ids.add(lid)
        matched_bank_ids.add(bid)
        accepted_matches.append(
            {
                "ledger_id":     lid,
                "bank_row_index": bid,
                "confidence":    m.confidence,
                "reason":        m.reason,
                # Full objects for downstream reporting
                "ledger_item":   l_lookup[lid],
                "bank_item":     b_lookup[bid],
            }
        )

    # 5. Remove matched items from the leftover pools
    final_ledger = [i for i in unreconciled_ledger if i.ledger_id not in matched_ledger_ids]
    final_bank   = [i for i in unreconciled_bank   if str(i.row_index) not in matched_bank_ids]

    logger.info(
        "ai_agent_match: accepted %d match(es) | remaining ledger=%d, bank=%d",
        len(accepted_matches), len(final_ledger), len(final_bank),
    )

    return {
        "NEW_AI_MATCHES":            accepted_matches,
        "FINAL_UNRECONCILED_LEDGER": final_ledger,
        "FINAL_UNRECONCILED_BANK":   final_bank,
    }
