# General Ledger & Bank Statement Reconciliation Engine

This repository contains the enterprise-grade logic for reconciling standardized General Ledger (GL) cashbook records against physical Bank Statement records. 

The reconciliation pipeline utilizes a robust **Four-Stage Waterfall Architecture**:
1. **Phase 1: Exact Matching Engine** (Zero-tolerance deterministic pass)
2. **Phase 2: Fuzzy Heuristic Engine** (17-strategy priority waterfall)
3. **Phase 3: AI Agent Orchestrator** (Local GPU-accelerated LLM semantic matching)

---

## Architecture Overview

```text
 [Raw GL & Bank Exports]
           │
           ▼
┌────────────────────────────────────────────────────────┐
│             PHASE 1: EXACT MATCHING ENGINE             │
└────────────────────────────────────────────────────────┘
           │ (Reconciles ~60-80% instant hits)
           ▼
 [Leftover Residual Pools]
           │
           ▼
┌────────────────────────────────────────────────────────┐
│             PHASE 2: FUZZY HEURISTIC ENGINE            │
│  ├─ Phase 0: Pre-Match Cleansing & Reversal Flagging   │
│  ├─ Phase 1: Timing Lags (DIT, Checks) & Text Sim      │
│  ├─ Phase 2: Zero-Sum Contra Pairs & Aggregations      │
│  └─ Phase 3: Specialized Fee+Tax & Split Charge passes │
└────────────────────────────────────────────────────────┘
           │ (Resolves timing lags & structural quirks)
           ▼
 [Hard Unreconciled Leftovers]
           │
           ▼
┌────────────────────────────────────────────────────────┐
│              PHASE 3: AI AGENT ORCHESTRATOR            │
│  ├─ 30-Day Rolling Window Batch Matcher (Semantic)     │
│  ├─ Strict Python Arithmetic Bouncer (Math Verification)│
│  └─ One-to-Many Complex Residual Matcher               │
└────────────────────────────────────────────────────────┘
           │
           ▼
 { Final Consolidated Enterprise JSON Report }
```

---

## Phase 1: Exact Matching Engine

The exact matcher attempts to reconcile records with zero tolerance for discrepancies in amount or date.

### Core Rules
* **Amount Parity (Inverted):** GL debit amount must equal the bank credit, and GL credit amount must equal the bank debit. Single-column banks (amount + type) are resolved into separate debit/credit fields prior to comparison.
* **Date Alignment:** The GL `transaction_date` must exactly equal the bank's standardized date. No date tolerance is permitted.
* **Transaction ID Validation:** If a transaction ID is present on **both** sides, they must be identical. If absent on one or both sides, this check is safely skipped.
* **Strictness:** No partial-amount tolerance (beyond float rounding) and no narration matching are applied in this phase.

### Directional Conventions (`same_side`)
* **`same_side=True` (Cashbook / Bank-book format):** GL Debit matches Bank Debit (both represent money out). GL Credit matches Bank Credit (both represent money in).
* **`same_side=False` (Standard double-entry GL):** GL Debit matches Bank Credit (inflow in books = deposit in bank). GL Credit matches Bank Debit (outflow in books = withdrawal in bank).

---

## Phase 2: Fuzzy Matching Engine

The Fuzzy Matching Engine processes the leftover `PENDING_FUZZY_LEDGER` and `PENDING_FUZZY_BANK` arrays that Phase 1 could not reconcile. It applies **17 specialized strategies in strict waterfall priority**, consuming matched records immediately so subsequent passes never see them.

### Core Principles
* **Amount Tolerance:** `_AMOUNT_TOL = 0.05` (5 paise / half a US cent). Amounts within this window are treated as equal throughout all strategies to prevent floating-point noise from blocking matches.
* **Defensive Extraction:** Uses defensive helpers (`_get_amt()`, `_get_date()`) to extract monetary values and standardized ISO dates regardless of underlying Pydantic or Dataclass structures.

### Strategy Summary (Run-Order)

