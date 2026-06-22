# General Ledger & Bank Statement Reconciliation Engine

This repository contains the logic for reconciling standardized General Ledger (GL) records against standardized Bank Statement records. The matching process is divided into two phases: **Exact Matching** and the **Fuzzy Matching Engine**.

---

## Phase 1: Exact Matching

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

The Fuzzy Matching Engine processes the leftover `PENDING_FUZZY_LEDGER` and `PENDING_FUZZY_BANK` arrays that Phase 1 could not reconcile. It applies **13 heuristics in strict priority order**, consuming matched records as it goes so later passes never see them.

### Core Principles
* **Amount Tolerance:** `_AMOUNT_TOL = 0.05` (5 paise / half a US cent). Amounts within this window are treated as equal throughout all strategies to prevent floating-point noise from blocking matches.
* **Helper Lambdas:** The engine uses helper lambdas (`gl_out`, `gl_in`, `bank_out`, `bank_in`) which automatically swap based on the `same_side` parameter defined in Phase 1.

### Strategy Summary (Run-Order)

| # | Strategy Name | Type | Trigger | Confidence |
| :--- | :--- | :--- | :--- | :--- |
| **1** | Deposit in Transit | L→B | GL receipt, bank credits later | High/Med |
| **2** | Outstanding Checks | L→B | GL payment, bank debits later | High/Med |
| **3** | Bank Service Charges | L→B | Bank debits slightly more | High/Med/Low |
| **4** | Text Similarity Match | L↔B | Same amount, weak/absent ref | High/Med/Low |
| **5** | Book Error (Transposition) | L↔B | Same digits, wrong order | High/Med |
| **6** | Rounding Differences | L↔B | ≤ 0.05 gap, strong text match | High |
| **7** | NSF / Returned Items | B→L | Bank reversal, find original | High |
| **8** | Interest Income | B | Standalone interest credit | Medium |
| **9** | Discounts & Tax Withholdings | L↔B | Bank gets exact % less | Medium |
| **10** | Bank-Side Zero Sum | B↔B | Contra pair on bank statement | High/Med |
| **11** | Ledger-Side Zero Sum | L↔L | Contra pair in the ledger | High/Med |
| **12** | Aggregated Split (1:N) | L→B | One GL = sum of N bank rows | High |
| **13** | Many-to-One Aggregation (N:1) | B→L | One bank = sum of N GL rows | High |

---

## Detailed Strategy Reference

### 1. Deposit in Transit
**Scenario:** Company records receiving cash on a given date, but the bank processes the deposit 1-5 business days later.
* **Rules:** Inbound amounts are exactly equal; bank date is 1 to 5 days after the ledger date.
* **Confidence:** High (≤ 3 days) or Medium (4-5 days). Beyond 5 days falls to UNRECONCILED.

### 2. Outstanding Checks
**Scenario:** Company records issuing a payment, but the payee doesn't cash it for up to 14 days.
* **Rules:** Outbound amounts are exactly equal; bank date is 1 to 14 days after the ledger date.
* **Confidence:** High (≤ 7 days) or Medium (8-14 days). Beyond 14 days falls to UNRECONCILED.

### 3. Bank Service Charges
**Scenario:** Bank deducts a slightly higher amount than recorded due to embedded transaction fees (e.g., wire fees, GST).
* **Rules:** Bank outbound is strictly greater than GL outbound by up to ₹500 or 15% (whichever is smaller). Dates within ±3 days. Narration text similarity ≥ 0.15.
* **Confidence:** High (≥ 0.50), Medium (≥ 0.25), Low (< 0.25). Low-confidence routed to AI_AGENT.

### 4. Text Similarity Match
**Scenario:** Amounts match exactly, but the reference ID is missing. The ledger account name and bank narration describe the same entity.
* **Rules:** Amounts strictly equal. No conflicting reference IDs. Text similarity (max of SequenceMatcher, Jaccard, or Acronym score) ≥ 0.30.
* **Confidence:** High (≥ 0.60), Medium (≥ 0.30), Low (< 0.30). Low-confidence routed to AI_AGENT.

