# Progress record — after Phase 2 (Demurrage & Port Charges)

> **How to use this file:** attach it as the only context in a new chat to continue the build.
> It supersedes `progress_1.md`. The authoritative specs are in `docs/`
> (`problem_statement.md`, `architecture.md`, `implementation_plan.md`, `conventions.md`,
> `edge_cases.md`) — read the relevant phase there before starting work.

- **Last updated:** 2026-09-27
- **Phases complete:** 0, 1, 2 (of 0–13)
- **Next up:** Phase 3 — Currency & Exchange Rate Service (user plans to start 2026-09-28)
- **Open decision to raise with the user before Phase 8:** see §10.

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
4. **After each phase:** full quality gate green → (user reviews; offer to launch the dashboard)
   → commit phase on its branch → write a new self-sufficient
   `docs/progress/progress_<N>.md` → commit it → fast-forward `main` → tag
   `phase-<N>-complete` → create the next phase branch when work on it starts.
5. Conventional commits (`feat(fx): ...`, `docs: ...`); branch names `phase-<N>-<slug>`
   (Phase 3: `phase-3-fx-service`).
6. The user works on **Windows 11 / PowerShell** in VS Code. PowerShell 5.1: no `&&`; pass
   multi-line commit messages via `git commit -F <file>` (write the message to a temp file).