| # | Phase | Strategy Name | Type | Trigger / Logic | Confidence |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **0a** | `0` | Cleanse Metadata | B/L | Pops header/footer rows (amount ≤ 0.001) | N/A (Ignored) |
| **0b** | `0` | Flag Ghost Reversals | B | Regex match for returned/bounced checks | Action Required |
| **1** | `1` | Deposit in Transit | L→B | GL receipt, bank credits 1–5 days later | High / Med |
| **2** | `1` | Outstanding Checks | L→B | GL payment, bank debits 1–14 days later | High / Med |
| **3** | `1` | Bank Service Charges | L→B | Bank debits embedded fee (≤₹500 / 15%) | High / Med / Low |
| **4** | `1` | Text Similarity Match | L↔B | Exact amount, weak/absent reference ID | High / Med / Low |
| **5** | `1` | Book Error (Transpos.) | L↔B | Identical digit multiset, transposed order | High / Med |
| **6** | `1` | Rounding Differences | L↔B | ≤0.05 gap, strong semantic text match | High |
| **7** | `1` | NSF / Returned Items | B→L | Bank debit reversal matched to old receipt| High |
| **8** | `1` | Standalone Interest | B | Standalone bank credit matching interest | Medium |
| **9** | `1` | Discounts & Tax | L↔B | Bank gets exact % less (TDS/GST deduction)| Medium |
| **10** | `2` | Bank-Side Zero-Sum | B↔B | Equal & opposite contra pair in bank CSV | High / Med |
| **11** | `2` | Ledger-Side Zero-Sum | L↔L | Equal & opposite contra pair in ledger | High / Med |
| **12** | `2` | Aggregated Split (1:N)| L→B | 1 GL entry = sum of N bank withdrawals | High |
| **13** | `2` | Many-to-One Sum (N:1) | B→L | 1 Bank deposit = sum of N GL entries | High |
| **14** | `3` | Base Fee + Tax Sum | L↔B | 1 GL entry = sum of 2 adjacent bank rows | High |
| **15** | `3` | Split Charge Matcher | L↔B | Bank fee = exactly 2× GL entry (30d window)| High + Audit |

---

## Detailed Strategy Reference

### Strategy 0a: Zero-Amount Metadata Cleansing
* **Scenario:** Exported banking CSVs often contain metadata headers (`OPENING BALANCE`, `ENDING BALANCE`) with `$0.00` amounts.
* **Logic:** Silently extracts and drops any record where `abs(amount) <= 0.001` prior to distance calculations to prevent zero-sum division crashes. Moved to `IGNORED_METADATA`.

### Strategy 0b: Ghost Reversal Flagging
* **Scenario:** A customer check bounces. The bank logs a deposit reversal, but the accounting team never journaled the failure in the General Ledger.
* **Logic:** Scans bank narrations for `r"\b(reversal|return|bounced|reject|dup|duplicate)\b"`. Intercepts these items and pushes them directly to `AUDIT_INVESTIGATION` rather than attempting a forced mathematical match.

### 1. Deposit in Transit
* **Scenario:** Company records receiving cash on a given date, but the bank processes the deposit 1-5 business days later.
* **Rules:** Inbound amounts are exactly equal; bank date is 1 to 5 days after the ledger date.
* **Confidence:** High (≤ 3 days) or Medium (4-5 days). Beyond 5 days falls to UNRECONCILED.

### 2. Outstanding Checks
* **Scenario:** Company records issuing a payment, but the payee doesn't cash it for up to 14 days.
* **Rules:** Outbound amounts are exactly equal; bank date is 1 to 14 days after the ledger date.
* **Confidence:** High (≤ 7 days) or Medium (8-14 days). Beyond 14 days falls to UNRECONCILED.

### 3. Bank Service Charges
* **Scenario:** Bank deducts a slightly higher amount than recorded due to embedded transaction fees (e.g., wire fees, GST).
* **Rules:** Bank outbound is strictly greater than GL outbound by up to ₹500 or 15% (whichever is smaller). Dates within ±3 days. Narration text similarity ≥ 0.15.
* **Confidence:** High (≥ 0.50), Medium (≥ 0.25), Low (< 0.25). Low-confidence routed to AI_AGENT.

### 4. Text Similarity Match
* **Scenario:** Amounts match exactly, but the reference ID is missing. The ledger account name and bank narration describe the same entity.
* **Rules:** Amounts strictly equal. No conflicting reference IDs. Text similarity (max of SequenceMatcher, Jaccard, or Acronym score) ≥ 0.30.
* **Confidence:** High (≥ 0.60), Medium (≥ 0.30), Low (< 0.30). Low-confidence routed to AI_AGENT.

