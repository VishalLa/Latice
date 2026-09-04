# FinSync backend

The FinSync backend is a Flask API for bank reconciliation, bill processing, ledger reporting, TDS, and GSTR-1 workflows. Long-running reconciliation, OCR, and report work is run by Celery with Redis; PostgreSQL stores application data.

## Components

| Component | Responsibility |
| --- | --- |
| `api/` | Flask blueprints and request validation |
| `service/` | Application workflows, persistence, report generation, and ledger rebuilds |
| `tasks/` | Celery tasks for reconciliation, bills, GSTR-1, and reports |
| `matcher/` | Exact, fuzzy, memory, AI-assisted, and residual reconciliation stages |
| `entry_point/` | Bank/ledger file loading, OCR, and invoice extraction |
| `ledger/` | Journal, general-ledger, TDS, and GSTR-1 domain logic |
| `database/` | SQLAlchemy models, database manager, and authentication models |
| `schema/` | Domain data structures used by the pipelines |

The matcher and ledger packages have their own implementation notes: [matcher/README.md](matcher/README.md) and [ledger/README.md](ledger/README.md).

## Architecture

```text
Client
  │ JWT-authenticated HTTP requests
  ▼
Flask API (`api/`)
  ├── synchronous reads: user profile, task state, reconciliation results, trial balance
  └── queued work ──► Redis ──► Celery worker (`tasks/`)
                                      ├── reconciliation and Excel exports
                                      ├── bill OCR, extraction, and journal processing
                                      └── GSTR-1 generation
                                                │
                                                ▼
                                           PostgreSQL

Optional local services: PaddleOCR processes uploaded bills; Ollama is used by the AI matching stage.
```

## Requirements

- Python `>=3.10,<3.13` (the Docker image uses Python 3.11)
- PostgreSQL
- Redis
- Optional: Ollama with the configured model for AI-assisted matching
- Optional: Docker and Docker Compose for the containerised stack

Dependencies are declared in `pyproject.toml`; `req.txt` is used by the Docker image.

## Configuration

Create `Backend/.env`. `Config` loads it at startup.

```dotenv
# Use DATABASE_URL, or provide the full POSTGRES_* set below.
DATABASE_URL=postgresql://finsync:change-me@localhost:5432/finsync
# POSTGRES_USER=finsync
# POSTGRES_PASSWORD=change-me
# POSTGRES_HOST=localhost
# POSTGRES_PORT=5432
# POSTGRES_DB=finsync

REDIS_URL=redis://localhost:6379/0
SECRET_KEY=replace-with-a-long-random-value
JWT_SECRET_KEY=replace-with-a-different-long-random-value

# Optional
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_NAME=phi3:latest
STORAGE_DIR=storage
POOL_WORKERS=1
```

`DATABASE_URL` takes precedence when it is a PostgreSQL URL. A SQLite URL is accepted for local development, but PostgreSQL is the supported database. `STORAGE_DIR` is created automatically and holds uploaded files and generated reports. In containers, the default storage path is `/data/uploads` and the default Ollama host is `ollama`.

For Docker Compose, use container hostnames in `.env`:

```dotenv
POSTGRES_USER=finsync
POSTGRES_PASSWORD=change-me
POSTGRES_DB=finsync
POSTGRES_HOST=postgres_db
POSTGRES_PORT=5432
REDIS_URL=redis://redis_server:6379/0
SECRET_KEY=replace-with-a-long-random-value
JWT_SECRET_KEY=replace-with-a-different-long-random-value
```

## Run locally

From `Backend/`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r req.txt

# Start PostgreSQL and Redis, then configure .env as above.
python main.py
```

In another terminal, with the same virtual environment and configuration:

```bash
celery -A app.celery worker --loglevel=info
```

The Flask development server listens on `http://localhost:8000`.

To enable the optional AI matching stage locally:

```bash
ollama pull phi3:latest
ollama serve
```

## Run with Docker Compose

Add the Docker configuration values above to `Backend/.env`, then run:

```bash
docker compose up --build
```

Compose starts `postgres_db`, `redis_server`, `backend`, `worker`, and `ollama`. The API is exposed on port `8000`; Ollama is exposed on `11434`. PostgreSQL and Redis are also currently exposed on `5432` and `6379` for local development.

Pull the model once the Ollama container is healthy:

```bash
docker compose exec ollama ollama pull phi3:latest
```

## API overview

All routes other than registration and login require `Authorization: Bearer <token>`.