### 5. Book Error (Transposition)
**Scenario:** Data entry error where digits are typed in the wrong order (e.g., ₹1,054 as ₹1,045).
* **Rules:** Multiset of decimal digits is identical. Amounts are not equal. Dates within ±3 days. Narration similarity ≥ 0.30.
* **Action:** Recommends a journal entry correction. High confidence if similarity ≥ 0.50 and date diff = 0.

### 6. Rounding Differences
**Scenario:** Different systems round sub-rupee amounts differently, producing a gap of ≤ 0.05.
* **Rules:** Amount gap is between 0.00 and 0.05. Dates within ±3 days. Text similarity ≥ 0.50.
* **Action:** Always High confidence. Recommends writing off the difference.

### 7. NSF / Returned Items
**Scenario:** A previously-recorded receipt is reversed on the bank statement (e.g., "BOUNCED", "NSF").
* **Rules:** Bank records a debit. Narration matches `_NSF_RE`. Original receipt amount equals the bank reversal. Bank date ≥ ledger date.
* **Action:** Always High confidence. Explicitly recommends a reversing journal entry.

### 8. Interest Income (Standalone)
**Scenario:** Bank posts an interest credit not yet recorded in the books.
* **Rules:** Bank records a credit. Narration matches `_INTEREST_RE`. No ledger counterpart expected.
* **Action:** Always Medium confidence. Instructs adding the amount via journal entry (`ledger_id` set to None).

### 9. Discounts and Tax Withholdings
**Scenario:** Bank receives/pays exactly 1%, 2%, 5%, or 10% less than the ledger amount (TDS, GST, early discount).
* **Rules:** Bank amount < GL amount. The reduction matches a specified percentage with 0.001 tolerance. Dates within ±5 days. Text similarity ≥ 0.40.
* **Action:** Always Medium confidence. Output identifies the implied discount/withholding rate.

### 10. Bank-Side Zero Sum
**Scenario:** Two bank entries (debit/credit) cancel each other out (bank error, temporary advance).
* **Rules:** Opposite directions on the bank statement. Amounts are equal. Text similarity ≥ 0.60, or ≥ 0.30 if date gap ≤ 90 days.
* **Action:** High/Medium confidence based on similarity. Both entries consumed; `ledger_id` is None.

### 11. Ledger-Side Zero Sum
**Scenario:** Two ledger entries cancel each other out (internal advance, failed payment reversed in books).
* **Rules:** Opposite directions in the ledger. Amounts are equal. Text similarity ≥ 0.60, or ≥ 0.30 if date gap ≤ 90 days.
* **Action:** High/Medium confidence based on similarity. Both entries consumed; `bank_id` is None.

### 12. Aggregated Split Transactions (1 Ledger : N Bank)
**Scenario:** One ledger batch entry is split into multiple individual bank transactions (e.g., payment gateway splits).
* **Rules:** Single GL amount equals the sum of 2 to `MAX_COMBINATION_SIZE` bank amounts of the same direction within a 3-day window. Minimum text similarity of 0.15 required on at least one item.
* **Action:** Always High confidence. All N bank entries and the single ledger entry are consumed.

### 13. Many-to-One Aggregation (N Ledger : 1 Bank)
**Scenario:** A batch payment from the bank was recorded as multiple separate ledger entries.
* **Rules:** Single bank amount equals the sum of 2 to `MAX_COMBINATION_SIZE` ledger amounts of the same direction within a 3-day window. Minimum text similarity of 0.15 required.
* **Action:** Always High confidence. All N ledger entries and the single bank entry are consumed.

---

## Output Format

The engine returns a JSON structure categorizing all matched and unmatched records:

```json
{
    "FUZZY_MATCHES": [
        {
            "ledger_id": "str | None", 
            "bank_id": "int | str | None",
            "adjustment_type": "str",
            "confidence_score": "High | Medium | Low",
            "details": "str"
        }
    ],
    "AI_AGENT": [
        "// Low-confidence matches requiring LLM or human review"
    ],
    "UNRECONCILED_ITEMS": {
        "ledger": ["// Array of LedgerFormat items"],
        "bank": ["// Array of BankStatement items"]
    }
}