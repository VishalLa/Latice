# FinSync Backend (Architecture & Documentation Showcase)

> ⚠️ **Note:** The source code for this project is currently kept **private**. This repository serves as a portfolio showcase to explain the architecture, system design, and API structure of the FinSync backend.

**🌟 Live Demo:** [https://bank-rec-frontend.vercel.app](https://bank-rec-frontend.vercel.app)

---

The FinSync backend is a robust Flask API designed for bank reconciliation, bill processing, ledger reporting, TDS, and GSTR-1 workflows. Long-running reconciliation, OCR, and report work is executed by Celery with Redis, while PostgreSQL is used for reliable application data storage.

## Components

| Component | Responsibility |
| --- | --- |
| `api/` | Flask blueprints and request validation |
| `service/` | The only PostgreSQL client; DB RPC, persistence, reports, and legacy DB workflows |
| `matcher/` | Exact, fuzzy, memory, AI-assisted, and residual reconciliation stages |
| `entry_point/` | Bank/ledger file loading and the opt-in OCR worker app |
| `ledger/` | Journal, general-ledger, TDS, and GSTR-1 domain logic |
| `database/` | SQLAlchemy models, database manager, and authentication models |
| `schema/` | Domain data structures used by the pipelines |
| `shared/` | Queue messages, light settings, storage, and Celery factory |

## Architecture

```text
Client
  │ JWT-authenticated HTTP requests
  ▼
Flask API (`api/`)
  ├── synchronous DB requests ──► Redis RPC ──► service worker
  └── queued work ──────────────► Redis queues
                                     ├── entry_point worker
                                     ├── matcher worker
                                     └── service worker
                                           │
                                           ▼
                                      PostgreSQL

Optional local services: PaddleOCR processes uploaded bills; Ollama is used by the AI matching stage.
```

For bank reconciliation, the queued stages are currently:

```text
API upload
  -> entry.process_run        parse ledger and bank files
  -> db.store_inputs          create the run and persist parsed rows
  -> matcher.run              reconcile the normalized rows
  -> service.save_results     build the reconciliation workbook
  -> db.store_results         persist matches, audit rows, and final status
```

Queue messages contain the run/user IDs and references under `STORAGE_DIR/runs/<run_id>/`; raw files and normalized/results JSON stay in shared storage rather than being sent through Redis. The database-input stage intentionally finishes before matching starts so match rows can reference persisted ledger and bank rows. The API, entry, and matcher processes do not import database code or receive database credentials. All consumers must share the same storage volume.

## API Overview

All routes other than registration and login require `Authorization: Bearer <token>`.

| Area | Routes | Notes |
| --- | --- | --- |
| Authentication | `POST /auth/register`, `POST /auth/login`, `GET /auth/verify-email?token=...`, `POST /auth/logout`, `GET /auth/me`, `DELETE /auth/delete` | An unverified user receives a verification link on login and cannot obtain a JWT until verified. Registration requires `password`, `first_name`, `phone_no`, and `email`. |
| Reconciliation | `POST /api/bank-rec/reconciliation` | Multipart upload with `ledger_file` and `bank_file`; both must be CSV or XLSX. Returns a Celery task ID. |
| Reconciliation status/results | `GET /api/bank-rec/reconciliation/<run_id>/status`, `GET /api/bank-rec/reconciliation/<run_id>` | Poll task state, then read persisted reconciliation results. |
| Saved reconciliation runs | `GET /api/bank-rec/previous/runs/<user_id>`, `GET /api/bank-rec/previous/runs/<user_id>/<run_id>`, `DELETE /api/bank-rec/previous/runs/<user_id>/<run_id>` | JWT identity must match `user_id`. |
| Reconciliation follow-up | `POST /api/bank-rec/reconciliation/<run_id>/report`, `POST /api/bank-rec/reconciliation/<run_id>/journal-entries/approve` | Queue a run report or approve a non-empty `entries` list. |
| Bills | `POST /api/pipeline/bills`, `GET /api/pipeline/bills/<bill_id>/status` | Submit multipart `file` (`png`, `jpg`, `jpeg`, or `pdf`) or multipart `raw_data` JSON. `direction` is `input` or `output`. |
| Bill-to-bank full run | `POST /api/pipeline/fullrun`, `GET /api/pipeline/tasks/<task_id>` | Upload bill files and a bank statement together. The worker extracts and stores the bills, builds their bank-facing ledger rows, then runs reconciliation. |
| GSTR-1 pipeline | `POST /api/pipeline/gstr1`, `GET /api/pipeline/tasks/<task_id>` | Requires `period_label`, `period_start`, and `period_end`. |
| Ledger | `GET /api/ledger/trial-balance?as_on=YYYY-MM-DD` | Returns the trial balance for the requested date. |
| Reports | `POST /api/reports/{bank-reconciliation,gstr1,journal,ledger,tds}` | Queues an Excel report. Use `GET /api/reports/tasks/<task_id>` to poll and `GET /api/reports/tasks/<task_id>/download` to download it. |

### Bill-to-bank full run

`POST /api/pipeline/fullrun` is a multipart endpoint for the end-to-end flow.
Send one or more bill files under `bill_files` and a CSV or XLSX statement under `bank_statement`. 

```bash
curl -X POST http://localhost:8000/api/pipeline/fullrun \
  -H "Authorization: Bearer <token>" \
  -F "bill_files=@purchase-invoice.jpg" \
  -F "bill_files=@paid-expense.pdf" \
  -F "bank_statement=@statement.xlsx"
```

The response is `202 Accepted` and includes `task_id`, `run_id`, and a `status_url`. Poll `GET /api/pipeline/tasks/<task_id>` until it succeeds.

## Internal Development & Setup Notes
*The following sections are preserved from the internal development repository to provide insight into the tech stack requirements and local environment configurations.*

### Requirements
- Python `>=3.10,<3.13` (Docker image uses Python 3.11)
- PostgreSQL
- Redis
- Optional: Ollama with the configured model for AI-assisted matching
- Optional: Docker and Docker Compose for the containerized stack

### Background Work
Celery uses Redis for both broker and result backend. Worker apps are declared with their domain packages:
- `entry_point.celery_app`: file parsing and handoff to the service.
- `matcher.celery_app`: reconciliation and handoff to the service.
- `service.celery_app`: DB persistence, synchronous API RPC, bills, reports, and full-run workflows.
- `ledger.celery_app`: reserved for ledger-specific worker tasks; DB-dependent ledger operations remain in `service`.

Only the service worker receives PostgreSQL credentials. The API uses Celery result RPC for DB-backed operations, and workers share the same Redis and storage volume.

### Operational Notes
- `main.py` is a development entrypoint. The Flask app runs behind a production WSGI server for deployment.
- `STORAGE_DIR` is persisted in production; report downloads depend on the generated file still being available there.
- Database access and credentials are strictly isolated to the service worker to minimize exposure.