### 5. Book Error (Transposition)
* **Scenario:** Data entry error where digits are typed in the wrong order (e.g., ₹1,054 as ₹1,045).
* **Rules:** Multiset of decimal digits is identical. Amounts are not equal. Dates within ±3 days. Narration similarity ≥ 0.30.
* **Action:** Recommends a journal entry correction. High confidence if similarity ≥ 0.50 and date diff = 0.

### 6. Rounding Differences
* **Scenario:** Different systems round sub-rupee amounts differently, producing a gap of ≤ 0.05.
* **Rules:** Amount gap is between 0.00 and 0.05. Dates within ±3 days. Text similarity ≥ 0.50.
* **Action:** Always High confidence. Recommends writing off the difference.

### 7. NSF / Returned Items
* **Scenario:** A previously-recorded receipt is reversed on the bank statement (e.g., "BOUNCED", "NSF").
* **Rules:** Bank records a debit. Narration matches `_NSF_RE`. Original receipt amount equals the bank reversal. Bank date ≥ ledger date.
* **Action:** Always High confidence. Explicitly recommends a reversing journal entry.

### 8. Interest Income (Standalone)
* **Scenario:** Bank posts an interest credit not yet recorded in the books.
* **Rules:** Bank records a credit. Narration matches `_INTEREST_RE`. No ledger counterpart expected.
* **Action:** Always Medium confidence. Instructs adding the amount via journal entry (`ledger_id` set to None).

### 9. Discounts and Tax Withholdings
* **Scenario:** Bank receives/pays exactly 1%, 2%, 5%, or 10% less than the ledger amount (TDS, GST, early discount).
* **Rules:** Bank amount < GL amount. The reduction matches a specified percentage with 0.001 tolerance. Dates within ±5 days. Text similarity ≥ 0.40.
* **Action:** Always Medium confidence. Output identifies the implied discount/withholding rate.

### 10. Bank-Side Zero Sum
* **Scenario:** Two bank entries (debit/credit) cancel each other out (bank error, temporary advance).
* **Rules:** Opposite directions on the bank statement. Amounts are equal. Text similarity ≥ 0.60, or ≥ 0.30 if date gap ≤ 90 days.
* **Action:** High/Medium confidence based on similarity. Both entries consumed; `ledger_id` is None.

### 11. Ledger-Side Zero Sum
* **Scenario:** Two ledger entries cancel each other out (internal advance, failed payment reversed in books).
* **Rules:** Opposite directions in the ledger. Amounts are equal. Text similarity ≥ 0.60, or ≥ 0.30 if date gap ≤ 90 days.
* **Action:** High/Medium confidence based on similarity. Both entries consumed; `bank_id` is None.

### 12. Aggregated Split Transactions (1 Ledger : N Bank)
* **Scenario:** One ledger batch entry is split into multiple individual bank transactions (e.g., payment gateway splits).
* **Rules:** Single GL amount equals the sum of 2 to `MAX_COMBINATION_SIZE` bank amounts of the same direction within a 3-day window. Minimum text similarity of 0.15 required on at least one item.
* **Action:** Always High confidence. All N bank entries and the single ledger entry are consumed.

### 13. Many-to-One Aggregation (N Ledger : 1 Bank)
* **Scenario:** A batch payment from the bank was recorded as multiple separate ledger entries.
* **Rules:** Single bank amount equals the sum of 2 to `MAX_COMBINATION_SIZE` ledger amounts of the same direction within a 3-day window. Minimum text similarity of 0.15 required.
* **Action:** Always High confidence. All N ledger entries and the single bank entry are consumed.

### 14. Base Fee + Tax Aggregation (Parent-Child Rows)
* **Scenario:** A bank deducts a base fee ($1,180.00) and its associated tax ($212.40) as two separate, adjacent rows. The corporate bookkeeper journaled them as one lumped expense of $1,392.40.
* **Logic:** Iterates remaining bank rows. If two adjacent rows moving the same direction sum exactly to a single GL expense entry within a ±1 day window, they are paired and consumed simultaneously.

