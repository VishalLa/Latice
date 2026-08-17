from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple

from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate

from schema import BankStatement, LedgerFormat, AIWindowOutput, AIManyToOneOutput
from core.config import  Config
from .ai_utils import (
    get_shared_llm,
    reset_shared_llm,
    _safe_parse_json,
    _passes_1to1_bouncer,
    _passes_many_to_one_bouncer,
    _filter_candidates_for_bank,
    _format_record_for_prompt,
)

CONFIDENCE_THRESHOLD: float = 0.75
CANDIDATE_DATE_WINDOW_DAYS: int = 10
WINDOW_OVERLAP_DAYS: int = 7
BATCH_WINDOW_DAYS: int = 30
MAX_RECORDS_PER_WINDOW: int = 20


class AIMatcher:
    """
    AI-assisted reconciliation: a 1-to-1 batch matcher over sliding time
    windows, followed by a many-to-1 residual matcher for whatever's left.

    Bundling `llm`, `tol`, and `same_side` on the instance is the main thing
    this buys over the free-function version — every method below stops
    needing to thread those three values through as parameters, and the
    "is this the shared LLM or one I was handed" bookkeeping lives in one
    place instead of being reasoned about at every call site.
    """

    def __init__(
        self,
        config: Config,
        llm: Optional[ChatOllama] = None,
        tol: float = 0.50,
        same_side: bool = True,
    ) -> None:
        self._owns_shared_llm = llm is None
        self.llm = llm or get_shared_llm(config)
        self.tol = tol
        self.same_side = same_side


    def _check_connectivity(self) -> Optional[str]:
        """Returns None if the LLM is reachable, else an error string."""
        try:
            self.llm.invoke("ping")
            return None
        except Exception as exc:
            if self._owns_shared_llm:
                reset_shared_llm()
            return str(exc)


    # Phase A — 1-to-1 batch matcher, sliding time windows
    _BATCH_PROMPT = ChatPromptTemplate.from_messages([
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

    def batch_match(
        self,
        unreconciled: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[LedgerFormat], List[BankStatement]]:

        gl_remaining: List[LedgerFormat] = list(unreconciled["UNRECONCILED_LEDGER"])
        bk_remaining: List[BankStatement] = list(unreconciled["UNRECONCILED_BANK"])
        ai_matches: List[dict] = []
        audit_queue: List[dict] = []

        empty = {
            "AI_MATCHES": ai_matches,
            "AUDIT_QUEUE": audit_queue,
            "MATCHED": unreconciled["MATCHED"],
        }

        if not gl_remaining or not bk_remaining:
            return empty, gl_remaining, bk_remaining

        dates = sorted(
            [r.date for r in bk_remaining if r.date] +
            [r.transaction_date for r in gl_remaining if r.transaction_date]
        )
        if not dates:
            return empty, gl_remaining, bk_remaining

        window_size = timedelta(days=BATCH_WINDOW_DAYS)
        window_step = window_size - timedelta(days=WINDOW_OVERLAP_DAYS)
        current_start = datetime.strptime(dates[0][:10], "%Y-%m-%d")
        final_end = datetime.strptime(dates[-1][:10], "%Y-%m-%d")

        chain = self._BATCH_PROMPT | self.llm

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

            gl_ctx = [_format_record_for_prompt(asdict(r), False) for r in gl_chunk[:MAX_RECORDS_PER_WINDOW]]
            bk_ctx = [_format_record_for_prompt(asdict(r), True) for r in bk_chunk[:MAX_RECORDS_PER_WINDOW]]

            if not gl_ctx or not bk_ctx:
                current_start += window_step
                continue

            raw = chain.invoke({
                "tol": f"{self.tol:.2f}",
                "confidence_threshold": str(CONFIDENCE_THRESHOLD),
                "start_date": current_start.strftime("%Y-%m-%d"),
                "end_date": window_end.strftime("%Y-%m-%d"),
                "ledger_list": "\n".join(gl_ctx),
                "bank_list": "\n".join(bk_ctx),
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

                    if m.confidence < CONFIDENCE_THRESHOLD:
                        audit_queue.append({
                            **m.model_dump(),
                            "flag": "LOW_CONFIDENCE",
                            "action": "Route to human audit - confidence below threshold.",
                        })
                        print(f" LOW CONFIDENCE ({m.confidence:.0%}): "
                              f"[Ledger {m.ledger_id}] ↔ [Bank {m.bank_id}] → audit queue.")
                        continue

                    passed, reason = _passes_1to1_bouncer(gl_item, bk_item, self.tol, self.same_side)
                    if not passed:
                        print(f" REJECTED HALLUCINATION (1-to-1): "
                              f"[Ledger {m.ledger_id}] ↔ [Bank {m.bank_id}] - {reason}")
                        continue

                    ai_matches.append(m.model_dump())
                    gl_remaining = [r for r in gl_remaining if r.ledger_id != m.ledger_id]
                    bk_remaining = [r for r in bk_remaining if str(r.row_index) != str(m.bank_id)]

            current_start += window_step

        return {
            "AI_MATCHES": ai_matches,
            "AUDIT_QUEUE": audit_queue,
            "MATCHED": unreconciled["MATCHED"],
        }, gl_remaining, bk_remaining


    # Phase B — many-to-1 residual matcher
    _RESIDUAL_PROMPT = ChatPromptTemplate.from_messages([
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

    def residual_match(
        self,
        gl_remaining: List[LedgerFormat],
        bk_remaining: List[BankStatement],
    ) -> Tuple[Dict[str, Any], List[LedgerFormat]]:

        ai_many_matches: List[dict] = []
        audit_queue: List[dict] = []
        gl_left = list(gl_remaining)

        if not bk_remaining:
            return {
                "AI_MANY_MATCHES": ai_many_matches, 
                "AUDIT_QUEUE": audit_queue
            }, gl_left

        chain = self._RESIDUAL_PROMPT | self.llm

        for bank in bk_remaining:
            candidates = _filter_candidates_for_bank(
                bank, gl_left,
                date_window=CANDIDATE_DATE_WINDOW_DAYS,
                tol=self.tol,
            )
            if not candidates:
                continue

            ctx_ledger = "\n".join(_format_record_for_prompt(asdict(g), False) for g in candidates)
            ctx_bank = _format_record_for_prompt(asdict(bank), True)

            raw = chain.invoke({
                "tol": f"{self.tol:.2f}",
                "confidence_threshold": str(CONFIDENCE_THRESHOLD),
                "date_window": str(CANDIDATE_DATE_WINDOW_DAYS),
                "bank_info": ctx_bank,
                "ledger_candidates": ctx_ledger,
            })
            result = _safe_parse_json(raw, AIManyToOneOutput)

            if not result or not hasattr(result, "matches"):
                continue

            for m in result.matches:
                matched_ids = [lid.ledger_id for lid in m.ledger_ids]
                gl_items = [r for r in gl_left if r.ledger_id in matched_ids]

                if m.confidence < CONFIDENCE_THRESHOLD:
                    audit_queue.append({
                        **m.model_dump(),
                        "flag": "LOW_CONFIDENCE",
                        "action": "Route to human audit - confidence below threshold.",
                    })
                    print(f" LOW CONFIDENCE ({m.confidence:.0%}): "
                          f"Many-to-one [Bank {m.bank_id}] → audit queue.")
                    continue

                passed, reason = _passes_many_to_one_bouncer(gl_items, bank, self.tol, self.same_side)
                if not passed:
                    print(f" REJECTED HALLUCINATION (many-to-1): "
                          f"[Bank {m.bank_id}] ← {matched_ids} - {reason}")
                    continue

                ai_many_matches.append(m.model_dump())
                gl_left = [r for r in gl_left if r.ledger_id not in matched_ids]

        return {"AI_MANY_MATCHES": ai_many_matches, "AUDIT_QUEUE": audit_queue}, gl_left


    # Orchestration
    def run(
        self, 
        result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Runs both phases against `result` (expects UNRECONCILED_LEDGER,
        UNRECONCILED_BANK, MATCHED keys) and returns a single FINAL_RESULT
        dict, mirroring the shape the rest of the pipeline already expects.
        """
        gl_input = result.get("UNRECONCILED_LEDGER", [])
        bk_input = result.get("UNRECONCILED_BANK", [])

        conn_err = self._check_connectivity()
        if conn_err is not None:
            print(
                f"\n AI LAYER UNAVAILABLE: {conn_err}\n"
                "   Skipping AI matching. All remaining records are "
                "returned as UNRECONCILED for manual review.\n"
            )
            return {
                "FINAL_RESULT": {
                    "AI_MATCHES": [],
                    "AI_MANY_MATCHES": [],
                    "AUDIT_QUEUE": [],
                    "FINAL_RESIDUALS_LEDGER": gl_input,
                    "FINAL_RESIDUALS_BANK": bk_input,
                    "MATCHED": result.get("MATCHED", []),
                    "ai_skipped": True,
                    "ai_skip_reason": conn_err,
                }
            }

        print(" AI Time-Window Batch Matcher (1-to-1 Semantic)...")
        try:
            batch_res, gl_rem, bk_rem = self.batch_match(result)
        except Exception as e:
            print(f" AI Batch Matcher crashed: {e}. Falling through to residual matcher.")
            batch_res = {"AI_MATCHES": [], "AUDIT_QUEUE": [], "MATCHED": result.get("MATCHED", [])}
            gl_rem, bk_rem = gl_input, bk_input

        if not bk_rem:
            print(" All remaining records reconciled via AI Batch.")
            return {
                "FINAL_RESULT": {
                    **batch_res,
                    "AI_MANY_MATCHES": [],
                    "FINAL_RESIDUALS_LEDGER": gl_rem,
                    "FINAL_RESIDUALS_BANK": [],
                }
            }

        print(" AI One-to-Many Residual Matcher...")
        try:
            residual_res, final_gl_left = self.residual_match(gl_rem, bk_rem)
        except Exception as e:
            print(f" AI Residual Matcher crashed: {e}. Returning pools as unreconciled.")
            residual_res = {"AI_MANY_MATCHES": [], "AUDIT_QUEUE": []}
            final_gl_left = gl_rem

        combined_audit = batch_res.get("AUDIT_QUEUE", []) + residual_res.get("AUDIT_QUEUE", [])

        return {
            "FINAL_RESULT": {
                "AI_MATCHES": batch_res.get("AI_MATCHES", []),
                "AI_MANY_MATCHES": residual_res.get("AI_MANY_MATCHES", []),
                "AUDIT_QUEUE": combined_audit,
                "MATCHED": batch_res.get("MATCHED", []),
                "FINAL_RESIDUALS_LEDGER": final_gl_left,
                "FINAL_RESIDUALS_BANK": bk_rem,
            }
        }


def ai_matcher(
    config: Config,
    result: Dict[str, Any],
    tol: float = 0.50,
    same_side: bool = True,
    llm: Optional[ChatOllama] = None,
) -> Dict[str, Any]:
    matcher = AIMatcher(llm=llm, tol=tol, same_side=same_side, config=config)
    return matcher.run(result)

