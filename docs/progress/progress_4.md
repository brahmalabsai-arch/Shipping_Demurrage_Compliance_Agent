# Progress record — after Phase 4 (Compliance Rulebook + Customs Bulletin)

> **How to use this file:** attach it as the only context in a new chat to continue the build.
> It supersedes `progress_3.md`. The authoritative specs are in `docs/`
> (`problem_statement.md`, `architecture.md`, `implementation_plan.md`, `conventions.md`,
> `edge_cases.md`) — read the relevant phase there before starting work.

- **Last updated:** 2026-09-28
- **Phases complete:** 0, 1, 2, 3, 4 (of 0–13)
- **Next up:** Phase 5 — MCP Server Layer (started 2026-09-28)
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
5. Conventional commits (`feat(mcp): ...`, `docs: ...`); branch names
   `phase-<N>-<slug>` (Phase 5: `phase-5-mcp-servers`).
6. The user works on **Windows 11 / PowerShell** in VS Code. PowerShell 5.1: no `&&`; pass
   multi-line commit messages via `git commit -F <file>` (write the message to a temp file).
   **Never round-trip a UTF-8 source file through PowerShell 5.1 `Get-Content`/`Set-Content`**
   — it reads it as ANSI and mangles non-ASCII (e.g. `→`). Edit with the Edit tool or Python
   (`write_text(..., encoding="utf-8", newline="\n")`).
7. `docs/simple_docs/` (user's plain-language docs) is git-ignored and must never be committed
   or edited by the assistant.
8. Resolve spec gaps with a sensible, documented choice and tell the user; ask only when the
   decision genuinely changes the data model or scope (as in §10).

## 3. Repository state

- Branches: `main` (= tag `phase-4-complete`), `phase-0-foundations`,
  `phase-1-shipping-records`, `phase-2-demurrage`, `phase-3-fx-service`, `phase-4-compliance`;
  `phase-5-mcp-servers` is created from `main` right after this record.
- Tags: `phase-0-complete` … `phase-4-complete`.
- **No git remote yet** — nothing pushed; CI (`.github/workflows/ci.yml`) runs on first push.
- `.env` (git-ignored) holds a generated `POSTGRES_PASSWORD` for role `customs`; other keys
  empty → defaults in `config/settings.py`. The Postgres **superuser** (`postgres`) password is
  known only to the user (needed only for `scripts.setup_local_db`) and never stored in `.env`.
- Databases (both owned by `customs`, `timezone=UTC`):
  - `customs_copilot` (dev) — migrated to `0004`; 4 fixture shipments; 9 tariff periods
    (8 seeded + the user's Phase 2 review edits, see §8); FX exactly as seeded (24 spot,
    7 locked, 26 gazetted); 6 approved v1 compliance rules (seed); the published bulletin
    carries the user's Phase 4 review edit (see §8); **0 snapshots, 0 change_log rows** (no
    scan has ever run); 118 audit events.
  - `customs_copilot_test` — disposable; pytest wipes and rebuilds it every run.
- Streamlit is **not** running (stopped after the Phase 4 review).

## 4. Commands

```powershell
# one-time (already done on this machine) — prompts for the postgres superuser password
.\.venv\Scripts\python.exe -m scripts.setup_local_db   # idempotent: role + app DB + test DB

.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m scripts.seed              # every domain seed; idempotent
.\.venv\Scripts\streamlit.exe run dashboards/Home.py    # http://localhost:8501
#   pages: /Shipping_Ops, /Demurrage, /FX_Rates, /Compliance, /Customs_Bulletin  (for review, run headless in the background:
#   streamlit run dashboards/Home.py --server.headless true --server.port 8501)

# quality gate (all must pass before a phase is "done")
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\black.exe --check .
.\.venv\Scripts\mypy.exe .            # strict
.\.venv\Scripts\pytest.exe            # 214 tests, ~10 s
.\.venv\Scripts\alembic.exe check     # models == migrations

# new migration for phase N (conventions: <phase-seq>_<slug>.py) — hand-write, then check
.\.venv\Scripts\alembic.exe revision --rev-id 0005 -m "<slug>"   # Phase 5 needs none
```

