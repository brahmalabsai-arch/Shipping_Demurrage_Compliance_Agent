# Port & Customs Clearance Copilot

Multi-agent system that cross-validates a shipment's documents against port demurrage rates, FX
rate rules and a compliance rulebook, with human-gated rule changes and clearance approval.
Specs live in [`docs/`](docs/); the build follows the 13 phases in
[`docs/implementation_plan.md`](docs/implementation_plan.md).

> Full setup and demo instructions arrive in Phase 13. This is the developer quick start.

## Prerequisites

- Python 3.11+
- PostgreSQL 15+ installed natively and running (developed against 18.x)

No Docker needed locally. ChromaDB runs embedded inside the Python process. `docker-compose.yml`
exists only for GitHub CI.

## Setup

All Python dependencies are installed into the project's `.venv` only — never globally.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env   # then set POSTGRES_PASSWORD (other empty keys use defaults)

# One-time: create the app role, app DB and test DB (prompts for the postgres superuser password)
.\.venv\Scripts\python.exe -m scripts.setup_local_db

.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m scripts.seed             # all demo data (shipments, port rates); safe to re-run
```

## Dashboards

```powershell
.\.venv\Scripts\streamlit.exe run dashboards/Home.py
```

Times are shown in IST; everything is stored in UTC.

## Quality checks

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\black.exe --check .
.\.venv\Scripts\mypy.exe .
.\.venv\Scripts\pytest.exe              # all tests; DB tests use POSTGRES_TEST_DB, rebuilt each run
.\.venv\Scripts\pytest.exe -m "not db"  # only the tests that need no database
```

## New migrations

Migrations are prefixed with the phase that introduced them:

```powershell
.\.venv\Scripts\alembic.exe revision --autogenerate --rev-id 0001 -m "shipping"
```
