# Progress record — after Phase 1 (Shipping Records)

> **How to use this file:** attach it as the only context in a new chat to continue the build.
> It supersedes every earlier progress file. The authoritative specs are in `docs/`
> (`problem_statement.md`, `architecture.md`, `implementation_plan.md`, `conventions.md`,
> `edge_cases.md`) — read the relevant phase there before starting work.

- **Last updated:** 2026-09-27
- **Phases complete:** 0, 1 (of 0–13)
- **Next up:** Phase 2 — Demurrage & Port Charges

---

## 1. Project in one paragraph

**Port & Customs Clearance Copilot** — a locally-run multi-agent system (LangGraph + MCP) that
ingests a simulated shipment's documents and lifecycle events, cross-checks them against three
independently editable synthetic data sources (port demurrage rates, FX-rate rules, a
compliance rulebook), and produces discrepancy flags, a demurrage/hold cost estimate and a
human-gated clearance workflow. Every data domain is exposed both through a Streamlit dashboard
and an MCP server, both calling **one shared service layer** (no drift). A scheduled Regulatory
Watch agent diffs a simulated customs bulletin and emails a reviewer via real Gmail OAuth.
Built in 14 phases (0–13), one git branch per phase.

## 2. Standing rules (agreed with the user — always follow)

1. **Python deps go only into the project venv** `.venv` (Python 3.13.14). Always invoke tools
   as `.\.venv\Scripts\python.exe -m ...` / `.\.venv\Scripts\<tool>.exe`. Declare every new
   dependency in `pyproject.toml`, then `pip install -e ".[dev]"`.
2. **No Docker in local development.** Postgres is the natively installed **PostgreSQL 18.4**
   Windows service (`postgresql-x64-18`, `localhost:5432`, binaries in
   `C:\Program Files\PostgreSQL\18\bin`, not on PATH). ChromaDB is **embedded**
   (`chromadb.PersistentClient(path=CHROMA_PATH)`, default `./chroma_data/`).
   `docker-compose.yml` exists **only for GitHub CI** (Postgres 18.4 service).
3. **Commits are authored by the user's identity** `brahmalabsai-arch <brahmalabs.ai@gmail.com>`
   (already the global git config). **No `Co-Authored-By: Claude` trailer** or any Claude
   attribution. Commit only when the user asks.
4. **After each phase:** full suite green → commit phase on its branch → write a new
   self-sufficient `docs/progress/progress_<N>.md` → commit it → fast-forward `main` →
   tag `phase-<N>-complete` → create the next phase branch.
5. Conventional commits (`feat(demurrage): ...`, `docs: ...`); branch names
   `phase-<N>-<slug>`.
6. The user works on **Windows 11 / PowerShell** in VS Code. PowerShell 5.1: no `&&`; pass
   multi-line commit messages via `git commit -F <file>`.