## 5. What exists (as built)

```
config/settings.py        Settings (pydantic-settings, env_ignore_empty); database_url,
                          test_database_url (psycopg v3 URLs built from POSTGRES_* keys)
models/enums.py           ShipmentStatus (incl. 'cancelled'), ContainerType, DocType,
                          FxStage, LockType, InvoiceKind, RateSource, RuleStatus,
                          RuleDecision, SectionChangeKind (StrEnum)
models/db_models.py       Base (naming convention), utcnow(), _pg_enum(), _uuid_pk();
                          Shipment, Document (schema shipping); PortRate (schema demurrage);
                          SpotRate, LockedRate, GazettedRate (schema fx);
                          BulletinPublication, BulletinSnapshot, ChangeLogEntry (schema
                          bulletin); ComplianceRule (schema compliance);
                          AuditEvent (schema audit); DOMAIN_SCHEMAS tuple
models/schemas.py         ShipmentCreate/Update/Record, DocumentCreate/Amend/Record,
                          PortRateUpsert/Record, DemurrageCharge, SpotRateUpsert/Record,
                          LockedRateCreate/Record, GazettedRateCreate/Record, ResolvedRate,
                          RuleProposal, ComplianceRuleRecord (.applies_to(hs_code)),
                          BulletinPublicationRecord, BulletinSnapshotRecord,
                          ChangeLogEntryRecord, BulletinSection, SectionChange, RuleKey;
                          annotated types CurrencyCode, ContainerId, HsCode, PortCode,
                          NonNegativeDecimal, CurrencyPair ('USD/INR'), FxRateValue
                          (> 0, fits NUMERIC(12,6)), ...
services/db.py            get_engine(), get_session_factory(), session_scope()
services/errors.py        CustomsCopilotError root + Phase 1–4 errors
services/audit_service.py record(session, actor, action, entity_type, entity_id, payload)
services/shipping_service.py, demurrage_service.py, fx_service.py, compliance_service.py,
                          bulletin_service.py   (APIs in §6)
alembic/env.py            URL from Settings; accepts config.attributes["connection"] (tests)
alembic/versions/0001_shipping.py   shipping.shipments, shipping.documents, audit.events,
                          enums shipping.shipment_status / shipping.doc_type
alembic/versions/0002_demurrage.py  btree_gist extension, demurrage.port_rates
alembic/versions/0003_fx.py         fx.spot_rates, fx.locked_rates, fx.gazetted_rates, enum
                          fx.lock_type, function fx.reject_mutation() + immutability triggers
alembic/versions/0004_compliance.py bulletin.publication/snapshots/change_log, compliance.rules,
                          enum compliance.rule_status, partial unique index
                          uq_rules_current_approved, triggers compliance.guard_rule(),
                          bulletin.reject_mutation(), bulletin.guard_change_log()
dashboards/Home.py, dashboards/ui.py (IST helpers, run_action, flash, to_decimal),
dashboards/pages/1_Shipping_Ops.py, 2_Demurrage.py, 3_FX_Rates.py (tabs: Resolve, Spot,
                          Locked, Gazetted), 4_Compliance.py (tabs: Pending review, Rulebook
                          in force, All versions, Propose), 5_Customs_Bulletin.py (tabs:
                          Published, Edit, Scans & changes)
scripts/__init__.py, setup_local_db.py + .sql, seed.py (runs all), seed_shipments.py,
                          seed_port_rates.py, seed_fx_rates.py, seed_compliance.py
tests/conftest.py         DB harness (see §7)
tests/fixtures/shipments.py (4 seed shipments + make_shipment), port_rates.py, fx_rates.py,
                          compliance.py (BASELINE_BULLETIN, BASELINE_RULE_KEYS, edit_section)
tests/test_foundations.py, test_infrastructure.py,
tests/test_services/test_shipping_service.py, test_demurrage_service.py, test_fx_service.py,
                          test_compliance_service.py (incl. the AST governance scan),
                          test_bulletin_service.py
tests/test_scripts/test_seed_shipments.py, test_seed_port_rates.py, test_seed_fx_rates.py,
                          test_seed_compliance.py
tests/test_dashboards/test_shipping_ops_page.py, test_demurrage_page.py, test_fx_rates_page.py,
                          test_compliance_page.py, test_customs_bulletin_page.py
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
  (`shipment.created`, `document.amended`, `port_rate.superseded`, `gazetted_rate.revised`,
  ...). A no-op change writes no event. Payload may hold Decimal/date/UUID/enum — `record`
  normalises to JSON.
- **ORM rows never leave services:** return `XRecord.model_validate(row)` (Pydantic,
  `from_attributes=True`, frozen). Inputs are `XCreate` / `XUpdate` / `XUpsert`
  (`extra="forbid"`); updates apply `model_dump(exclude_unset=True)` and validate the *merged*
  state before mutating.
- **Typed errors** in `services/errors.py`, one per failure mode, subclass `CustomsCopilotError`
  — never a bare `ValueError` for a domain failure. Unique/constraint violations: write inside
  `session.begin_nested()`, catch `IntegrityError`, match the constraint name in
  `str(exc.orig)`, raise the typed error (session stays usable).
- **Lookups are deterministic: raise, don't guess.** No nearest-date / latest-row fallbacks.
- **No deletes.** History via status / version / `superseded_by` / closing `effective_to` /
  `revision_of`. Where a table is spec'd immutable/append-only, enforce it with a
  `BEFORE UPDATE OR DELETE` trigger raising SQLSTATE `restrict_violation` (→ SQLAlchemy
  `IntegrityError`), as `fx.reject_mutation()` does; drop the function in the downgrade.
- **Integrity in the database too:** CHECK constraints for non-negative/positive values and
  valid periods; GiST exclusion constraints (`btree_gist` installed) for non-overlapping date
  periods — partial ones via `ExcludeConstraint(..., where=text("..."))`. Multi-column unique
  constraints get an explicit name (`uq_<table>_<col>_<col>`) because the naming convention
  only uses column 0. Tests prove the DB rejects bad rows even when the service is bypassed.
- **Money = `Numeric` + `CHAR(3)` currency; lifecycle dates = `Date`; timestamps =
  `DateTime(timezone=True)` in UTC** (`default=utcnow, server_default=func.now()`). Where a
  business "calendar day" is derived from a timestamp, it is the **IST** day.
- **Postgres ENUMs** via `_pg_enum(EnumCls, name, schema)` (stores lowercase `.value`); where
  the spec says TEXT (e.g. `container_type`), keep TEXT and validate with a StrEnum in Pydantic.
- **Migrations are hand-written** from the models, then verified with `alembic check`
  ("No new upgrade operations detected"). Each creates its schema (`CREATE SCHEMA IF NOT
  EXISTS`); downgrade drops tables, functions, enum types and the schema.
- **Seeds:** fixture data lives in `tests/fixtures/<domain>.py`; `scripts/seed_<domain>.py`
  creates via the service layer, skips anything already present (never overwrites dashboard
  edits), one transaction per item; register it in `scripts/seed.py`. Rows that reference
  other seeded rows (e.g. a gazetted revision) name the parent by a natural key and resolve
  its id at seed time.
- **Governance by structure:** when only one function may perform a transition (e.g.
  `compliance_service.decide`), give the input model no field for it, make that function the
  only assignment site, guard it with a DB trigger, and add a code-scan test (AST visitor) that
  fails if any other function writes it or any other module imports the ORM class.
- **Dashboards:** every widget has an explicit `key` (suffix with the entity id for per-entity
  forms); writes go through `dashboards.ui.run_action(lambda session: ..., "success msg")`;
  reads that may fail show `st.error(f"{type(exc).__name__}: {exc}")`; timestamps via
  `fmt_ist`; option lists passed to `st.selectbox` must be module-level annotated variables
  (see §11). Inputs that change which fields a form shows live **outside** the `st.form`
  (forms don't rerun on change). `st.tabs` is fine — AppTest sees widgets in every tab. No
  `st.cache_*` on service reads (edits must be visible immediately). Leave `min_value` off
  numeric inputs whose validation should be demonstrated by Pydantic/the service.

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

### FX service API (Phase 3) — `services/fx_service.py`
- `resolve_rate(session, pair, as_of, stage, *, quote_validity_days=None, invoice_kind=None,
  bol_date=None) -> ResolvedRate`. `ResolvedRate`: pair, stage, as_of, rate, source
  (`spot`/`locked`/`gazetted`), source_id (the fx row used), basis (human-readable), locked,
  expires, invoice_kind. Rules:
  - quotation → `get_spot_rate`: latest spot with `as_of` before the end of that **IST**
    calendar day; `expires = as_of + quote_validity_days` (required, 7–14); `locked=False`.
  - bol → exact `locked_rates` row (`lock_type='bol'`, `lock_date=as_of`).
  - invoice → `invoice_kind` required. commercial → `commercial_invoice` lock on `as_of`;
    freight → `bol` lock on `bol_date` (required for freight only).
  - port_charges → `get_gazetted_rate`: the original covering the date, then down the
    revision chain to the deepest row covering it.
  - Missing or irrelevant stage context → `InvalidRateRequestError`; no rate →
    `RateNotFoundError` (never a nearest date). `locked=True` only for locked-table rates.
- `is_quote_expired(resolved, on)` — valid **through** `expires`; raises
  `InvalidRateRequestError` for non-quotation rates. Callers (dashboard now, Phase 7 agent
  later) use it to surface "quote expired".
- Writes (all audited): `upsert_spot_rate` (same pair + as_of → in-place correction
  `spot_rate.updated`, identical re-entry writes nothing; else `spot_rate.created`);
  `lock_rate` (`locked_rate.created`; duplicate pair+type+date → `RateAlreadyLockedError`);
  `publish_gazetted_rate` (`gazetted_rate.created` / `gazetted_rate.revised`; overlap →
  `OverlappingGazettedPeriodError`; revision for another pair or outside the parent period →
  `InvalidGazettedRevisionError`; unknown parent → `GazettedRateNotFoundError`).
- Reads: `get_spot_rate`, `get_locked_rate`, `get_gazetted_rate`, `list_spot_rates`,
  `list_locked_rates`, `list_gazetted_rates` (original listed before its revisions),
  `list_pairs`.

### Compliance service API (Phase 4) — `services/compliance_service.py`
- `propose_change(session, RuleProposal, actor) -> ComplianceRuleRecord` — always `pending`,
  `version = max + 1` per `rule_key`; identical (content, hs_codes, effective_date) to the
  version in force or to a pending one → `DuplicateProposalError`; unknown `source_diff_id`
  → `ChangeLogEntryNotFoundError`. Audit `compliance_rule.proposed`.
  `RuleProposal`: rule_key, content, hs_codes (list, `[]` = all goods; sorted/deduped),
  effective_date, change_summary?, source_diff_id?. **No status field.**
- `decide(session, rule_id, decision: RuleDecision | "approve"/"reject", reviewer)` — the ONLY
  status writer. Blank reviewer / bad decision → `InvalidDecisionError`; not pending →
  `RuleAlreadyDecidedError`; unknown → `RuleNotFoundError`. Approve: the version in force gets
  `superseded_by` (flushed first, because of the partial unique index), audit
  `compliance_rule.superseded` + `compliance_rule.approved`; approving an older version than
  the one in force → `StaleProposalError`. Reject: final, audit `compliance_rule.rejected`.
  Audit actor = reviewer. A comment marks where Phase 9 embeds approved rules into Chroma.
- Reads: `get_rule`, `get_current_rule(rule_key)` (the version in force, else
  `RuleNotFoundError`), `list_approved(hs_code=None)` (versions in force; HS match = either
  code is a prefix of the other, empty hs_codes = all), `list_pending()` (oldest first),
  `list_rules(rule_key=None, status=None)`.

### Bulletin service API (Phase 4) — `services/bulletin_service.py`
- Format: free text, then sections `## <rule_key>` / `Effective: YYYY-MM-DD` /
  `HS codes: 8708, 870830 | all` / rule text. `parse_sections(content)` →
  `list[BulletinSection]` or `InvalidBulletinError` (no sections, duplicate key, bad/missing
  metadata, empty text). `normalize_text` = collapse whitespace + casefold.
