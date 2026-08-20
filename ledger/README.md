# Ledger Generation Pipeline

An Indian accounting engine that turns raw bill/invoice data into a full
double-entry general ledger — complete with GST computation, TDS
(Tax Deducted at Source) handling, GSTR-1 return data, trial balance,
cash book, creditors ageing, and year-end closing.

Give it a list of bill dictionaries (and optionally an opening-balances
file), and it hands back a posted ledger plus every downstream report an
Indian small-business books-of-accounts needs.

---

## Pipeline at a glance

```
bills (list[dict])                opening_balances.json
        │                                  │
        ▼                                  ▼
  journal.py                     opening_balances.py
  to_journal_entries()           load_opening_balances()
        │                                  │
        ▼                                  │
  raw JournalEntry[]                       │
        │                                  │
        ▼                                  │
     tds.py                                │
   TDSEngine.process_bill()                │
  (rewrites Dr/Cr lines to route           │
   TDS into "TDS Payable")                 │
        │                                  │
        ▼                                  │
  tds_modified JournalEntry[]  ◄───────────┘
        │
        ▼
        `ledger.py` also exposes a slimmer `build_ledger()` for cases where you
        just want a posted `GeneralLedger` without the TDS/GST/GSTR-1 reporting
        layer. It returns the ledger, the sorted audit trail, and an optional closing
        result.
    build_ledger()  →  GeneralLedger (posts every entry)
        │
        ├──► close_books()  (journal.py)   — optional year-end close
        │
        ▼
  ┌─────────────────────────────────────────────────────────┐
  │  Reports, all derived from the posted GeneralLedger:     │
  │   • trial_balance()        — ledger.py                   │
  │   • gst_summary()          — ledger.py                   │
  │   • extract_cash_book()    — ledger.py                   │
  │   • creditors_ageing()     — ledger.py                   │
  │   • build_gstr1()          — gstr1.py (works off bills)  │
  │   • TDS register/Form 26Q  — tds.py                      │
  └─────────────────────────────────────────────────────────┘
        │
        ▼
  __init__.py → build_complete_ledger()
  (single entry point that wires the whole pipeline together
   and returns one result dict)
```

`ledger.py` also exposes a slimmer `build_ledger()` for cases where you
just want a posted `GeneralLedger` without the TDS/GST/GSTR-1 reporting
layer. It returns the ledger, the sorted audit trail, and an optional closing
result.

---

## Module reference

### `journal.py`
Converts a raw **bill dict** into a balanced double-entry `JournalEntry`.

- `to_journal_entry(bill)` — dispatches on `bill["direction"]` /
  `bill["return_type"]` to one of four builders:
  - `_purchase_entry` — Dr. Purchase/Expense + Input GST, Cr. Creditor
  - `_sales_entry` — Dr. Debtor/Cash, Cr. Sales + Output GST
  - `_purchase_return_entry` — reverses a purchase (debit note)
  - `_sales_return_entry` — reverses a sale (credit note)
- `to_journal_entries(bills)` — maps the above over a list, skipping
  bills that fail validation.
- `gst_settlement_entry(...)` — books the monthly GST set-off/payment.
- `close_books(gl, close_on, period_label)` — computes Gross Profit and
  Net Profit/Loss from Trading & P&L account balances and returns a
  `ClosingResult` with the closing journal entries.
- `parse_indian_date(raw)` — parses `DD-MM-YYYY`-style strings.

### `tds.py`
The **TDS Engine** — Indian Income Tax Act, Chapter XVII-B. Sits between
`journal.py` and `ledger.py`: it intercepts payments that attract TDS and
rewrites the journal entry so the net amount goes to the vendor/creditor
and the deducted tax goes to "TDS Payable".

- `TDSEngine(financial_year, deductor_tan, deductor_name)`
- `.process_bill(bill, journal_entry, deductee_pan, deductee_type, deductee_gstin, tds_section)`
  → `TDSResult` (modified `JournalEntry` + `TDSEntry` + warnings)
- `.process_manual_entry(...)` — same, for manually entered vouchers.
- `.detect_section(description)` — keyword-based auto-detection of the
  applicable TDS section (194C, 194I, 194J, 194H, 194A, 192, etc.) from
  the expense description/narration.
- **Threshold tracking** — maintains running per-deductee/per-section
  aggregates for the financial year; TDS kicks in once the threshold is
  crossed, with a warning to review earlier undeducted payments.
- **Section 206AA** — automatically raises the rate to 20% (or higher)
  when no valid PAN is supplied.
- `.tds_deposit_entry(...)` / `.mark_deposited(...)` — books/tracks
  challan deposits (TDS Payable → Bank).
- `.get_register()` — full `TDSRegister` (deducted/deposited/pending).
- `.build_form_26q(quarter, start, end)` — quarterly Form 26Q data.
- `.pending_deposits(as_on)` — outstanding challans, flagged if overdue.

### `general_ledger_class.py`
The core in-memory ledger.

- `GeneralLedger` — keyed dict of `LedgerAccount` by account name.
- `.post_entries(entries)` — posts every `EntryLine` in each
  `JournalEntry` to its account, auto-creating accounts and generating
  "To .../By ..." particulars from the opposite side(s) of the entry.
- `.accounts`, `.get(name)`, `.accounts_in_group(group)` — accessors.

### `ledger.py`
Reporting functions that read off a posted `GeneralLedger`, plus the
mid-level `build_ledger()` orchestrator.

- `build_ledger(bills, opening_balances, _prebuilt_entries, close_books_on, period_label)`
  → `(GeneralLedger, all_entries, ClosingResult | None)`