7. `docs/simple_docs/` (user's plain-language docs) is git-ignored and must never be committed
   or edited by the assistant.

## 3. Repository state

- Branches: `main` (= `phase-1-complete`), `phase-0-foundations`, `phase-1-shipping-records`.
- Tags: `phase-0-complete`, `phase-1-complete`.
- **No git remote yet** — nothing pushed; CI (`.github/workflows/ci.yml`) runs on first push.
- `.env` (git-ignored) exists with a generated `POSTGRES_PASSWORD` for role `customs`; other keys
  empty → defaults from `config/settings.py`. The Postgres **superuser** (`postgres`) password
  is known only to the user and is never stored in `.env`.
- Databases: `customs_copilot` (dev, migrated to `0001`, seeded with the 4 fixtures, 28 audit
  events) and `customs_copilot_test` (disposable; pytest rebuilds it every run). Both owned by
  `customs`, both `timezone=UTC`.

## 4. Commands

```powershell
# one-time (already done on this machine) — prompts for the postgres superuser password
.\.venv\Scripts\python.exe -m scripts.setup_local_db   # idempotent: role + app DB + test DB

.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m scripts.seed_shipments    # idempotent
.\.venv\Scripts\streamlit.exe run dashboards/Home.py

# quality gate (all must pass before a phase is "done")
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\black.exe --check .
.\.venv\Scripts\mypy.exe .            # strict
.\.venv\Scripts\pytest.exe            # 50 tests, ~5 s
.\.venv\Scripts\alembic.exe check     # models == migrations

# new migration for phase N (conventions: <phase-seq>_<slug>.py)
.\.venv\Scripts\alembic.exe revision --autogenerate --rev-id 0002 -m "demurrage"
```

## 5. What exists (as built)

```
config/settings.py        Settings (pydantic-settings, env_ignore_empty); database_url,
                          test_database_url (psycopg v3 URLs built from POSTGRES_* keys)
models/enums.py           ShipmentStatus (incl. 'cancelled'), DocType  (StrEnum)
models/db_models.py       Base (naming convention), utcnow(), _pg_enum(), _uuid_pk();
                          Shipment, Document (schema shipping), AuditEvent (schema audit);
                          DOMAIN_SCHEMAS tuple
models/schemas.py         ShipmentCreate/Update/Record, DocumentCreate/Amend/Record;
                          annotated types CurrencyCode, ContainerId, HsCode, PortCode, ...
services/db.py            get_engine(), get_session_factory(), session_scope()
services/errors.py        CustomsCopilotError root + Phase 1 errors
services/audit_service.py record(session, actor, action, entity_type, entity_id, payload)
services/shipping_service.py   (see §6)
alembic/env.py            URL from Settings; accepts config.attributes["connection"] (tests)
alembic/versions/0001_shipping.py   shipping.shipments, shipping.documents, audit.events,
                          enums shipping.shipment_status / shipping.doc_type
dashboards/Home.py, dashboards/ui.py (IST helpers, run_action, flash),
dashboards/pages/1_Shipping_Ops.py
scripts/setup_local_db.py + .sql, scripts/seed_shipments.py
tests/conftest.py         DB harness (see §7)
tests/fixtures/shipments.py   4 seed fixtures + make_shipment(**overrides)
tests/test_foundations.py, test_infrastructure.py,
tests/test_services/test_shipping_service.py, tests/test_scripts/test_seed_shipments.py,
tests/test_dashboards/test_shipping_ops_page.py
.github/workflows/ci.yml  job `checks` (py3.11+3.13: ruff, black, mypy, pytest -m "not db");
                          job `db` (compose Postgres: alembic upgrade+check, pytest -m db)
```
Empty packages awaiting later phases: `agents/`, `rag/`, `scheduler/`, `integrations/`,
`mcp_servers/<six servers>/`, `tests/test_agents/`, `tests/test_mcp_servers/`.

## 6. Established patterns — reuse them in every phase

- **Service layer signature:** `fn(session: Session, ..., actor: str) -> <Noun>Record`. Services
  only `flush()`; the caller owns the transaction via `with session_scope() as session:`.
  Dashboards, scripts and (Phase 5) MCP handlers all call services this way.
- **Audit in the same transaction:** every mutating service function calls
  `audit_service.record(...)` before returning. Action names: `<entity>.<verb>` (e.g.
  `shipment.created`, `shipment.status_changed`, `document.amended`). Payload may hold
  Decimal/date/UUID/enum — `record` normalises to JSON.
- **ORM rows never leave services:** return `XRecord.model_validate(row)` (Pydantic,
  `from_attributes=True`, frozen). Inputs are `XCreate` / `XUpdate` (`extra="forbid"`);
  updates apply `model_dump(exclude_unset=True)` and validate the *merged* state before mutating.
- **Typed errors** in `services/errors.py`, one per failure mode, subclass `CustomsCopilotError`.
  Unique-constraint violations: insert inside `session.begin_nested()`, catch `IntegrityError`,
  match the constraint name, raise the typed error (session stays usable).
- **No deletes.** History via status / version / `superseded_by`.
- **Money = `Numeric` + `CHAR(3)` currency; lifecycle dates = `Date`; timestamps =
  `DateTime(timezone=True)` in UTC** (`default=utcnow, server_default=func.now()`).
- **Postgres ENUMs** via `_pg_enum(EnumCls, name, schema)` (stores lowercase `.value`).
- **Migrations are hand-written** from the models, then verified with `alembic check`
  ("No new upgrade operations detected"). Each migration `CREATE SCHEMA IF NOT EXISTS` for the
  schema it introduces; downgrade drops tables, enum types and the schema.
- **Dashboards:** every widget has an explicit `key` (suffix with the entity id for per-entity
  forms); writes go through `dashboards.ui.run_action(lambda session: ..., "success msg")`;
  timestamps shown via `fmt_ist`. Pydantic `ValidationError` and domain errors render as
  `st.error`.

### Shipping service API (Phase 1)
`create_shipment`, `get_shipment`, `get_shipment_by_container_id`, `get_active_shipment`
(raises `ShipmentCancelledError` — agents must use this), `list_shipments(status=None)`,
`update_shipment`, `update_status`, `allowed_transitions`, `add_document`, `amend_document`,
`get_documents(include_superseded=False)`.
Lifecycle `TRANSITIONS`: quoted→{booked,cancelled}; booked→{in_transit,cancelled};
in_transit→{docked,cancelled}; docked→{under_review,held}; under_review→{cleared,held};
held→{under_review}; cleared→{closed}; closed, cancelled terminal. (Phase 11 will add a human
approval gate before `cleared`.) `quote_expiry_date` = `quote_date + quote_validity_days`
(7–14), computed by the service.

## 7. Test harness

- DB tests use `POSTGRES_TEST_DB`. Session fixture `db_engine`: drops all `DOMAIN_SCHEMAS` +
  `alembic_version`, then upgrade head → downgrade base → upgrade head (proves reversibility).
- Per test: one outer transaction rolled back afterwards. `session_factory` binds sessions with
  `join_transaction_mode="create_savepoint"` **and monkeypatches
  `services.db.get_session_factory`**, so code using `session_scope()` (dashboards, scripts)
  writes into the same rolled-back transaction. `db_session` = a session from that factory.
- Any test using `db_engine`/`db_connection`/`session_factory`/`db_session` is auto-marked `db`.
  `pytest` runs everything locally; CI splits `-m "not db"` / `-m db`.
- Dashboard tests use `streamlit.testing.v1.AppTest.from_file(..., default_timeout=60)`; form
  submit buttons are addressed by their `key`.
- **When adding a domain schema**, append it to `DOMAIN_SCHEMAS` in `models/db_models.py` if not
  already present (it already lists shipping, demurrage, fx, compliance, bulletin, audit) — the
  harness drops those schemas on every run.

## 8. Seed data — JNPT auto-parts case study (`tests/fixtures/shipments.py`)

All ship to **INNSA** (Nhava Sheva/JNPT), HS heading 8708. Dates are **fixed**, not relative to
today, so later FX/demurrage calculations are deterministic.

| key | container | origin | ccy | value | docs | final status | notes |
|---|---|---|---|---|---|---|---|
| clean | MSKU7001001 | CNSHA | USD | 84,000 | invoice, packing list, BoL (qty 1200) | docked | ETA 2026-09-20 |
| qty_mismatch | MSCU7002002 | KRPUS | USD | 136,000 | invoice 800 vs packing list **760**, BoL 800 | docked | ETA 2026-09-22 |
| missing_document | CMAU7003003 | JPYOK | JPY | 18,500,000 | packing list, BoL — **no invoice** | in_transit | ETA 2026-10-05, invoice_date NULL |
| already_delayed | HLXU7004004 | DEHAM | EUR | 96,500 | all 3 consistent (qty 600) | **held** | ETA/docked 2026-09-08 |

BoL dates: 2026-08-22, 08-25, 09-10, 08-01. Invoice dates: 08-18, 08-20, —, 07-28.
Phase 2/3 rate data must cover INNSA and USD/INR, EUR/INR, JPY/INR on/around these dates.

## 9. Decisions and deviations from the original spec (already reflected in `docs/`)

- No Docker locally; native Postgres 18 (spec said 15 — 15+ accepted) + embedded Chroma.
  Problem statement NFR + DoD #5, architecture §1/§10, plan Phase 0/13 and edge case Phase 13
  were rewritten accordingly.
- `cancelled` added to `ShipmentStatus` (edge cases required it; architecture §3.1 updated).
- `audit.events` created in migration `0001` (conventions require Phase 1 writes be audited).
  `audit_service.query_events` is still Phase 12.
- `list_shipments(status=None)` implements the plan's "`list_by_status`" (matches the MCP
  contract and verb-first naming).