- `publish(session, content, actor)` — validates, CRLF→LF, single row upsert; identical →
  no-op; audit `bulletin.published` (section keys + sha256s). **Never captures a snapshot.**
  `get_publication` (`BulletinNotPublishedError` if none).
- Scan path (Phase 6 scheduler only): `capture_snapshot(session, actor)` (audit
  `bulletin_snapshot.captured`); `diff_since_last(session, actor) -> ChangeLogEntryRecord |
  None` — the two most recent snapshots by `seq`; `None` if <2, identical, or that pair is
  already logged; else logs one entry (audit `bulletin_change.detected`). `is_substantive` =
  some section changed beyond whitespace/case (dates/codes compared parsed) or the text
  outside sections did. `diff_summary` e.g. `"Substantive change.\n- HSN-8708-duty: modified"`.
- `section_changes(session, change_id) -> list[SectionChange]` (rule_key, kind
  added/modified/removed, before/after `BulletinSection`, is_substantive) — the watch agent
  proposes one change per substantive section. Also `get_latest_snapshot`
  (`BulletinSnapshotNotFoundError`), `list_snapshots(limit)`, `get_change`, `list_changes`,
  `change_texts(change_id)`, `diff_bulletins(old, new)`, `unified_diff(old, new)`.
- Not built yet (Phase 6): setting `change_log.emailed_at` (the DB allows it once).