| Area | Routes | Notes |
| --- | --- | --- |
| Authentication | `POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `DELETE /auth/delete` | Registration requires `password`, `first_name`, `phone_no`, and `email`. |
| Reconciliation | `POST /api/bank-rec/reconciliation` | Multipart upload with `ledger_file` and `bank_file`; both must be CSV or XLSX. Returns a Celery task ID. |
| Reconciliation status/results | `GET /api/bank-rec/reconciliation/<run_id>/status`, `GET /api/bank-rec/reconciliation/<run_id>` | Poll task state, then read persisted reconciliation results. |
| Reconciliation follow-up | `POST /api/bank-rec/reconciliation/<run_id>/report`, `POST /api/bank-rec/reconciliation/<run_id>/journal-entries/approve` | Queue a run report or approve a non-empty `entries` list. |
| Bills | `POST /api/pipeline/bills`, `GET /api/pipeline/bills/<bill_id>/status` | Submit multipart `file` (`png`, `jpg`, `jpeg`, or `pdf`) or multipart `raw_data` JSON. `direction` is `input` or `output`. |
| Bill-to-bank full run | `POST /api/pipeline/fullrun`, `GET /api/pipeline/tasks/<task_id>` | Upload bill files and a bank statement together. The worker extracts and stores the bills, builds their bank-facing ledger rows, then runs reconciliation. |
| GSTR-1 pipeline | `POST /api/pipeline/gstr1`, `GET /api/pipeline/tasks/<task_id>` | Requires `period_label`, `period_start`, and `period_end`. |
| Ledger | `GET /api/ledger/trial-balance?as_on=YYYY-MM-DD` | Returns the trial balance for the requested date. |
| Reports | `POST /api/reports/{bank-reconciliation,gstr1,journal,ledger,tds}` | Queues an Excel report. Use `GET /api/reports/tasks/<task_id>` to poll and `GET /api/reports/tasks/<task_id>/download` to download it. |

Report request fields:

| Endpoint | Required JSON fields |
| --- | --- |
| `/api/reports/bank-reconciliation` | `run_id` |
| `/api/reports/gstr1` | `period_label` |
| `/api/reports/journal` | `date_from`, `date_to` (`YYYY-MM-DD`) |
| `/api/reports/ledger` | `as_on` (`YYYY-MM-DD`) |
| `/api/reports/tds` | `period_start`, `period_end` (`YYYY-MM-DD`) |

### Bill-to-bank full run

`POST /api/pipeline/fullrun` is a multipart endpoint for the end-to-end flow.
Send one or more bill files under `bill_files` and a CSV or XLSX statement
under `bank_statement` (the aliases `bills` and `bank_file` are also
accepted). Bill files may be `png`, `jpg`, `jpeg`, or `pdf`.

```bash
curl -X POST http://localhost:8000/api/pipeline/fullrun \
  -H "Authorization: Bearer <token>" \
  -F "bill_files=@purchase-invoice.jpg" \
  -F "bill_files=@paid-expense.pdf" \
  -F "bank_statement=@statement.xlsx"
```

The response is `202 Accepted` and includes `task_id`, `run_id`, and a
`status_url`. Poll `GET /api/pipeline/tasks/<task_id>` until it succeeds.
The completed result includes each bill's processing result, the count of
bank-reconcilable ledger rows created from its persisted journal entries, and
the normal reconciliation summary/report links. Only bills with a Cash or
Bank journal posting produce rows that can be matched to the statement.

## Background work

Celery imports these task modules through `app/celery.py` and uses Redis for both broker and result backend:

- `tasks.bank_rec`: pre/post persistence helpers and the reconciliation pipeline.
- `tasks.bill_pipeline`: bill processing and GSTR-1 generation.
- `tasks.full_run`: bill OCR/extraction, journal persistence, automatic ledger formation, and bank reconciliation in one job.
- `tasks.generate_report_tasks`: bank reconciliation, GSTR-1, journal, ledger, and TDS report generation.

Tasks are configured to retry up to three times, except `tasks.full_run`, which runs once to avoid creating duplicate bills and journal entries. Keep the API and worker connected to the same Redis instance and database.

## Operational notes

- `main.py` is a development entrypoint. Run the Flask app behind a production WSGI server for deployment.
- Persist `STORAGE_DIR` in production; report downloads depend on the generated file still being available there.
- Replace all example credentials and secrets. Do not expose PostgreSQL or Redis ports outside a trusted network.
- Flask-CORS is installed but is not configured in the application factory. Configure CORS explicitly if a separately hosted browser client needs cross-origin access.