- Separate test database `customs_copilot_test` (`POSTGRES_TEST_DB`); pytest marker `db`
  replaced Phase 0's `integration` marker.
- `scripts/` is a package; run scripts as modules (`python -m scripts.<name>`). The seed script
  imports fixtures from `tests/fixtures/` (conventions place fixtures there).
- mypy runs strict everywhere with `python_version` unset (numpy stubs need ≥3.12 syntax);
  dashboards/tests have relaxed `disallow_untyped_defs`.

## 10. Gotchas already hit (don't rediscover them)

- **ruff isort + local `alembic/` folder:** `known-third-party = ["alembic"]` is set in
  `pyproject.toml`; keep it.
- **Streamlit + mypy:** passing an inline list/`list(Enum)` as `st.selectbox` options makes mypy
  choose the `-> None` overload. Use a module-level annotated variable
  (`DOC_TYPES: list[DocType] = list(DocType)`).
- SQLAlchemy `__table_args__` must be a tuple (`({"schema": "x"},)`), not a dict (ruff RUF012).
- Pydantic class-level constants need `ClassVar[...]` (a leading-underscore attribute becomes a
  private attr).
- Python `Path.write_text` on Windows writes CRLF; `.gitattributes` (`* text=auto eol=lf`)
  normalises in the repo — the CRLF warnings on `git add` are harmless.