## 7. Test harness

- DB tests use `POSTGRES_TEST_DB`. Session fixture `db_engine`: drops all `DOMAIN_SCHEMAS` +
  `alembic_version`, then upgrade head → downgrade base → upgrade head (proves reversibility).
- Per test: one outer transaction rolled back afterwards. `session_factory` binds sessions with
  `join_transaction_mode="create_savepoint"` **and monkeypatches
  `services.db.get_session_factory`**, so code using `session_scope()` (dashboards, scripts)
  writes into the same rolled-back transaction. `db_session` = a session from that factory.
  Tests needing the full fixture data call the domain's `seed()` (e.g. a `seeded` fixture
  calling `seed_fx_rates.seed()`).
- Tests using `db_engine`/`db_connection`/`session_factory`/`db_session` are auto-marked `db`.
  Local `pytest` runs everything; CI splits `-m "not db"` / `-m db`.
- Dashboard tests: `streamlit.testing.v1.AppTest.from_file(..., default_timeout=60)`; form
  submit buttons addressed by `key`; `at.error`, `at.success`, `at.warning`, `at.info`,
  `at.metric` for assertions. After changing a widget outside a form that alters the form
  (e.g. a stage selector), call `at.run()` before filling the form.
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

**Dev DB differs from these fixtures** because of the user's Phase 2 dashboard review edits
(audited as `ops-dashboard`): INNSA 40ft Oct period corrected to 6 free / 120.0; INNSA 20ft has
a third period from 2026-11-12 at 5 free / 150.0 (Oct period now ends 2026-11-11). Tests never
use the dev DB, so this is harmless; re-seeding will not revert it (by design).

