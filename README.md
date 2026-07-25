# FinSync — Bank Reconciliation & Indian Tax Compliance Platform

FinSync is a full-stack, multi-tenant accounting back-office system built around one core workflow — **bank reconciliation** — and extended into a complete Indian statutory-compliance suite: general ledger, journal posting, TDS (Tax Deducted at Source) computation, and GSTR-1 filing exports. It also includes an OCR-driven bill/invoice ingestion pipeline that turns scanned images or PDFs into structured, tax-aware journal entries automatically.

The system is split into two independently deployable parts:

| Layer | Stack | Location |
|---|---|---|
| **Backend** | Python 3.11, Flask, SQLAlchemy, Celery, Redis, PostgreSQL | `Backend/` |
| **Frontend** | Vue 3, Vite, Pinia, Tailwind CSS | `Frontend/` |

---

## Table of Contents

1. [What it does](#1-what-it-does)
2. [High-level architecture](#2-high-level-architecture)
3. [Tech stack](#3-tech-stack)
4. [Project structure](#4-project-structure)
5. [Core subsystems, in depth](#5-core-subsystems-in-depth)
   - 5.1 [Bank reconciliation engine](#51-bank-reconciliation-engine)
   - 5.2 [Bill/Invoice OCR pipeline](#52-billinvoice-ocr-pipeline)
   - 5.3 [Journal & General Ledger](#53-journal--general-ledger)
   - 5.4 [TDS Engine](#54-tds-engine)
   - 5.5 [GSTR-1 Engine](#55-gstr-1-engine)
   - 5.6 [Reporting (Excel exports)](#56-reporting-excel-exports)
6. [Data model](#6-data-model)
7. [Authentication & multi-tenancy](#7-authentication--multi-tenancy)
8. [API reference](#8-api-reference)
9. [Background jobs (Celery)](#9-background-jobs-celery)
10. [Environment variables](#10-environment-variables)
11. [Getting started](#11-getting-started)
    - 11.1 [Docker Compose (recommended)](#111-docker-compose-recommended)
    - 11.2 [Manual / local setup](#112-manual--local-setup)
    - 11.3 [Frontend setup](#113-frontend-setup)
    - 11.4 [Local LLM setup (Ollama + Phi-3.5)](#114-local-llm-setup-ollama--phi-35)
12. [Frontend application](#12-frontend-application)
13. [Deployment notes](#13-deployment-notes)
14. [Security checklist before going to production](#14-security-checklist-before-going-to-production)
---

## 1. What it does

A user (typically an accountant or business owner) can:

- **Upload a bank statement + a general ledger export** (CSV/XLSX) and get an automated reconciliation: which transactions match, which are timing differences, which are unexplained, and a downloadable Excel report.
- **Upload a photo or PDF of a bill/invoice** and have it OCR'd, classified (GST invoice vs. retail bill, inter-state vs. intra-state), converted into a double-entry journal entry, and — where applicable — have TDS automatically deducted and posted.
- **Browse the general ledger** and pull a trial balance as of any date.
- **View the TDS register**, broken down by section and deductee, and export data in the NSDL Form 26Q layout.
- **Generate a GSTR-1 return** for a filing period from the recorded sales invoices.
- **Close an accounting period**, locking balances and carrying them forward as opening balances for the next period.

Everything is scoped per logged-in user, with an admin role that can see across all users' data (e.g. for an accounting firm managing multiple clients).

---

## 2. High-level architecture

```
                         ┌──────────────────────────┐
                         │        Frontend          │
                         │   Vue 3 SPA (Vite)       │
                         │   Pinia stores + Axios   │
                         └────────────┬─────────────┘
                                      │ REST / JWT Bearer
                                      ▼
                         ┌──────────────────────────┐
                         │      Flask API           │
                         │  (Blueprints per domain) │
                         │  auth · bank_rec · bills │
                         │  journal · ledger · tds  │
                         │  gstr1                   │
                         └──────┬───────────┬───────┘
                                │           │
                     synchronous│           │async dispatch
                                ▼           ▼
                    ┌────────────────┐  ┌───────────────────────┐
                    │  PostgreSQL    │  │   Celery workers      │
                    │  (SQLAlchemy)  │  │   (broker: Redis)     │
                    └────────────────┘  │                       │
                                        │  • bank_rec_task      │
                                        │  • bill_pipeline_task │
                                        └─────────┬─────────────┘
                                                  │
                     ┌────────────────────────────┼───────────────────────┐
                     ▼                            ▼                       ▼
           ┌───────────────────┐        ┌─────────────────────┐   ┌───────────────────┐
           │  Matcher pipeline │        │  PaddleOCR + regex  │   │  TDS / GSTR-1     │
           │  exact → fuzzy →  │        │  invoice extractor  │   │  engines          │
           │  memory → AI →    │        │                     │   │                   │
           │  residual solver  │        │                     │   │                   │
           └────────┬──────────┘        └──────────┬──────────┘   └──────────┬────────┘
                    │                              │                         │
                    ▼                              ▼                         ▼
           ┌──────────────────────────────────────────────────────────────────────┐
           │        Ollama (local LLM server, model: phi3) — used only as a       │
           │        fallback matcher for ambiguous bank-vs-ledger pairs           │
           └──────────────────────────────────────────────────────────────────────┘
```

Long-running work (reconciliation runs, OCR + bill posting) is **never done in the request/response cycle** — the API enqueues a Celery task and returns immediately with a run/bill ID; the frontend polls a status endpoint.

---

## 3. Tech stack

### Backend
- **Framework:** Flask 3.1 (Blueprint-per-domain), Flask-JWT-Extended for auth, Flask-CORS, Flask-Caching
- **ORM / DB:** SQLAlchemy 2.0 (declarative, typed `Mapped[...]` columns), PostgreSQL via `psycopg2`, SQLAlchemy-Utils for DB bootstrapping
- **Async jobs:** Celery 5.6 with Redis as broker + result backend
- **Validation/config:** Pydantic v2 + `pydantic-settings`
- **Auth:** `passlib` (bcrypt_sha256) for password hashing, `itsdangerous` for signed tokens, JWT for session auth
- **OCR:** PaddleOCR (CPU mode) + OpenCV headless
- **Local LLM inference:** `langchain-ollama` + `ollama` Python client, talking to a local Ollama server running **Phi-3.5-mini-instruct**
- **Fuzzy string matching:** `rapidfuzz`
- **Excel I/O:** `openpyxl`, `pandas`

### Frontend
- **Framework:** Vue 3 (Composition API), Vite build tool
- **State:** Pinia stores (`auth`, `ledgerSuite`, `reconciliation`, `toast`)
- **HTTP:** Axios with request/response interceptors (auto-attaches JWT, global error toasts, auto-logout on 401)
- **Styling:** Tailwind CSS 3, custom design tokens (dark theme, "beam" gradient backdrops)
- **Icons:** `@lucide/vue`
- **Routing:** `vue-router` with a global navigation guard for auth

---

## 4. Project structure

```
Reconcile/
├── Backend/
│   ├── main.py                     # dev entrypoint (Flask app.run)
│   ├── app/
│   │   ├── __init__.py             # create_app() factory, blueprint registration
│   │   ├── celery.py               # Celery app instance
│   │   └── cache.py                # Flask-Caching instance
│   ├── core/
│   │   └── config.py               # Pydantic Settings, DB URL assembly, DB bootstrap
│   ├── database/
│   │   ├── base.py                 # SQLAlchemy declarative Base + UUID helper
│   │   ├── session.py              # session factory / get_session() context manager
│   │   ├── user.py                 # User model + password hashing
│   │   ├── security.py             # bcrypt hashing, signed-token helpers
│   │   ├── bank_renc_model.py      # reconciliation-related ORM models
│   │   ├── ledger_tax_models.py    # Bill / Journal / TDS / GSTR1 ORM models
│   │   └── period_model.py         # Fiscal period + carried-forward balances
│   ├── schema/                     # plain dataclasses (non-ORM) — the "domain" layer
│   │   ├── bank_renc_schema.py
│   │   ├── journal_schema.py
│   │   ├── ledger_schema.py
│   │   ├── tds_schema.py
│   │   └── template.py             # known bank-statement column "fingerprints"
│   ├── entry_point/                # ingestion layer
│   │   ├── loader.py                # CSV/XLSX → schema objects (bank templates, ledger)
│   │   ├── bank_processor.py        # bank-statement template auto-detection
│   │   ├── ocr.py                   # PaddleOCR wrapper
│   │   └── data_extractor.py        # regex/heuristic invoice field extraction
│   ├── matcher/                    # the reconciliation engine
│   │   ├── __init__.py              # orchestrates the full matching pipeline
│   │   ├── exact_match.py
│   │   ├── fuzzy_match.py
│   │   ├── same_side_detect.py
│   │   ├── memory.py                 # learns recurring counterparty patterns
│   │   ├── ai_matcher.py              # Ollama/phi3-backed fallback matcher
│   │   ├── residual_reconciler.py     # many-to-one / split-transaction solver
│   │   └── confidence.py
│   ├── ledger/                     # accounting logic
│   │   ├── journal.py                # bill → double-entry journal entries
│   │   ├── ledger.py, general_ledger_class.py
│   │   ├── opening_balances.py
│   │   ├── tds.py                    # TDSEngine (see §5.4)
│   │   ├── gstr1.py                  # GSTR-1 builder (see §5.5)
│   │   └── run_pipeline.py           # build_ledger() orchestration helper
│   ├── service/                    # cross-cutting application services
│   │   ├── store_data.py / store_ledger_data.py   # persistence helpers
│   │   ├── reconciliation_journal_posting.py       # approve & post matched entries
│   │   ├── bank_rec_report.py                      # assembles reconciliation results
│   │   └── period_close.py                         # fiscal period close-out
│   ├── reports/                    # Excel report generators (openpyxl)
│   │   ├── bank_recon_xlsx.py, journal_xlsx.py
│   │   ├── ledger_xlsx.py, tds_xlsx.py, gstr1_xlsx.py
│   ├── tasks/                      # Celery task definitions
│   │   ├── bank_rec_task.py
│   │   └── bill_pipeline_task.py
│   ├── api/                        # Flask blueprints (HTTP layer only)
│   │   ├── auth.py, bank_rec_api.py, bills_api.py
│   │   ├── journal_api.py, ledger_api.py, tds_api.py, gstr1_api.py
│   │   └── _scoping.py               # shared multi-tenant scoping helpers
│   ├── scripts/
│   │   └── promote_admin.py          # CLI: promote a user to ADMIN
│   ├── storage/                      # generated report files land here
│   ├── docker-compose.yml, Dockerfile, start.sh, start.bat
│   └── req.txt / pyproject.toml
│
└── Frontend/
    ├── src/
    │   ├── main.js, router.js
    │   ├── api/axios.js                # configured Axios instance
    │   ├── stores/                     # Pinia: auth, ledgerSuite, reconciliation, toast
    │   ├── components/                 # NavBar, DataGrid, FileUploadDropzone,
    │   │                                # JournalEntryReview, MatchBreakdown, etc.
    │   └── views/                      # one view per major feature (see §12)
    ├── tailwind.config.js, vite.config.js, postcss.config.js
    └── package.json
```

---

## 5. Core subsystems, in depth

### 5.1 Bank reconciliation engine

This is the flagship feature, implemented as a **five-stage cascading pipeline** (`matcher/__init__.py`), where each stage only operates on what the previous stage couldn't resolve:

1. **Exact match** (`exact_match.py`) — same date (or within tolerance), same amount, correct debit/credit side. Cheapest and most reliable stage; disposes of the majority of well-behaved transactions.
2. **Fuzzy match** (`fuzzy_match.py`, ~1,100 lines) — handles rounding differences, timing differences (payment recorded a few days apart in bank vs. ledger), and transposition errors, using `rapidfuzz` string similarity on narrations plus configurable amount tolerances per adjustment type:
   | Adjustment type | Amount tolerance |
   |---|---|
   | `EXACT` | ₹0.00 |
   | `ROUNDING_DIFFERENCE` | ₹2.00 |
   | `TIMING_DIFFERENCE` | ₹2.00 |
   | `TRANSPOSITION` | ₹0.00 (digit-swap detection) |
   | `AI_MATCHER` | ₹5.00 |
   | `DEFAULT` | ₹1.00 |
3. **Memory-based match** (`memory.py`) — a per-tenant learned dictionary of "this ledger account name + this bank narration pattern have matched before," so recurring counterparties (rent, salary runs, recurring vendor payments) are auto-matched with high confidence on sight.
4. **AI matcher** (`ai_matcher.py`) — for whatever remains, candidate ledger/bank rows within a configurable date window (`CANDIDATE_DATE_WINDOW_DAYS = 10`, sliding with `WINDOW_OVERLAP_DAYS = 7`, capped at `MAX_CANDIDATES = 12` per window) are sent to a **local** LLM (Ollama running Phi-3.5, `temperature=0.0` for determinism) with a structured-output prompt. The model returns strict Pydantic-validated JSON (`AI1to1Match` / `AIManyToOneMatch`), and anything below `CONFIDENCE_THRESHOLD = 0.75` is discarded rather than trusted. Running this locally (rather than calling a cloud LLM API) means bank statement and ledger data never leaves the deployment — an intentional privacy decision given the sensitivity of the data.
5. **Residual reconciler** (`residual_reconciler.py`) — for transactions still unmatched after all of the above, attempts many-to-one / split-payment reconciliation (e.g. three ledger invoices summing to one bulk bank credit) and produces a final "audit investigation" list of genuinely unexplained items for human review.

Every match is annotated with a normalized confidence score (`confidence.py`) — numeric matches are clamped to `[0,1]`, string labels (`"high"/"medium"/"low"`) are mapped to `0.9/0.6/0.3` — and the pipeline produces an overall quality summary (average confidence, % of low-confidence matches, breakdown by adjustment type) so a human reviewer knows how much to trust a given run before approving journal postings from it.

**Bank statement format detection** (`entry_point/bank_processor.py`) doesn't assume a fixed CSV layout — it scans the first ~25 rows of the uploaded file looking for a header row matching a library of known bank "fingerprints" (`schema/template.py`), falling back to a fuzzy header-overlap score (≥0.6) if no exact fingerprint matches.

### 5.2 Bill/Invoice OCR pipeline

`entry_point/ocr.py` wraps PaddleOCR (CPU-only, angle-classification enabled) and converts raw OCR output into a sorted list of `Block(x, y, text, confidence)` objects, filtered at a 0.25 confidence threshold.

`entry_point/data_extractor.py` then applies a battery of regex/heuristic extractors on top of those blocks:
- GSTIN extraction via a strict 15-character regex
- Date extraction/normalization (via `dateutil`, day-first) across multiple date formats
- Row-grouping by Y-coordinate (tolerance ±8px) to reconstruct table structure from OCR'd text fragments
- "Value right of keyword" extraction for line-item labels (invoice number, GST rate, totals)
- A vendor-noise blocklist (`SKIP_VENDOR`) to avoid misreading boilerplate invoice text ("tax invoice", "e-invoice", "original for recipient", etc.) as the vendor name
- Invoice classification into `GST Invoice (Intra-state)`, `GST Invoice (Inter-state)`, `GST Invoice`, or `Retail Bill` based on which of CGST/SGST/IGST/GSTIN were detected

The pipeline is deliberately conservative: rather than guessing, extractors return `None` when they can't find a confident match, and the bill is flagged with a `_status` of `"ok"` or `"failed"` (based on whether a positive grand total was extracted) so failed extractions surface for manual correction instead of silently posting bad data.

### 5.3 Journal & General Ledger

`ledger/journal.py` converts a bill dict (or manual entry) into a proper double-entry `JournalEntry` (`schema/journal_schema.py`), against a chart of accounts (`COA`) with a defined `AccountGroup` taxonomy and `DrCr` enum. `ledger/ledger.py` / `general_ledger_class.py` implement `GeneralLedger`, which posts entries and can produce a `TrialBalance` as-of any date. `ledger/opening_balances.py` allows a general ledger to be seeded with carried-forward balances from a prior period (see Period Close, §5.4-adjacent, in `service/period_close.py`).

### 5.4 TDS Engine

`ledger/tds.py` (`TDSEngine`) implements Indian **Income Tax Act, 1961, Chapter XVII-B** TDS logic:

- **Section auto-detection** (`detect_section()`) scans expense account name / narration / hints against a keyword table, e.g.:
  | Keyword | Section |
  |---|---|
  | rent / office rent / building rent | 194I(b) |
  | plant / machinery / equipment rent | 194I(a) |
  | professional / advocate / CA / CS / doctor | 194J(b) |
  | technical / IT service / software service | 194J(a) |
  | contractor / labour / manpower | 194C |
  | commission / brokerage / agent fee | 194H |
  | interest (non-bank) | 194A |
  | salary / wages | 192 |

  The caller can always override with an explicit `tds_section` to bypass auto-detection — the keyword table is intentionally conservative and only covers clear-cut cases.
- **Journal adjustment:** when TDS applies, the engine rewrites the journal entry so the net payment goes to Cash/Bank and the deducted amount is routed to a **TDS Payable** account, e.g.:
  ```
  Purchase A/c        Dr.   [taxable]
  Input CGST A/c       Dr.   [cgst]
  Input SGST A/c       Dr.   [sgst]
    To Vendor A/c            Cr.   [net_payment]   ← gross − TDS
    To TDS Payable A/c       Cr.   [tds_amount]
  ```
- **Aggregate threshold tracking:** a running per-deductee, per-section total is maintained across the financial year; once a payment causes the total to cross the statutory threshold, TDS is applied from that payment forward and a warning is raised so earlier (under-deducted) payments can be reviewed.
- **Section 206AA (no-PAN penalty):** if the deductee's PAN is missing or `"PANNOTAVBL"`, the deduction rate is automatically raised to `max(section_rate, 20%)` and flagged `rate_enhanced_206aa = True`.
- **Deposit tracking:** `mark_deposited()` records the challan BSR code, serial number, and deposit date once TDS is actually paid to the government, moving the register entry to a "deposited" status.
- **Form 26Q export:** `build_form_26q(quarter, start, end)` assembles the quarterly TDS return in the NSDL line format (note: `section_code` is rendered in NSDL's own notation, e.g. `194Jb` rather than the internal `194J_b`).

### 5.5 GSTR-1 Engine

`ledger/gstr1.py` builds a GSTR-1 return (`GSTR1Record` / period rebuild logic) from posted sales invoices for a given filing period, split by the standard GSTR-1 sections (e.g. B2B, B2C, HSN summary — see the model for the exact fields captured). `PeriodGSTPosition` (in `database/period_model.py`) tracks each period's GST position so it can be carried forward at period close.

### 5.6 Reporting (Excel exports)

Every major module has a matching Excel report generator under `reports/`, built on `openpyxl`:
- `bank_recon_xlsx.py` — full reconciliation report (matched/unmatched, by adjustment type, confidence)
- `journal_xlsx.py` — journal register export
- `ledger_xlsx.py` — trial balance / ledger export
- `tds_xlsx.py` — TDS register / Form 26Q export
- `gstr1_xlsx.py` — GSTR-1 filing export

Generated files are written to `STORAGE_DIR` (see §10) and served back via authenticated download endpoints.

---

## 6. Data model

Key tables (see `database/*.py` for full column definitions):

| Model | Table | Purpose |
|---|---|---|
| `User` | `user` | Login identity, bcrypt password hash, `role` (`admin`/`user`) |
| `LedgerFormatModel` | `ledger_format` | Uploaded ledger rows, normalized |
| `BankStatementModel` | `bank_statement` | Uploaded bank statement rows, normalized |
| `ReconciliationRunModel` | `reconciliation_run` | One row per reconciliation job (tracks Celery task id, owner, status) |
| `MatchResultModel` | `match_result` | Individual matched pairs from a run, with adjustment type & confidence |
| `IgnoredMetadataRecordModel` | `ignored_metadata_record` | Rows explicitly excluded from matching (bank charges, etc.) |
| `MatchPatternModel` | `match_pattern` | Learned recurring-pattern memory (see §5.1, stage 3) |
| `AuditInvestigationItemModel` | `audit_investigation_item` | Final unexplained items needing human review |
| `BillModel` | `bill` | Uploaded bill/invoice, OCR status, extracted fields |
| `JournalEntryModel` / `JournalLineModel` | `journal_entry` / `journal_line` | Double-entry postings |
| `TDSEntryModel` / `TDSAggregateModel` | `tds_entry` / `tds_aggregate` | TDS register + running per-deductee/section aggregates |
| `GSTR1RecordModel` | `gstr1_record` | Per-invoice GSTR-1 line data |
| `FiscalPeriod` / `PeriodAccountBalance` / `PeriodGSTPosition` / `PeriodTDSPosition` | — | Period-close snapshots carried forward |

All domain-owning tables carry a `user_id` foreign key for tenant isolation (see §7).

---

## 7. Authentication & multi-tenancy

- **Auth:** JWT bearer tokens (`Flask-JWT-Extended`), issued on `/auth/login`, required on virtually every API route via `@jwt_required()`.
- **Passwords:** hashed with `bcrypt_sha256` via `passlib` — never stored or logged in plaintext.
- **Roles:** two roles — `USER` (default) and `ADMIN`. Promotion is a deliberate out-of-band action: `python scripts/promote_admin.py <username>` — there is **no API endpoint** that can self-promote a user to admin.
- **Scoping (`api/_scoping.py`):** every data-fetching route calls `scope_owner_id(user, requested_user_id)` — a non-admin is always hard-locked to `user.id` regardless of what they pass in query params; an admin may optionally pass `?user_id=...` to view a specific tenant's data, or omit it to see everything. This is the single choke point for tenant isolation — worth reviewing carefully if you extend the API.

---

## 8. API reference

Base URL prefix per blueprint (registered in `app/__init__.py`):

### `/auth`
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | — | Create a user (`username`, `password`, optional `email`) |
| POST | `/auth/login` | — | Returns `{ access_token, user_id }` |
| GET | `/auth/me` | JWT | Current user profile |

### `/api` (bank reconciliation) — `bank_rec_api.py`
| Method | Path | Description |
|---|---|---|
| POST | `/api/run_reconciliation` | Upload `ledger_file` + `bank_file` (csv/xlsx), enqueues a Celery run |
| GET | `/api/run_status/<run_id>` | Poll Celery task state / run status |
| GET | `/api/run_result/<run_id>` | Full match results once complete |
| GET | `/api/download_report/run/<run_id>` | Download the generated `.xlsx` for a run |
| GET | `/api/download_report/<filename>` | Download a report by filename |
| POST | `/api/generate_report/run/<run_id>` | (Re)generate the Excel report for a completed run |
| POST | `/api/run/<run_id>/approve_journal_entries` | Approve matched entries → post to the general ledger |

### `/api/bills` — `bills_api.py`
| Method | Path | Description |
|---|---|---|
| POST | `/api/bills/upload` | Upload a bill image/PDF **or** raw JSON (`raw_data`), enqueues OCR + journal pipeline |
| GET | `/api/bills/<bill_id>/status` | Poll OCR/posting status for a bill |
| GET | `/api/bills` | List bills (filterable by `status`, `direction`; admin can pass `user_id`) |

### `/api/journal` — `journal_api.py`
| Method | Path | Description |
|---|---|---|
| GET | `/api/journal` | List journal entries (filter by date range) |
| GET | `/api/journal/export` | Excel export of the journal register |

### `/api/ledger` — `ledger_api.py`
| Method | Path | Description |
|---|---|---|
| GET | `/api/ledger/trial-balance` | Trial balance as of a date |
| GET | `/api/ledger/export` | Excel export of the ledger |
| POST | `/api/ledger/close-period` | Close the current fiscal period, carry forward balances |
| GET | `/api/ledger/periods` | List fiscal periods |

### `/api/tds` — `tds_api.py`
| Method | Path | Description |
|---|---|---|
| GET | `/api/tds` | TDS register for a period |
| GET | `/api/tds/export` | Excel / Form 26Q export |

### `/api/gstr1` — `gstr1_api.py`
| Method | Path | Description |
|---|---|---|
| POST | `/api/gstr1/<period_label>/generate` | Build/rebuild GSTR-1 for a period (async) |
| GET | `/api/gstr1/task_status/<task_id>` | Poll build status |
| GET | `/api/gstr1/<period_label>` | Fetch the built GSTR-1 data |
| GET | `/api/gstr1/<period_label>/export` | Excel export |

All routes above except `/auth/register` and `/auth/login` require a valid `Authorization: Bearer <JWT>` header.

---

## 9. Background jobs (Celery)

Two task modules, registered in `app/celery.py` (`broker`/`backend` = Redis, timezone `Asia/Kolkata`):

- **`tasks/bank_rec_task.py`**
  - `process_pre_data` — persists uploaded bank/ledger rows before matching
  - `run_reconciliation_pipeline` — runs the full matcher pipeline (§5.1) end-to-end
  - `process_post_data` — persists match results, ignored records, and audit items
  - `generate_report_from_db` — regenerates the `.xlsx` report from stored results
  - All tasks are `bind=True, max_retries=3` with a 5-second retry backoff on exception.

- **`tasks/bill_pipeline_task.py`**
  - `process_bill_task` — runs OCR (if an image/PDF was uploaded) → invoice field extraction → direction classification (`input`/`output`) → journal entry construction → TDS engine pass → persists everything, updating the `BillModel` status along the way.

Run a worker with:
```bash
celery -A app.celery worker --loglevel=info
```

---

## 10. Environment variables

Set these in a `.env` file in `Backend/` (loaded via `python-dotenv`) or as real environment variables in your deployment.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | one of this or the `POSTGRES_*` set | — | Full Postgres URL (or `sqlite:///...` for local dev only) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` | — | — | Alternative to `DATABASE_URL`; assembled into a connection string |
| `SECRET_KEY` | **yes** | — | Flask secret key |
| `JWT_SECRET_KEY` | **yes** | — | JWT signing key |
| `EMAIL_OTP_SALT` | for email verification flows | — | Salt for `itsdangerous` signed tokens |
| `CORS_ALLOWED_ORIGINS` | recommended | *(unset = allow all)* | Comma-separated list of allowed frontend origins |
| `REDIS_URL` | **yes** | — | Celery broker + result backend, e.g. `redis://redis_server:6379/0` |
| `CACHE_TYPE` / `CACHE_DEFAULT_TIMEOUT` | no | — | Flask-Caching backend config |
| `STORAGE_DIR` | no | `<Backend>/storage` | Where generated reports are written |
| `BILL_UPLOAD_FOLDER` | no | system temp dir | Where uploaded bill images/PDFs are stored |
| `LOG_TDS` | no | — | Verbose TDS-engine logging toggle |
| `VITE_API_BASE_URL` (frontend, in `Frontend/.env.local`) | recommended | `http://localhost:8000` | Backend base URL the SPA calls |

>  **Do not reuse the `SECRET_KEY` / `JWT_SECRET_KEY` values checked into `docker-compose.yml`** — generate your own before deploying anywhere reachable outside your own machine. See §14.

---

## 11. Getting started

### 11.1 Docker Compose (recommended)

From `Backend/`:

```bash
docker compose up --build
```

This starts four services: `postgres_db` (Postgres 15), `redis_server` (Redis 7), `backend` (Flask API on `:8000`), and `worker` (Celery worker). The database is auto-created on first boot (`ensure_database_exists()` in `core/config.py`).

> The bundled `docker-compose.yml` ships with placeholder Postgres credentials and generated-looking secret keys for convenience — **replace them** before any non-local use.

### 11.2 Manual / local setup

```bash
cd Backend
python -m venv .venv && source .venv/bin/activate     # or use uv / pyproject.toml
pip install -r req.txt

# Postgres and Redis must be running and reachable
export DATABASE_URL=postgresql://user:pass@localhost:5432/finsync
export REDIS_URL=redis://localhost:6379/0
export SECRET_KEY=$(openssl rand -hex 32)
export JWT_SECRET_KEY=$(openssl rand -hex 32)

python main.py            # dev server on :8000
# in a second terminal:
celery -A app.celery worker --loglevel=info
```

`start.sh` / `start.bat` are provided as convenience entrypoints for the container/local flows respectively.

### 11.3 Frontend setup

```bash
cd Frontend
npm install
cp .env.example .env.local     # set VITE_API_BASE_URL to your backend
npm run dev                    # Vite dev server
# npm run build                # production build
```

> `package.json` currently pins most dependencies to `"latest"`. For reproducible builds, pin exact versions (or commit a lockfile you trust) before deploying.

### 11.4 Local LLM setup (Ollama + Phi-3.5)

The AI-matcher fallback (§5.1, stage 4) requires a local [Ollama](https://ollama.com) server running the `phi3` model:

```bash
# install Ollama, then:
ollama pull phi3
ollama serve      # listens on http://127.0.0.1:11434 by default
```

`Backend/README.md` additionally references downloading Phi-3.5-mini-instruct weights directly from Hugging Face (`microsoft/Phi-3.5-mini-instruct`) — this is only needed if you intend to run inference outside of Ollama's own model management. For the default `ai_matcher.py` code path, `ollama pull phi3` is sufficient.

If Ollama isn't running, the AI-matcher stage will fail to produce matches — the pipeline still functions using exact/fuzzy/memory matching, but ambiguous transactions will fall through to the residual reconciler / manual review instead.

---

## 12. Frontend application

Views (`src/views/`), each mapped to a route in `router.js`:

| View | Route | Purpose |
|---|---|---|
| `AuthView` | `/auth` | Login / register |
| `LandingView` | `/` | Product landing / entry point |
| `RunsView` | `/runs` | Reconciliation run history |
| `ResultsView` | `/results/:runId` | Match breakdown for a specific run, with live refresh |
| `BillsView` | `/bills` | Bill scanner — upload & track OCR status |
| `JournalView` | `/journal` | Journal register |
| `LedgerView` | `/ledger` | Trial balance |
| `TdsView` | `/tds` | TDS register |
| `Gstr1View` | `/gstr1` | GSTR-1 filing |

Shared components include `DataGrid` (tabular results), `FileUploadDropzone`, `JournalEntryReview` (approve/edit before posting), `MatchBreakdown` (confidence/adjustment visualization), `NavBar`, `StatCard`, `ToastHost`, and `AppModal`.

State is split across four Pinia stores: `auth` (session/JWT), `ledgerSuite` (journal/ledger/tds/gstr1 data), `reconciliation` (runs/results), and `toast` (global notifications, wired directly into the Axios error interceptor).

---

## 13. Deployment notes

- Frontend and backend are designed to be deployed **separately** (see `Frontend/.env.example`, which references Vercel by name for the frontend). Set `VITE_API_BASE_URL` to the backend's public URL, and set `CORS_ALLOWED_ORIGINS` on the backend to the frontend's deployed origin.
- The backend needs persistent storage for `STORAGE_DIR` (generated reports) and `BILL_UPLOAD_FOLDER` (uploaded bill images) — in Docker Compose these are named volumes; in a multi-instance deployment they should be a shared network volume or object storage, not local disk.
- PaddleOCR and Ollama both add non-trivial container size and startup time / memory footprint — budget for this in whatever compute you deploy on (CPU inference for both OCR and the LLM fallback).

---

## 14. Security checklist before going to production

The codebase gets a lot right (bcrypt hashing, per-tenant scoping, `secure_filename` + extension allow-lists on uploads, parameterized queries via the ORM). Before shipping this beyond local development, address the following:

- [ ] **Rotate the secrets committed in `docker-compose.yml`.** The checked-in `SECRET_KEY` / `JWT_SECRET_KEY` and default `postgres/postgres` DB credentials are real values sitting in source control — treat them as already compromised.
- [ ] **Turn off Flask debug mode in `main.py`.** `app.run(..., debug=True, use_reloader=True)` enables the Werkzeug interactive debugger, which allows remote code execution if an unhandled exception is ever reachable from the outside. Use a production WSGI server (gunicorn/uvicorn-equivalent) instead of `main.py`'s dev server.
- [ ] **Don't expose Postgres/Redis ports to the host** (`5432`/`6379` in `docker-compose.yml`) in any environment reachable beyond your own machine.
- [ ] **Add rate limiting to `/auth/login` and `/auth/register`** — there is currently no brute-force protection.
- [ ] **Move the JWT off `localStorage`** on the frontend (or add short expiries + refresh-token rotation) to reduce blast radius from any XSS.
- [ ] **Set `CORS_ALLOWED_ORIGINS` explicitly** — the app falls back to allowing all origins with only a log warning if it's unset.
- [ ] Remove leftover debug `print(...)` statements from request-handling code paths (e.g. `api/bank_rec_api.py`).
- [ ] Don't commit generated artifacts (the sample `.xlsx` files currently under `Backend/storage/`) or pin frontend dependencies to `"latest"` — both undermine reproducible, auditable deploys.