- `trial_balance(gl, as_on)` — Dr/Cr closing balances per account,
  balance check.
- `gst_summary(gl, period_label)` — output vs. input CGST/SGST/IGST,
  ITC set-off waterfall (IGST → IGST/CGST/SGST → CGST → SGST), net
  payable and carry-forward ITC.
- `extract_cash_book(gl)` — merged Cash + Bank postings as a running
  balance cash book.
- `creditors_ageing(gl, as_on)` — buckets outstanding creditor balances
  into Current / 30–60 / 60–90 / 90+ days.

### `opening_balances.py`
Loads a JSON file of opening balances and one-off manual journal entries.

- `load_opening_balances(json_path)` → `(list[JournalEntry], period_start)`
- `resolve_account(name)` — maps a free-text account name to a `COA`
  (Chart of Accounts) entry, falling back to keyword-based group
  inference (e.g. "rent" → Indirect Expenses, "furniture" → Fixed
  Assets), then Indirect Expenses as a last resort.
- Auto-balances the opening entry against Capital if Dr ≠ Cr.

### `gstr1.py`
Builds **GSTR-1** (outward-supply GST return) data directly from bills
(independent of the ledger).

- `build_gstr1(bills, period_label)` → dict with:
  - `b2b` — invoice-level rows for registered buyers (GSTIN ≥ 15 chars)
  - `b2c_large` — grouped by (place of supply, rate) for inter-state
    unregistered invoices ≥ ₹2,50,000
  - `nil_rated` — Nil-rated / Exempt / Non-GST, split B2B vs B2C
  - `hsn_summary` — Table 12, grouped by HSN/SAC + UOM + rate, split
    across line items where present
  - `totals` — grand totals for reconciliation
  - `warnings` — e.g. missing invoice numbers, missing HSN codes

### `__init__.py`
The **package entry point**. `build_complete_ledger(...)` runs the full
pipeline end-to-end:

1. Load opening balances (`opening_balances.py`)
2. Build raw journal entries from bills (`journal.py`)
3. Run every non-output (purchase-side) entry through `TDSEngine`
   (`tds.py`), auto-detecting TDS section/deductee details from each
   bill
4. Post everything to a `GeneralLedger`, optionally closing the books
   on a given date (`ledger.py`)
5. Compute trial balance, GST summary, cash book, creditors ageing
6. Build the TDS register, Form 26Q per quarter, and pending-deposit list
7. Return one consolidated result `dict`

Console output at each stage is prefixed `[pipeline]` / `[tds]` for
progress tracing (set `LOG_TDS=0` to silence TDS line-item logging).

---

## Usage

```python
from datetime import date
from ledger import build_complete_ledger

bills = [
    {
        "_status": "ok",
        "direction": "input",              # "input" = purchase, "output" = sale
        "vendor_name": "Sharma Associates",
        "invoice_number": "INV-104",
        "invoice_date": "12-06-2025",
        "taxable_amount": 50000,
        "cgst_amount": 4500, "sgst_amount": 4500, "igst_amount": 0,
        "grand_total": 59000,
        "narration": "Professional fees for FY audit",
        "account_name": "Professional Fees",
        "tds_section": None,               # let the engine auto-detect
        "deductee_pan": "ABCPD1234E",
        "deductee_type": "individual",
    },
    # ...more bills
]
result = build_complete_ledger(
    bills                 = bills,
    opening_balances_json = "opening_balances.json",
    as_on_date            = date(2026, 3, 31),
    period_label          = "FY 2025-26",
    financial_year        = "2025-26",
    deductor_tan          = "DELX12345Y",
    deductor_name         = "Your Firm Pvt Ltd",
    close_books_on        = date(2026, 3, 31),   # omit to skip year-end close
)
tb  = result["trial_balance"]
gst = result["gst_summary"]
tds_register = result["tds_register"]
form_26q     = result["form_26q"]          # {"Q1": Form26Q, "Q2": ..., ...}
```

### Minimal path (no TDS/GST reporting, just a ledger)

```python
from ledger import build_ledger

gl, entries, closing_result = build_ledger(bills=bills)
```

`build_ledger()` accepts optional `opening_balances` as already-built
`JournalEntry` objects. The internal `_prebuilt_entries` argument is intended
for the full orchestrator after TDS processing; callers should normally pass
raw bills instead.

---

## Data flow summary

| Stage | Input | Output |
|---|---|---|
| Journalising | `bill: dict` | `JournalEntry` (balanced Dr/Cr lines) |
| TDS | `JournalEntry` + bill metadata | TDS-adjusted `JournalEntry` + `TDSEntry` |
| Posting | `JournalEntry[]` | `GeneralLedger` (per-account postings) |
| Closing (optional) | `GeneralLedger` + close date | `ClosingResult` (P&L closing entries) |
| Reporting | `GeneralLedger` / `bills` | `TrialBalance`, `GSTSummary`, `CashBookLine[]`, `AgeingLine[]`, GSTR-1 dict, `TDSRegister`, `Form26Q` |

## Requirements

- A `schema` module defining `Account`, `AccountGroup`, `COA`,
  `JournalEntry`, `EntryLine`, `DrCr`, `LedgerAccount`, `LedgerPosting`,
  `TrialBalance`, `GSTSummary`, `CashBookLine`, `AgeingLine`,
  `DeducteeType`, `TDSEntry`, `TDSRegister`, `TDSSection`,
  `TDS_SECTIONS`, `TDSStatus`, `TDSResult`, `Form26Q`, `Form26QLine`.
- Python 3.10+ (uses `X | Y` union types and `from __future__ import annotations`).