### FX rates (`tests/fixtures/fx_rates.py`; pairs `USD/INR`, `EUR/INR`, `JPY/INR` = INR per 1 unit)
- **Spot** (source `simulated-spot-feed`, 13:30 IST = 08:00 UTC) for every pair on 07-20,
  08-10, 08-12, 09-01, 09-08, 09-20, 09-22, 10-05. USD: 87.4200, 87.6150, 87.7300, 88.0400,
  88.1900, 88.3550, 88.4100, 88.6200. EUR: 101.8500 … 103.8800. JPY: 0.5921 … 0.6034.
- **Locked:** commercial_invoice EUR 07-28 101.9700, USD 08-18 87.7050, USD 08-20 87.7800;
  bol EUR 08-01 102.0500, USD 08-22 87.8600, USD 08-25 87.9400, JPY 09-10 0.6004. (No JPY
  invoice lock — the missing-document shipment has no invoice.)
- **Gazetted** (`CBIC (simulated)`): fortnightly periods 07-01 … 10-31 (1–15, 16–end) for each
  pair, e.g. USD 09-01..09-15 88.05, 09-16..09-30 88.30, 10-01..10-15 88.55; EUR
  09-01..09-15 102.95; JPY 10-01..10-15 0.6030. Two revisions:
  - **EUR 09-01..09-15 → 103.05 (whole period)** — the already-delayed shipment (docked 09-08)
    must be converted at 103.05 in Phase 8.
  - **USD 09-21..09-30 → 88.40**, revising 09-16..09-30 — clean (arrives 09-20) gets 88.30,
    qty_mismatch (arrives 09-22) gets 88.40.