- `Settings()` reads `.env` from the project root; tests that construct `Settings` pass
  `_env_file=None` and clear env vars.
- Forgotten Postgres superuser password: reset by temporarily switching localhost lines in
  `C:\Program Files\PostgreSQL\18\data\pg_hba.conf` to `trust` (admin PowerShell), restart the
  service, `ALTER ROLE postgres PASSWORD ...`, restore the file, restart. (A timestamped
  `pg_hba.conf.bak-*` from the last reset may still sit in that folder.)

## 11. Next: Phase 2 — Demurrage & Port Charges

From `docs/implementation_plan.md` and `docs/edge_cases.md`:

- **Deliverables:** `models/db_models.py::PortRate` (schema `demurrage`, table `port_rates`:
  id, port_code, container_type, currency CHAR(3), free_days INT, daily_rate NUMERIC,
  effective_from DATE, effective_to DATE NULL); migration `0002_demurrage`;
  `services/demurrage_service.py::get_rate(port_code, container_type, as_of)`, `upsert_rate`;
  `dashboards/pages/2_Demurrage.py`.
- **Exit criteria:** unconfigured port → `PortRateNotFoundError` (assert the type);
  a dashboard edit is visible to `get_rate` in the same test run without restart.
- **Edge cases:** `get_rate("UNKNOWN","20ft",date)` raises; `free_days=0` accepted;
  `get_rate(as_of=dock_date)` returns the rate effective on that date even after a future-dated
  rate is added (historical lookup, not "latest row"); negative `daily_rate`/`free_days`
  rejected by Pydantic (`ge=0`) before reaching the service.
- Follow §6 patterns (audit every upsert; no deletes — superseding a rate should close the old
  row's `effective_to` rather than overwrite it). Seed INNSA rates covering the fixture dates.