7. `docs/simple_docs/` (user's plain-language docs) is git-ignored and must never be committed
   or edited by the assistant.
8. Resolve spec gaps with a sensible, documented choice and tell the user; ask only when the
   decision genuinely changes the data model or scope (as in §10).

## 3. Repository state

- Branches: `main` (= tag `phase-2-complete`), `phase-0-foundations`,
  `phase-1-shipping-records`, `phase-2-demurrage`. Phase 3 branch not yet created.
- Tags: `phase-0-complete`, `phase-1-complete`, `phase-2-complete`.
- **No git remote yet** — nothing pushed; CI (`.github/workflows/ci.yml`) runs on first push.
- `.env` (git-ignored) holds a generated `POSTGRES_PASSWORD` for role `customs`; other keys
  empty → defaults in `config/settings.py`. The Postgres **superuser** (`postgres`) password is
  known only to the user (needed only for `scripts.setup_local_db`) and never stored in `.env`.
- Databases (both owned by `customs`, `timezone=UTC`):
  - `customs_copilot` (dev) — migrated to `0002`; 4 fixture shipments; 9 tariff periods
    (8 seeded + the user's review edits, see §8); 46 audit events.
  - `customs_copilot_test` — disposable; pytest wipes and rebuilds it every run.
- Streamlit is **not** running (stopped after the Phase 2 review).

## 4. Commands

```powershell
# one-time (already done on this machine) — prompts for the postgres superuser password
.\.venv\Scripts\python.exe -m scripts.setup_local_db   # idempotent: role + app DB + test DB

.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m scripts.seed              # every domain seed; idempotent
.\.venv\Scripts\streamlit.exe run dashboards/Home.py    # http://localhost:8501
#   pages: /Shipping_Ops, /Demurrage  (for review, run headless in the background:
#   streamlit run dashboards/Home.py --server.headless true --server.port 8501)

# quality gate (all must pass before a phase is "done")
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\black.exe --check .
.\.venv\Scripts\mypy.exe .            # strict
.\.venv\Scripts\pytest.exe            # 80 tests, ~5 s
.\.venv\Scripts\alembic.exe check     # models == migrations

# new migration for phase N (conventions: <phase-seq>_<slug>.py) — hand-write, then check
.\.venv\Scripts\alembic.exe revision --rev-id 0003 -m "fx"
```

## 5. What exists (as built)

```
config/settings.py        Settings (pydantic-settings, env_ignore_empty); database_url,
                          test_database_url (psycopg v3 URLs built from POSTGRES_* keys)
models/enums.py           ShipmentStatus (incl. 'cancelled'), ContainerType, DocType (StrEnum)
models/db_models.py       Base (naming convention), utcnow(), _pg_enum(), _uuid_pk();
                          Shipment, Document (schema shipping); PortRate (schema demurrage);
                          AuditEvent (schema audit); DOMAIN_SCHEMAS tuple
models/schemas.py         ShipmentCreate/Update/Record, DocumentCreate/Amend/Record,
                          PortRateUpsert/Record, DemurrageCharge; annotated types
                          CurrencyCode, ContainerId, HsCode, PortCode, NonNegativeDecimal, ...
services/db.py            get_engine(), get_session_factory(), session_scope()
services/errors.py        CustomsCopilotError root + Phase 1/2 errors
services/audit_service.py record(session, actor, action, entity_type, entity_id, payload)
services/shipping_service.py, services/demurrage_service.py   (APIs in §6)
alembic/env.py            URL from Settings; accepts config.attributes["connection"] (tests)
alembic/versions/0001_shipping.py   shipping.shipments, shipping.documents, audit.events,
                          enums shipping.shipment_status / shipping.doc_type
alembic/versions/0002_demurrage.py  btree_gist extension, demurrage.port_rates
dashboards/Home.py, dashboards/ui.py (IST helpers, run_action, flash),
dashboards/pages/1_Shipping_Ops.py, dashboards/pages/2_Demurrage.py
scripts/__init__.py, setup_local_db.py + .sql, seed.py (runs all), seed_shipments.py,
                          seed_port_rates.py
tests/conftest.py         DB harness (see §7)
tests/fixtures/shipments.py (4 seed shipments + make_shipment), tests/fixtures/port_rates.py
tests/test_foundations.py, test_infrastructure.py,
tests/test_services/test_shipping_service.py, test_demurrage_service.py,
tests/test_scripts/test_seed_shipments.py, test_seed_port_rates.py,
tests/test_dashboards/test_shipping_ops_page.py, test_demurrage_page.py
.github/workflows/ci.yml  job `checks` (py3.11+3.13: ruff, black, mypy, pytest -m "not db");
                          job `db` (compose Postgres: alembic upgrade+check, pytest -m db)
```
Empty packages awaiting later phases: `agents/`, `rag/`, `scheduler/`, `integrations/`,
`mcp_servers/<six servers>/`, `tests/test_agents/`, `tests/test_mcp_servers/`.

## 6. Established patterns — reuse them in every phase

- **Service signature:** `fn(session: Session, ..., actor: str) -> <Noun>Record`. Services only
  `flush()`; the caller owns the transaction via `with session_scope() as session:`.
  Dashboards, scripts and (Phase 5) MCP handlers all call services this way.
- **Audit in the same transaction:** every mutating service function calls
  `audit_service.record(...)` before returning; action names `<entity>.<verb>`
  (`shipment.created`, `document.amended`, `port_rate.superseded`, ...). A no-op change writes
  no event. Payload may hold Decimal/date/UUID/enum — `record` normalises to JSON.
- **ORM rows never leave services:** return `XRecord.model_validate(row)` (Pydantic,
  `from_attributes=True`, frozen). Inputs are `XCreate` / `XUpdate` / `XUpsert`
  (`extra="forbid"`); updates apply `model_dump(exclude_unset=True)` and validate the *merged*
  state before mutating.
- **Typed errors** in `services/errors.py`, one per failure mode, subclass `CustomsCopilotError`
  — never a bare `ValueError` for a domain failure. Unique/constraint violations: write inside
  `session.begin_nested()`, catch `IntegrityError`, match the constraint name, raise the typed
  error (session stays usable).
- **Lookups are deterministic: raise, don't guess.** No nearest-date / latest-row fallbacks.
- **No deletes.** History via status / version / `superseded_by` / closing `effective_to`.
- **Integrity in the database too:** CHECK constraints for non-negative values and valid
  periods; GiST exclusion constraints (`btree_gist` installed) for non-overlapping date
  periods. Tests prove the DB rejects bad rows even when the service is bypassed.
- **Money = `Numeric` + `CHAR(3)` currency; lifecycle dates = `Date`; timestamps =
  `DateTime(timezone=True)` in UTC** (`default=utcnow, server_default=func.now()`).
- **Postgres ENUMs** via `_pg_enum(EnumCls, name, schema)` (stores lowercase `.value`); where
  the spec says TEXT (e.g. `container_type`), keep TEXT and validate with a StrEnum in Pydantic.
- **Migrations are hand-written** from the models, then verified with `alembic check`
  ("No new upgrade operations detected"). Each creates its schema (`CREATE SCHEMA IF NOT
  EXISTS`); downgrade drops tables, enum types and the schema.
- **Seeds:** fixture data lives in `tests/fixtures/<domain>.py`; `scripts/seed_<domain>.py`
  creates via the service layer, skips anything already present (never overwrites dashboard
  edits), one transaction per item; register it in `scripts/seed.py`.
- **Dashboards:** every widget has an explicit `key` (suffix with the entity id for per-entity
  forms); writes go through `dashboards.ui.run_action(lambda session: ..., "success msg")`;
  timestamps via `fmt_ist`; option lists passed to `st.selectbox` must be module-level
  annotated variables (see §11). No `st.cache_*` on service reads (edits must be visible
  immediately). Leave `min_value` off numeric inputs whose validation should be demonstrated
  by Pydantic.

### Shipping service API (Phase 1)
`create_shipment`, `get_shipment`, `get_shipment_by_container_id`, `get_active_shipment`
(raises `ShipmentCancelledError` — agents must use this), `list_shipments(status=None)`,
`update_shipment`, `update_status`, `allowed_transitions`, `add_document`, `amend_document`,
`get_documents(include_superseded=False)`.
`TRANSITIONS`: quoted→{booked,cancelled}; booked→{in_transit,cancelled};
in_transit→{docked,cancelled}; docked→{under_review,held}; under_review→{cleared,held};
held→{under_review}; cleared→{closed}; closed, cancelled terminal. (Phase 11 adds a human
approval gate before `cleared`.) `quote_expiry_date = quote_date + quote_validity_days` (7–14).

### Demurrage service API (Phase 2)
- `get_rate(session, port_code, container_type, as_of) -> PortRateRecord` — the period in force
  on `as_of` (`effective_from <= as_of <= effective_to`, NULL end = open). None →
  `PortRateNotFoundError`.
- `list_rates(session, port_code=None)`, `list_ports(session)`.
- `upsert_rate(session, PortRateUpsert, actor)` — same `effective_from` as an existing period →
  in-place correction (`port_rate.updated`); new `effective_from` → new period
  (`port_rate.created`) and the period in force that day is closed the day before
  (`port_rate.superseded`). Empty `effective_to` → day before the next period, else open.
  Explicit `effective_to` into the next period → `OverlappingRateError`.
- `demurrage_charge(rate, days_in_port) -> DemurrageCharge` — `chargeable_days =
  max(0, days - free_days)`, `amount = chargeable_days × daily_rate` in the tariff currency
  (FX conversion is the Phase 8 agent's job); negative days → `NegativeStayError`.

## 7. Test harness

- DB tests use `POSTGRES_TEST_DB`. Session fixture `db_engine`: drops all `DOMAIN_SCHEMAS` +
  `alembic_version`, then upgrade head → downgrade base → upgrade head (proves reversibility).
- Per test: one outer transaction rolled back afterwards. `session_factory` binds sessions with
  `join_transaction_mode="create_savepoint"` **and monkeypatches
  `services.db.get_session_factory`**, so code using `session_scope()` (dashboards, scripts)
  writes into the same rolled-back transaction. `db_session` = a session from that factory.
- Tests using `db_engine`/`db_connection`/`session_factory`/`db_session` are auto-marked `db`.
  Local `pytest` runs everything; CI splits `-m "not db"` / `-m db`.
- Dashboard tests: `streamlit.testing.v1.AppTest.from_file(..., default_timeout=60)`; form
  submit buttons addressed by `key`; `at.error`, `at.success`, `at.metric` for assertions.
- `DOMAIN_SCHEMAS` already lists shipping, demurrage, fx, compliance, bulletin, audit.

## 8. Seed / demo data

### Shipments — JNPT auto-parts case study (`tests/fixtures/shipments.py`)
All to **INNSA** (Nhava Sheva/JNPT), HS heading 8708. **Fixed dates** (deterministic FX and
demurrage later).

| key | container | origin | ccy | value | docs | status | key dates |
|---|---|---|---|---|---|---|---|
| clean | MSKU7001001 | CNSHA | USD | 84,000 | invoice, packing list, BoL (qty 1200) | docked | quote 08-10 (14d), invoice 08-18, BoL 08-22, ETA 09-20 |
| qty_mismatch | MSCU7002002 | KRPUS | USD | 136,000 | invoice 800 vs packing list **760**, BoL 800 | docked | quote 08-12 (10d), invoice 08-20, BoL 08-25, ETA 09-22 |
| missing_document | CMAU7003003 | JPYOK | JPY | 18,500,000 | packing list, BoL — **no invoice** | in_transit | quote 09-01 (7d), BoL 09-10, ETA 10-05, invoice_date NULL |
| already_delayed | HLXU7004004 | DEHAM | EUR | 96,500 | all 3 consistent (qty 600) | **held** | quote 07-20 (14d), invoice 07-28, BoL 08-01, ETA/docked 09-08 |

(All dates 2026.)

### Demurrage tariffs (`tests/fixtures/port_rates.py`, all USD per container per day)
| port | type | 2026-01-01 → 2026-09-30 | from 2026-10-01 |
|---|---|---|---|
| INNSA | 20ft | 5 free, 45.00 | 4 free, 55.00 |
| INNSA | 40ft | 5 free, 90.00 | 4 free, 110.00 |
| INNSA | 40hc | 5 free, 100.00 | 4 free, 120.00 |
| INMUN | 20ft / 40ft | 7 free, 40.00 / 80.00 (open-ended) | — |

**Dev DB differs from the fixtures** because of the user's dashboard review edits (audited as
`ops-dashboard`): INNSA 40ft Oct period corrected to 6 free / 120.0; INNSA 20ft has a third
period from 2026-11-12 at 5 free / 150.0 (Oct period now ends 2026-11-11). Tests never use the
dev DB, so this is harmless; re-seeding will not revert it (by design).

## 9. Decisions and deviations from the original spec (already reflected in `docs/`)

- No Docker locally; native Postgres 18 (spec said 15 — 15+ accepted) + embedded Chroma.
- `cancelled` added to `ShipmentStatus` (edge cases required it).
- `audit.events` created in migration `0001` (conventions require Phase 1 writes be audited);
  `audit_service.query_events` is still Phase 12.
- `list_shipments(status=None)` implements the plan's "`list_by_status`".
- Separate test DB `customs_copilot_test` (`POSTGRES_TEST_DB`); pytest marker `db`.
- `scripts/` is a package — run as modules (`python -m scripts.<name>`); seeds import fixtures
  from `tests/fixtures/`. `python -m scripts.seed` is the single documented seed command.
- Demurrage: rate periods are non-overlapping and history-preserving (exclusion constraint;
  revisions close the previous period instead of overwriting). `demurrage_charge` added to the
  service (plan listed only `get_rate`/`upsert_rate`) so `free_days=0` is testable now and
  Phase 8 reuses it. `container_type` is TEXT validated by the `ContainerType` enum.
- mypy strict everywhere with `python_version` unset (numpy stubs need ≥3.12 syntax);
  dashboards/tests have relaxed `disallow_untyped_defs`.

## 10. Open decision — raise with the user before Phase 8

`demurrage_service.get_rate` needs a **container type**, but `shipping.shipments` (spec §3.1)
has no `container_type` column, so the Phase 8 risk agent cannot price a shipment. Proposed
(already suggested to the user, not yet approved): add `container_type TEXT NOT NULL DEFAULT
'40ft'` to shipments in a small migration at the start of Phase 8 (or earlier), set per
fixture (all 40ft unless the user specifies otherwise), and expose it in `ShipmentCreate/
Update/Record` and the Shipping Ops page.

## 11. Gotchas already hit (don't rediscover them)

- **ruff isort + local `alembic/` folder:** `known-third-party = ["alembic"]` in
  `pyproject.toml`; keep it.
- **Streamlit + mypy:** inline lists / `list(Enum)` passed to `st.selectbox` make mypy pick the
  `-> None` overload. Use a module-level annotated variable
  (`CONTAINER_TYPES: list[ContainerType] = list(ContainerType)`).
- **ruff RUF001/RUF003** flag non-ASCII look-alikes (`×`, `➕`) in strings/comments — use ASCII.
- SQLAlchemy `__table_args__` must be a tuple (`({"schema": "x"},)`), not a dict (RUF012).
- Pydantic class-level constants need `ClassVar[...]`.
- `ExcludeConstraint` elements may be `text("daterange(a, b, '[]')")`; give it an explicit
  name (`ex_<table>_...`) — the naming convention has no `ex` key. `alembic check` accepts it.
- Flush the closing of an old period **before** inserting the new one, or the exclusion
  constraint fires mid-flush.
- Python `Path.write_text` on Windows writes CRLF; `.gitattributes` (`* text=auto eol=lf`)
  normalises in the repo — CRLF warnings on `git add` are harmless. Prefer
  `write_text(..., newline="\n")` in helper scripts.
- `Settings()` reads `.env` from the project root; tests constructing `Settings` pass
  `_env_file=None` and clear env vars.
- Forgotten Postgres superuser password: reset by temporarily switching the localhost lines in
  `C:\Program Files\PostgreSQL\18\data\pg_hba.conf` to `trust` (admin PowerShell), restart the
  service, `ALTER ROLE postgres PASSWORD ...`, restore the file, restart. A
  `pg_hba.conf.bak-*` from the last reset may remain in that folder.

## 12. Next: Phase 3 — Currency & Exchange Rate Service

Read `docs/architecture.md` §3.4–3.6 and §8, `implementation_plan.md` Phase 3,
`edge_cases.md` Phase 3 first. Create branch `phase-3-fx-service` from `main`.

- **Deliverables:** `models/db_models.py::SpotRate, LockedRate, GazettedRate` (schema `fx`);
  migration `0003_fx`; `services/fx_service.py::resolve_rate` implementing all four stages;
  `dashboards/pages/3_FX_Rates.py` with three editable tables.
- **Tables (arch §3.4–3.6):** `fx.spot_rates` (pair, rate NUMERIC(12,6), as_of TIMESTAMPTZ,
  source); `fx.locked_rates` (pair, rate, lock_date DATE, lock_type ENUM('bol',
  'commercial_invoice') — **immutable once written**); `fx.gazetted_rates` (pair, rate,
  period_start/period_end DATE, publishing_authority, `revision_of` FK → a mid-period
  correction that must be preferred over the original).
- **Four stages (arch §8):** quotation → latest spot with `as_of <= date`, returns
  `expires = date + quote_validity_days` (7–14, caller-supplied), `locked=False`;
  bol → `locked_rates` exact `lock_date`, `lock_type='bol'`; invoice → commercial invoice uses
  the rate locked to its own invoice date, **freight invoice re-uses the BoL-date rate**
  (caller passes `bol_date`) — so the API needs an invoice-kind parameter;
  port_charges → gazetted period containing the date, preferring a `revision_of` row.
  Every miss → `RateNotFoundError`, never a nearest-date fallback.
- **Exit criteria / edge cases:** parametrized tests over all four stages; BoL date resolves to
  its exact locked rate (not spot); port-charge date resolves to the gazetted period even when
  that day's spot differs; revision preferred; Saturday with no locked row → raises; date before
  the earliest locked row → raises (not earliest available); commercial vs freight invoice
  straddling a rate change → two different correct answers asserted separately; quotation with
  7-day validity looked up 10 days later → `expires` in the past, and the *caller* surfaces
  "quote expired" (assert on the caller too, e.g. a dashboard/helper function).
- **Seed data needed** (pairs USD/INR, EUR/INR, JPY/INR — fixture currencies): spot rates around
  the quote dates (07-20, 08-10, 08-12, 09-01); locked `bol` rates on 08-01, 08-22, 08-25,
  09-10; locked `commercial_invoice` rates on 07-28, 08-18, 08-20; gazetted periods covering the
  port-charge window (Aug–Oct 2026; India's CBIC notifies customs exchange rates roughly twice a
  month — fortnightly periods are realistic) with at least one `revision_of` correction.
- Follow §6: non-overlapping gazetted periods for originals (consider an exclusion constraint
  that ignores revision rows), audit every write, typed errors, idempotent seed registered in
  `scripts/seed.py`, dashboard tests via AppTest, then the full quality gate.