### Customs bulletin + compliance rulebook (`tests/fixtures/compliance.py`)
Baseline bulletin (SIMULATED, INNSA import desk) with six sections, each seeded as an approved
v1 rule (`decided_by='seed_script'` — the only way to `approved` is `decide`):
`HSN-8708-duty` (BCD 15%, SWS 10% of BCD, IGST 28%; HS 8708; eff. 2026-04-01),
`HSN-870830-brake-certification` (certificate of conformity for brakes; HS 870830),
`DOC-quantity-consistency` (invoice/packing list/BoL quantities must agree; all),
`DOC-commercial-invoice-required` (no assessment without the invoice; all),
`VAL-exchange-rate` (convert at the notified rate for the bill-of-entry period; all),
`ORG-KR-CEPA` (Korea CEPA preference needs a certificate of origin; HS 8708).
**Dev DB:** the user's Phase 4 review republished the bulletin with `HSN-8708-duty` changed to
**20%, effective 2026-10-01** (rules unchanged, still v1 at 15%). No scan has run, so the
first Phase 6 scan will capture this as its baseline (no change detected against nothing).

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
- FX (Phase 3): locked **and gazetted** rates are append-only via DB trigger (spec only said
  locked). Gazetted corrections are `revision_of` rows — same pair, inside the revised period,
  may be partial, may themselves be revised (deepest wins); originals never overlap and
  revisions of one row never overlap (partial exclusion constraints). Spot rows are
  correctable in place. Quote day = IST calendar day; a quote is valid through `expires`.
  `resolve_rate` takes keyword stage context and names its date parameter `as_of` (the Phase 5
  MCP tool may expose it as `date`). Reflected in `architecture.md` §3.4–3.6 and §8.
- Compliance/bulletin (Phase 4): the live bulletin is a new single-row table
  `bulletin.publication` (spec had only snapshots); snapshots gained `seq` (identity) for a
  reliable order; `compliance.rules` gained `change_summary`; `propose_change` takes
  `rule_key` and `effective_date` (spec signature omitted them) and `change_summary` instead
  of `diff_summary`; the bulletin has a sectioned text format so changes are attributable per
  rule; lifecycle and immutability enforced by triggers; `StaleProposalError` and
  `DuplicateProposalError` added. Reflected in `architecture.md` §3.7–3.8 and §5.
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
  (`CONTAINER_TYPES: list[ContainerType] = list(ContainerType)`). Variables assigned in
  branches of a page (e.g. optional form fields) need explicit `X | None` annotations.
- **ruff RUF001/RUF002/RUF003** flag non-ASCII look-alikes (`×`, `➕`, mojibake) in
  strings/docstrings/comments — use ASCII. (`→` and `—` are accepted.)