### 15. One-to-Many Split Charge Matcher
* **Scenario:** A company logs a recurring monthly software subscription ($295.00). The bank pulls two months of delayed fees at once as a single $590.00 withdrawal.
* **Logic:** Identifies pairings where `Bank Amount == exactly 2 × GL Amount` across an expanded 30-day window. Pairs the GL record against the bank row, and routes the orphaned unrecorded half ($295.00) straight to the `AUDIT_INVESTIGATION` queue for book adjustment.

---

## Phase 3: AI Agent Orchestrator (`ai_matcher_pipeline`)

Leftovers that survive the 17 fuzzy heuristics represent complex narratives, heavy abbreviations, or lumped multi-invoice dispersals. These are passed to an automated AI Agent powered by **LangChain** and local **Ollama GPU Inference (`phi3` / `qwen`)**.

```text
[Hard Unreconciled Items] ──► (LangChain Prompt) ──► [Ollama GPU Worker] ──► (Strict Math Bouncer) ──► [Verified Matches]
```

### 1. Rolling Time-Window Semantic Matcher (1-to-1)
* **Context Protection:** To prevent LLM context degradation, leftover pools are sliced into rolling 30-day chronological windows and injected in tight 20-item prompt batches.
* **Escaped Syntax:** All literal JSON syntax inside the LangChain system prompt is strictly double-braced `{{"matches": [...]}}` to prevent prompt variable injection collisions.
* **Semantic Capabilities:** The model evaluates narrative proximity regardless of string position, resolving complex vendor abbreviations (e.g., matching `"TGT*OFFICE SUPPLIES"` to `"Target Store #4912"`).

### 2. The Bouncer: Strict Python Math Verification
Large Language Models excel at semantic reading comprehension but fail at deterministic arithmetic. To guarantee absolute financial integrity, the AI Agent's structured JSON response is intercepted by a native Python verification block:

```python
# THE BOUNCER: Rejects LLM arithmetic hallucinations instantly
diff = abs(gl_amt - bk_amt)
if diff <= 0.05:
    ai_matches.append(m) # Verified hit
else:
    print(f"⚠️ REJECTED AI HALLUCINATION: [Ledger {lid}] and [Bank {bid}] differed by ${diff:.2f}.")
```

### 3. AI One-to-Many Residual Matcher
Orphaned bank rows are evaluated against rolling subsets of candidate ledger records within a ±5 day window, allowing the AI agent to identify which specific combination of open invoices accounts for a lump-sum client wire transfer.

---

## Enterprise Output Contract

The pipeline returns a fully hydrated **Superset JSON Dictionary** guaranteeing backwards compatibility with standard reporting interfaces while providing rich investigative arrays:

```json
{
    "bank_name": "HDFC Bank Ltd",
    "template_version": "v2.4-ISO",
    "summary": {
        "ledger_records": 29,
        "bank_records": 37,
        "exact_matches": 22,
        "fuzzy_matches": 4,
        "ai_matches": 1,
        "unreconciled_ledger": 2,
        "unreconciled_bank": 4
    },
    "EXACT_MATCHES": [
        {
            "ledger_id": "V001",
            "bank_id": 1,
            "amount": 12500.00,
            "date": "2026-05-02"
        }
    ],
    "FUZZY_MATCHES": [
        {
            "ledger_id": "V028",
            "bank_id": 4,
            "adjustment_type": "Split Charge Match (1st Half)",
            "confidence_score": "High",
            "details": "Bank Row [4] (2026-05-03, ₹590.00) is exactly 2× Ledger [V028] (2026-05-30, ₹295.00)."
        }
    ],
    "AI_MATCHES": [
        {
            "ledger_id": "L0089",
            "bank_id": 31,
            "confidence": 0.98,
            "reasoning": "Semantic match: 'Amzn Web Svcs' shares direct corporate identity with 'AWS Cloud Hosting'."
        }
    ],
    "AI_AGENT": [], 
    "UNRECONCILED_ITEMS": {
        "ledger": ["// Remaining open book items (e.g. V025 Salary Payment)"],
        "bank": ["// Remaining unbooked bank deductions (e.g. Wire fees)"]
    },
    "IGNORED_METADATA": [
        {
            "source": "bank",
            "row_ref": "0",
            "narration": "OPENING BALANCE",
            "reason": "Zero-amount metadata row."
        }
    ],
    "warnings": []
}
```