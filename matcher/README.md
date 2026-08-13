# Bank Reconciliation Matcher Pipeline

An automated ledger-to-bank-statement reconciliation engine. It takes a General
Ledger (GL) extract and a bank statement, and progressively matches them
through five escalating phases — from strict exact matching down to
AI-assisted semantic matching and human-review drafting — so that only
genuinely ambiguous items are left for a person to look at.

```
GL Records + Bank Records
           │
           ▼
 ┌─────────────────────┐
 │ 0. Same-Side Detect │  (sanity check on debit/credit orientation)
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐
 │ 1. Exact Match      │  date + amount + reference/UTR
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐
 │ 2. Fuzzy Match      │  17 deterministic heuristics
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐
 │ 3. Memory Match     │  previously recognized recurring patterns
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐
 │ 4. AI Match         │  LLM semantic 1-to-1 and many-to-1 matching
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐
 │ 5. Residual         │  classify, resolve wide-window timing/splits,
 │    Reconciliation   │  draft journal entries, build review queue
 └─────────┬───────────┘
           ▼
   Final Reconciliation Report
```

---

## Table of Contents

- [Module Map](#module-map)
- [Pipeline Workflow](#pipeline-workflow)
  - [Phase 0 — Same-Side Detection](#phase-0--same-side-detection)
  - [Phase 1 — Exact Match](#phase-1--exact-match)
  - [Phase 2 — Fuzzy Match](#phase-2--fuzzy-match)
  - [Phase 3 — Memory Match](#phase-3--memory-match)
  - [Phase 4 — AI Match](#phase-4--ai-match)
  - [Phase 5 — Residual Reconciliation](#phase-5--residual-reconciliation)
  - [Confidence Annotation & Quality Summary](#confidence-annotation--quality-summary)
- [Tolerances](#tolerances)
- [Usage](#usage)
- [Output Shape](#output-shape)
- [Configuration Reference](#configuration-reference)
- [Dependencies](#dependencies)
- [Extending the Pipeline](#extending-the-pipeline)

---

## Module Map

| File                     | Responsibility                                                              |
|--------------------------|------------------------------------------------------------------------------|
| `__init__.py`            | Orchestrates the full pipeline (`reconcile()`), tolerances, quality summary |
| `same_side_detect.py`    | Infers whether GL debit/credit maps to the same or opposite bank column     |
| `exact_match.py`         | Phase 1 — strict date/amount/reference matching                             |
| `fuzzy_match.py`         | Phase 2 — 17 deterministic heuristic-matching strategies                    |
| `memory.py`              | Phase 3 — cross-run pattern memory (recurring counterparties)               |
| `ai_matcher.py`          | Phase 4 — LLM-backed semantic matcher (1-to-1 and many-to-1)                |
| `residual_reconciler.py` | Phase 5 — classification, wide-window resolution, journal drafting          |
| `confidence.py`          | Normalizes confidence values and computes match-quality statistics          |

---

## Pipeline Workflow

The entry point is `reconcile(ledger_result, bank_result, all_warnings, ...)`
in `__init__.py`. Each phase consumes the **unmatched residue** of the
previous phase — records are never re-examined by an earlier phase, and a
record matched at any phase is removed from every pool downstream.

### Phase 0 — Same-Side Detection

`same_side_detect.detect_same_side()`

Before any matching happens, the pipeline samples up to 40 GL and 40 bank
records and cross-checks whether GL debits line up with bank debits
(`same_side=True`) or with bank credits (`same_side=False`). This handles
statement formats where the bank's "debit"/"credit" columns are mirrored
relative to the ledger's perspective.

- Needs at least **3 amount-matched pairs** in the sample to render an opinion.
- Requires the winning orientation to hold **≥ 65%** of the votes to be
  "confident."
- If confident and it **disagrees** with the caller-supplied `same_side`, a
  warning is appended to `all_warnings` (matching still proceeds using the
  caller's setting — this is advisory only).

### Phase 1 — Exact Match

`exact_match.exact_matcher()`

Strict matching on **same calendar date** + **matching amount** (± `EXACT`
tolerance, default `0.0`). Runs in two passes:

1. **Pass 1 (`require_ref_confirmation=True`)** — only accept a match if the
   GL `reference_id` equals the bank `txn_id`, or a UTR/UPI/NEFT/IMPS
   reference number extracted from either side's reference/narration matches.
2. **Pass 2 (`require_ref_confirmation=False`)** — for everything still
   unmatched, accept on date + amount alone (`confirmation_method:
   "amount_date_only"`).

Every match records `confirmation_method` (`reference_id`, `narration_utr`,
or `amount_date_only`) so downstream confidence scoring can distinguish a
verified match from a coincidental one.

**Output:** `EXACT_MATCHES`, plus `PENDING_FUZZY_LEDGER` / `PENDING_FUZZY_BANK`
(everything not matched) passed to Phase 2.

### Phase 2 — Fuzzy Match

`fuzzy_match.fuzzy_matcher()` → `FuzzyMatcher.run()`

Applies 17 deterministic strategies **in a fixed order**, each pulling
matched items out of the shared `ledger_pool` / `bank_pool` before the next
strategy runs:

| # | Strategy                          | What it catches                                                   |
|---|------------------------------------|---------------------------------------------------------------------|
| 1 | `cleanse_zero_amount_metadata`     | Zero-amount/metadata-only rows — set aside, not treated as unmatched |
| 2 | `flag_ghost_reversals`             | Reversal/duplicate/bounced narrations — flagged for audit           |
| 3 | `match_deposit_in_transit`         | Deposits recorded on GL, not yet cleared on the bank                |
| 4 | `match_outstanding_checks`         | Checks issued on GL, not yet cleared on the bank                    |
| 5 | `match_bank_service_charges`       | Bank fees/charges with no GL counterpart                            |
| 6 | `match_text_similarity`            | Narration ↔ account-name text/acronym similarity                    |
| 7 | `match_transposition_errors`       | Digit-transposition typos (same digits, different order)            |
| 8 | `match_rounding_differences`       | Small rounding deltas within tolerance                              |
| 9 | `match_nsf_returned_items`         | NSF / bounced / dishonored items                                     |
| 10| `match_interest_income`            | Interest/APY credits                                                 |
| 11| `match_discounts_and_taxes`        | Discount/tax-adjusted amounts                                        |
| 12| `match_bank_side_zero_sum`         | Bank-side entries that net to zero                                   |
| 13| `match_ledger_side_zero_sum`       | Ledger-side entries that net to zero                                 |
| 14| `match_aggregated_transactions`    | Batched/aggregated transaction groups                                |
| 15| `match_many_to_one_aggregation`    | Many GL lines summing to one bank row                                |
| 16| `match_split_charge`               | One GL line = exactly half of one bank charge (flags missing 2nd half)|
| 17| `match_base_fee_plus_tax`          | One GL line = sum of a bank fee row + a nearby tax/GST row            |

Key mechanics:
- All strategies respect the `TOLERANCES` dict passed in from `__init__.py`.
- Combination-based strategies (many-to-one, aggregation) cap combination size
  at `MAX_COMBINATION_SIZE = 6` and pre-filter the candidate pool to
  `MAX_COMBINATION_POOL_SIZE = 15` for performance.
- Date windows automatically widen by `_CROSS_MONTH_BUFFER = 15` days when a
  candidate pair straddles a month boundary (common with month-end batch
  postings).
- UTR/UPI/NEFT/IMPS/RTGS reference numbers are stripped from narrations before
  text-similarity scoring (`extract_utr`, `_INDIAN_PREFIX_RE`) so payment-rail
  noise doesn't distort the match.

**Output:** `FUZZY_MATCHES`, `IGNORED_METADATA`, `AUDIT_INVESTIGATION`
(items flagged for manual audit, e.g. detected reversals/split-charge
2nd-halves), and `UNRECONCILED_ITEMS` passed to Phase 3.

### Phase 3 — Memory Match

`__init__._apply_memory_matches()` using `memory.MatchMemory`

If a `MatchMemory` instance is supplied, the pipeline checks whether any
remaining GL/bank pair's **normalized counterparty signature**
(account name ↔ narration) was recognized as a match in a **previous run**.
If the signature is known *and* the amount/direction still lines up this run,
it's matched immediately — skipping the more expensive AI phase for
recurring, already-vetted relationships (e.g. a monthly rent debit or
recurring vendor payment).

- Signatures are normalized (lowercased, alphanumeric-only, whitespace
  collapsed) so minor formatting differences don't break recognition.
- Matches are labeled `"Recognized Recurring Pattern"`, optionally suffixed
  with the adjustment type first assigned by fuzzy/AI matching, and given a
  flat `"High"` confidence score.
- Backends: `InMemoryBackend` (session-only), `JSONFileBackend` (persists to
  disk), or `SQLAlchemyBackend` (persists to a DB table) — pluggable via the
  `MemoryBackend` protocol.
- At the end of `reconcile()`, **all** matches from every phase (exact, fuzzy,
  AI, memory) are recorded back into memory via
  `record_matches_from_records()`, so the next run benefits from everything
  learned this run. Call `memory.save()` afterward to persist.

**Output:** `MEMORY_MATCHES`; anything left goes to Phase 4.

### Phase 4 — AI Match

`ai_matcher.ai_matcher_pipeline()`

Uses a local LLM (Ollama, `phi3` model by default) for semantic matching that
heuristics can't express — e.g. narrations that mean the same thing but use
different vendor abbreviations, word order, or paraphrasing.

1. **Connectivity check** — pings the LLM first. If unreachable, the entire
   phase is skipped gracefully; all residual records are marked
   `ai_skipped: True` with a reason, and a warning is surfaced. No exception
   propagates.
2. **Batch matcher (1-to-1)** — `ai_batch_matcher()`
   - Slides a **30-day window** (7-day overlap) across the combined date
     range of remaining records.
   - Per window, sends up to 20 GL + 20 bank candidate lines (closest to the
     window midpoint) to the LLM with a strict JSON-only prompt.
   - Every proposed match passes two gates before acceptance:
     - **Confidence gate** — must be ≥ `CONFIDENCE_THRESHOLD = 0.75`, else
       routed to `AUDIT_QUEUE` as `LOW_CONFIDENCE`.
     - **Bouncer gate** (`_passes_1to1_bouncer`) — a deterministic re-check
       that amount and direction actually agree within tolerance; catches
       LLM hallucinations even after the confidence gate.
   - "Ghost references" (LLM inventing an ID not in the actual candidate
     pool) are logged and discarded.
3. **Residual matcher (many-to-1)** — `ai_residual_matcher()`, only runs if
   bank rows remain after the batch pass.
   - Pre-filters candidate ledger entries per bank row (±10-day window,
     amount ≤ bank amount + tolerance, capped at 12 candidates) before
     asking the LLM which subset sums to the bank amount.
   - Same confidence gate (≥ 0.75) and a many-to-one bouncer
     (`_passes_many_to_one_bouncer`) that verifies the proposed ledger
     entries actually sum to the bank amount within tolerance and share a
     compatible direction.
4. Both stages fail safe — a crash in either is caught and simply falls
   through to returning the pool as unreconciled rather than aborting the
   whole reconciliation.

**Output:** `AI_MATCHES` (1-to-1), folded together with `AI_MANY_MATCHES`
(many-to-1) into `AI_MATCHES`/`AI_AGENT` in the final report; `AI_AUDIT_QUEUE`
for low-confidence proposals; residual ledger/bank pools passed to Phase 5.

### Phase 5 — Residual Reconciliation

`residual_reconciler.reconcile_residuals()`

The final phase never tries to force a match — it **classifies** each
remaining item by the type of evidence available, then applies
wider-tolerance resolution only where that's actually justified, and hands
everything else to a human with context.

**5a. Classification** (`classify_residuals` / `_classify_one`) — each
residual item is labeled:

| Label                        | Meaning                                                                 |
|-------------------------------|--------------------------------------------------------------------------|
| `TIMING_CANDIDATE`            | An equal amount exists on the other side, just outside earlier windows  |
| `SPLIT_CANDIDATE`             | A subset of the other side's amounts sums to this item (cheap probe, ≤4 items) |
| `MISSING_ENTRY_CANDIDATE`     | No amount evidence elsewhere — likely genuinely unrecorded              |
| `UNCLASSIFIABLE`              | No signal found at all (possible data-quality issue)                     |

**5b. Timing resolution** (`_resolve_timing_candidates`) — for
`TIMING_CANDIDATE` items, searches a **45-day window**
(`TIMING_WINDOW_DAYS`) for the closest-dated equal-amount counterpart.
Flags result as `"Medium"` confidence if more than one candidate amount
tied, `"High"` if unambiguous.

**5c. Split resolution** (`_resolve_split_candidates`) — for
`SPLIT_CANDIDATE` items, performs a fuller subset-sum search
(up to `SPLIT_MAX_COMBINATION_SIZE = 8` items, `SPLIT_TOLERANCE = 2.0`)
against the pooled remaining ledger/bank items (not just same-labeled ones).

**5d. Journal-entry drafting** (`_generate_journal_drafts`) — for bank rows
classified `MISSING_ENTRY_CANDIDATE` (a bank movement with a draftable
narration and no ledger evidence at all), asks the LLM to suggest a
counter-account and produces a draft journal entry (`debit_account`,
`credit_account`, `entry_narrative`, `confidence`, `status:
"pending_review"`). Falls back to a keyword-based heuristic
(`_heuristic_draft_fallback`) if the LLM is unavailable — this path is
always populated, never blocked by AI availability.

**5e. Human review queue** (`_build_review_queue`) — everything not resolved
by 5b/5c/5d is packaged with its classification, its top-3 closest
candidates on the other side (amount difference, date gap, text similarity),
and a plain-English `suggested_action` describing what a reviewer should
check next.

**Output:** `timing_matches`, `split_matches`, `suggested_journal_entries`,
`human_review_queue`, `stats`, and the final
`still_unreconciled_ledger` / `still_unreconciled_bank` lists.

### Confidence Annotation & Quality Summary

`confidence.py` + `__init__._quality_summary()`

Every match from every phase is passed through
`annotate_match_confidence()`, which normalizes whatever confidence signal
that phase produced into a single `confidence_numeric` (0.0–1.0):

- Numeric `confidence` fields (AI matches) are clamped to `[0, 1]`.
- String `confidence_score` fields (fuzzy/memory matches: `"High"` /
  `"Medium"` / `"Low"`) are mapped to `0.90 / 0.60 / 0.30`.
- Matches with neither (exact matches) default to `1.0`.

`_quality_summary()` then aggregates all annotated matches into:
- `average_confidence` across the whole run,
- `low_confidence_match_count` / `_pct` (below `LOW_QUALITY_CONFIDENCE_BAR =
  0.5`),
- a breakdown `by_adjustment_type` (count + average confidence per
  adjustment/match label) — useful for spotting which heuristic is
  systematically producing weak matches.

---

## Tolerances

Defined centrally in `__init__.py` and threaded through every phase:

```python
TOLERANCES = {
    "EXACT":               0.0,   # Phase 1 — must match to the cent
    "ROUNDING_DIFFERENCE":  2.0,   # Phase 2 rounding-adjustment strategy
    "TIMING_DIFFERENCE":    2.0,   # Phase 3 memory-match amount check
    "AI_MATCHER":           5.0,   # Phase 4 LLM bouncer tolerance
    "TRANSPOSITION":        0.0,   # Phase 2 transposition strategy (digit-exact)
    "DEFAULT":              1.0,   # fallback used across fuzzy strategies
}
```

Phase 5 uses its own constants (`TIMING_WINDOW_DAYS = 45`,
`SPLIT_TOLERANCE = 2.0`, `EXISTENCE_AMOUNT_TOLERANCE = 1.0`) since it
operates on a wider, less-certain search space by design.

---

## Usage

```python
from matcher import reconcile, MatchMemory
from memory import JSONFileBackend  # or your own backend

warnings = []
memory = MatchMemory(backend=JSONFileBackend("memory/patterns.json"))

result = reconcile(
    ledger_result=parsed_ledger,   # {"records": [LedgerFormat, ...]}
    bank_result=parsed_bank,       # {"records": [BankStatement, ...], "bank_name": ..., "template_version": ...}
    all_warnings=warnings,
    same_side=True,                 # or False, if GL debit maps to bank credit
    auto_detect_same_side=True,     # warns (doesn't override) if this looks wrong
    memory=memory,                  # omit to disable cross-run pattern memory
    llm=None,                       # omit to use the shared default Ollama client
    enable_residual_reconciliation=True,
)

memory.save()  # persist newly-learned patterns for next run

print(result["summary"])
```

`ledger_result["records"]` and `bank_result["records"]` must be sequences of
`LedgerFormat` / `BankStatement` objects (see the `schema` package) exposing
at minimum: `ledger_id`, `transaction_date`, `debit_amount`, `credit_amount`,
`account_name`, `reference_id` (ledger side) and `row_index`, `date`,
`debit`, `credit`, `narration`, `txn_id` (bank side).

---

## Output Shape

`reconcile()` returns a single dict:

```python
{
  "bank_name": ...,
  "template_version": ...,
  "summary": {
      "ledger_records": int, "bank_records": int,
      "exact_matches": int, "fuzzy_matches": int, "memory_matches": int,
      "ai_matches": int, "ai_audit_queue": int,
      "unreconciled_ledger": int, "unreconciled_bank": int,
      "ai_skipped": bool,
      "match_quality": {...},          # see confidence.py summary
      "memory": {...} | None,
      "residual_reconciliation": {...} | None,
  },
  "EXACT_MATCHES": [...],
  "FUZZY_MATCHES": [...],
  "MEMORY_MATCHES": [...],
  "AI_MATCHES": [...],                  # 1-to-1 + many-to-1, annotated
  "AI_AGENT": [...],                    # alias of AI_MATCHES
  "AI_AUDIT_QUEUE": [...],              # low-confidence AI proposals
  "RESIDUAL_TIMING_MATCHES": [...],
  "RESIDUAL_SPLIT_MATCHES": [...],
  "SUGGESTED_JOURNAL_ENTRIES": [...],
  "HUMAN_REVIEW_QUEUE": [...],
  "UNRECONCILED_ITEMS": {"ledger": [...], "bank": [...]},
  "IGNORED_METADATA": [...],
  "AUDIT_INVESTIGATION": [...],
  "warnings": [...],
}
```

Every match dict, regardless of phase, ends up with a `confidence_numeric`
field and (where applicable) a `match_phase` tag (`"exact"`, `"fuzzy"`,
`"memory"`, `"ai"`, `"ai_audit_queue"`) so a caller can process the union of
all matches uniformly.

---

## Configuration Reference

| Constant                              | Location                | Default | Purpose                                            |
|----------------------------------------|--------------------------|---------|-----------------------------------------------------|
| `LOW_QUALITY_CONFIDENCE_BAR`           | `__init__.py`            | `0.5`   | Threshold for "low confidence" in the quality summary |
| `_CONFIDENCE_RATIO`                    | `same_side_detect.py`    | `0.65`  | Vote share needed to be "confident" on orientation   |
| `_MIN_MATCHED_PAIRS`                   | `same_side_detect.py`    | `3`     | Minimum sample votes before rendering an opinion     |
| `_SAMPLE_SIZE`                         | `same_side_detect.py`    | `40`    | Records sampled from each side for orientation check |
| `MAX_COMBINATION_SIZE`                 | `fuzzy_match.py`         | `6`     | Max items considered in a many-to-one combination    |
| `MAX_COMBINATION_POOL_SIZE`            | `fuzzy_match.py`         | `15`    | Candidate pool size before combination search         |
| `_CROSS_MONTH_BUFFER`                  | `fuzzy_match.py`         | `15`    | Extra days allowed when a match spans month-end       |
| `CONFIDENCE_THRESHOLD`                 | `ai_matcher.py`          | `0.75`  | Minimum LLM confidence to accept (else → audit queue) |
| `CANDIDATE_DATE_WINDOW_DAYS`           | `ai_matcher.py`          | `10`    | Many-to-1 candidate pre-filter window                 |
| `MAX_CANDIDATES`                       | `ai_matcher.py`          | `12`    | Max candidates sent to LLM per many-to-1 bank row     |
| `WINDOW_OVERLAP_DAYS`                  | `ai_matcher.py`          | `7`     | Overlap between successive 30-day batch windows       |
| `TIMING_WINDOW_DAYS`                   | `residual_reconciler.py` | `45`    | Wide-window timing resolution horizon                 |
| `SPLIT_MAX_COMBINATION_SIZE`           | `residual_reconciler.py` | `8`     | Max items in Phase-5 split-sum search                 |
| `NARRATION_SIMILARITY_THRESHOLD`       | `residual_reconciler.py` | `0.6`   | Minimum text similarity to count as a "narration signal" |
| `EXISTENCE_AMOUNT_TOLERANCE`           | `residual_reconciler.py` | `1.0`   | Amount tolerance used during residual classification  |

---

## Dependencies

- `langchain-ollama`, `langchain-core`, `pydantic` — LLM orchestration and
  structured-output parsing (Phases 4 & 5d).
- A running **Ollama** server (`http://127.0.0.1:11434` by default) serving
  the `phi3` model. The pipeline is fully functional without it — every AI
  step degrades gracefully to heuristics/manual review and surfaces a
  warning rather than failing.
- `schema` (internal) — `BankStatement`, `LedgerFormat`,
  `IgnoredMetadataRecord`, `AuditInvestigationItem` dataclasses/models.
- Optional: `SQLAlchemy` + a `database.bank_renc_model.MatchPatternModel` if
  using `SQLAlchemyBackend` for cross-run memory persistence.

---

## Extending the Pipeline

- **New fuzzy strategy**: add a `match_*` method to `FuzzyMatcher` and append
  it to the call list in `FuzzyMatcher.run()` — order matters, since each
  strategy only sees what earlier strategies left behind.
- **New memory backend**: implement the `MemoryBackend` protocol (`load()` /
  `save()`) and pass an instance to `MatchMemory(backend=...)`.
- **Swap the LLM**: pass any object exposing `.invoke()` compatible with
  LangChain's chat model interface as the `llm=` argument to `reconcile()` —
  it's threaded through to both `ai_matcher_pipeline()` and
  `reconcile_residuals()`.
- **Tune aggressiveness**: adjust `TOLERANCES` in `__init__.py` for
  phases 1–3, or the module-level constants in `ai_matcher.py` /
  `residual_reconciler.py` for phases 4–5.