- **ruff RUF043:** a `pytest.raises(match=...)` pattern containing `.` must be a raw, escaped
  regex (`match=r"87\.86"`).
- SQLAlchemy `__table_args__` must be a tuple (`({"schema": "x"},)`), not a dict (RUF012).
- Pydantic class-level constants need `ClassVar[...]`.
- `ExcludeConstraint` elements may be `text("daterange(a, b, '[]')")`; give it an explicit
  name (`ex_<table>_...`) — the naming convention has no `ex` key. `alembic check` accepts it,
  including with `where=`.
- Flush the closing of an old period **before** inserting the new one, or the exclusion
  constraint fires mid-flush.
- Values read back from `NUMERIC(12,6)` carry six decimals (`Decimal("87.860000")`); audit
  diffs and error messages show that form.
- Streamlit `number_input(..., format="%.6f")` returns a float; convert via
  `dashboards.ui.to_decimal` (string form, no binary noise).
- Python `Path.write_text` on Windows writes CRLF; `.gitattributes` (`* text=auto eol=lf`)
  normalises in the repo — CRLF warnings on `git add` are harmless. Prefer
  `write_text(..., newline="\n")` in helper scripts.
- ruff isort wants `from services import a` and `from services import b as c` on separate
  lines (`ruff check --fix` sorts it).
- `session.refresh(row)` after flush to read DB-generated values such as an identity `seq`.
- A partial unique index (`Index(..., unique=True, postgresql_where=text(...))`) passes
  `alembic check`; when moving the "current" flag between rows, flush the old row's change
  first.
- Bash heredocs containing many backticks/quotes can fail to parse in the Bash tool — write
  long helper scripts to the scratchpad with the Write tool and run them instead.
- `Settings()` reads `.env` from the project root; tests constructing `Settings` pass
  `_env_file=None` and clear env vars.
- Forgotten Postgres superuser password: reset by temporarily switching the localhost lines in
  `C:\Program Files\PostgreSQL\18\data\pg_hba.conf` to `trust` (admin PowerShell), restart the
  service, `ALTER ROLE postgres PASSWORD ...`, restore the file, restart. A
  `pg_hba.conf.bak-*` from the last reset may remain in that folder.

## 12. Next: Phase 5 — MCP Server Layer

Read `docs/architecture.md` §1 (MCP SDK, `langchain-mcp-adapters`), §5 (tool contracts),
`implementation_plan.md` Phase 5, `edge_cases.md` Phase 5, and `conventions.md` §2 (verb-first
tool names, no synonyms across servers) and §4 (log every tool call via `structlog`: tool,
arguments, result/exception, timing, correlated by `run_id`). Branch `phase-5-mcp-servers`.

- **Deliverables:** six servers under `mcp_servers/<name>/server.py` — `shipping_records_mcp`,
  `demurrage_rates_mcp`, `fx_rates_mcp`, `compliance_rulebook_mcp`, `customs_bulletin_mcp`,
  `notifications_mcp` — each a thin adapter over its service module (official `mcp` SDK).
- **Exit criterion:** a reference client (pytest harness using the SDK's client session) calls
  every tool on every server and the results match direct service-layer calls byte-for-byte on
  the same fixture data (proves no dashboard/MCP drift).
- **Edge cases:** DB connection dropping mid-call → typed MCP error response, server survives;
  malformed argument → schema-validation error before the service; read right after a direct
  service write → identical (no caching layer).
- **Known wrinkles to resolve (tell the user the choice):**
  - `compliance_rulebook_mcp.search_rules` needs the Phase 9 RAG retriever — decide whether to
    ship it now over `list_approved` (SQL, approved-only) and swap the retriever in later.
  - `notifications_mcp.send_review_email` needs `integrations/gmail_client.py` (Phase 6) —
    decide on an injectable sender now, real Gmail in Phase 6.
  - Write tools (`update_status`, `propose_change`, `decide`) need an `actor`; MCP handlers
    must open their own `session_scope()` per call (services only flush).
  - New deps (`mcp`, `structlog`) go into `pyproject.toml` and the venv only.
