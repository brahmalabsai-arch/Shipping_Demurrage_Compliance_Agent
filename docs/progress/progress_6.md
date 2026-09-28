# Progress record — after Phase 6 (Regulatory Watch Agent)

> **How to use this file:** attach it as the only context in a new chat to continue the build.
> It supersedes `progress_5.md`. The authoritative specs are in `docs/`
> (`problem_statement.md`, `architecture.md`, `implementation_plan.md`, `conventions.md`,
> `edge_cases.md`) — read the relevant phase there before starting work.

- **Last updated:** 2026-09-28
- **Phases complete:** 0, 1, 2, 3, 4, 5, 6 (of 0–13)
- **Next up:** Phase 7 — Document Verification Agent (not started; the user will begin it in a
  new chat). Branch `phase-7-document-verification` is **not** created yet — create it from
  `main` when work starts.
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
5. Conventional commits (`feat(verification): ...`, `docs: ...`); branch names
   `phase-<N>-<slug>` (Phase 7: `phase-7-document-verification`).
6. The user works on **Windows 11 / PowerShell** in VS Code. PowerShell 5.1: no `&&`; pass
   multi-line commit messages via `git commit -F <file>` (write the message to a temp file).
   **Never round-trip a UTF-8 source file through PowerShell 5.1 `Get-Content`/`Set-Content`**
   — it reads it as ANSI and mangles non-ASCII (e.g. `→`). Edit with the Edit tool or Python
   (`write_text(..., encoding="utf-8", newline="\n")`).
7. `docs/simple_docs/` (user's plain-language docs) is git-ignored and must never be committed
   or edited by the assistant.
8. Resolve spec gaps with a sensible, documented choice and tell the user; ask only when the
   decision genuinely changes the data model or scope (as in §10).
9. **LLM = Groq** (`LLM_PROVIDER=groq`, `GROQ_API_KEY` in `.env`): `openai/gpt-oss-120b` for
   reasoning, `openai/gpt-oss-20b` for summaries (user's choice). **Never Anthropic** (user's
   credits exhausted). OpenAI is wired (`LLM_PROVIDER=openai`, gpt-5.5 / gpt-5.4-mini) but the
   account had no credits — switch only if the user says so. An LLM is never on the critical
   path: every use has a deterministic fallback, and tests never call a real model.
10. **Real email is live:** `EMAIL_BACKEND=gmail`, OAuth token in `secrets/`, sender
    brahmalabs.ai@gmail.com, reviewer **4x2kms@gmail.com** (`REVIEWER_EMAIL`). Tests always use
    a per-test outbox (autouse fixture) — never send real mail from a test.

## 3. Repository state

- Branches: `main` (= tag `phase-6-complete`), `phase-0-foundations`,
  `phase-1-shipping-records`, `phase-2-demurrage`, `phase-3-fx-service`, `phase-4-compliance`,
  `phase-5-mcp-servers`, `phase-6-regulatory-watch`.
- Tags: `phase-0-complete` … `phase-6-complete`.
- **No git remote yet** — nothing pushed; CI (`.github/workflows/ci.yml`) runs on first push.
- `.env` (git-ignored) holds `POSTGRES_PASSWORD` (role `customs`), `REVIEWER_EMAIL`,
  `EMAIL_BACKEND=gmail`, `GROQ_API_KEY` and `OPENAI_API_KEY` (no credits); other keys empty →
  defaults in `config/settings.py`. `secrets/credentials.json` + `secrets/token.json` hold the
  Gmail OAuth client and token (git-ignored). Never print `.env` values. The Postgres **superuser** (`postgres`) password is
  known only to the user (needed only for `scripts.setup_local_db`) and never stored in `.env`.
- Databases (both owned by `customs`, `timezone=UTC`):
  - `customs_copilot` (dev) — migrated to **`0006`**; 4 fixture shipments; 9 tariff periods
    (8 seeded + the user's Phase 2 review edits, see §8); FX exactly as seeded. Compliance:
    the 5 other seeded rules at v1; **HSN-8708-duty** went through the user's live Phase 6
    tests — v1 15% (seed), v2 25% approved by "Rakesh Brahma", v3 30% **rejected**
    ("Rate changed mid day"), **v4 40% approved by "Rupin" = in force**. Bulletin published
    at 40%; 7 snapshots (20, 20, 25, 25, 30, 40, 40 %), 3 change_log rows (all emailed);
    real review emails were delivered via Gmail. Latest scan matches the rulebook (no drift).
  - `customs_copilot_test` — disposable; pytest wipes and rebuilds it every run.
- Streamlit is **not** running (stopped after the Phase 6 review). No MCP server or scheduler
  process is running (MCP servers start on demand per scan).

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
.\.venv\Scripts\pytest.exe            # 358 tests, ~40 s

# MCP servers (stdio) — six: shipping_records_mcp, demurrage_rates_mcp, fx_rates_mcp,
# compliance_rulebook_mcp, customs_bulletin_mcp, notifications_mcp
.\.venv\Scripts\python.exe -m mcp_servers.fx_rates_mcp.server
# interactive inspection (Node 22 is installed; downloads the inspector from npm):
# npx @modelcontextprotocol/inspector .\.venv\Scripts\python.exe -m mcp_servers.fx_rates_mcp.server

# Regulatory Watch (Phase 6) — also: Customs Bulletin page → Scans & changes → Run scheduled scan now
.\.venv\Scripts\python.exe -m scheduler.watch_scheduler --once   # one scan now, prints WatchState
.\.venv\Scripts\python.exe -m scheduler.watch_scheduler          # scan every WATCH_SCAN_INTERVAL_MINUTES
# Gmail (docs/setup_gmail.md). Testing-mode tokens expire after 7 days → re-run authorize.
.\.venv\Scripts\python.exe -m integrations.gmail_client authorize
.\.venv\Scripts\python.exe -m integrations.gmail_client send-test   # to REVIEWER_EMAIL
.\.venv\Scripts\alembic.exe check     # models == migrations

# new migration for phase N (conventions: <phase-seq>_<slug>.py) — hand-write, then check
.\.venv\Scripts\alembic.exe revision --rev-id 0007 -m "<slug>"   # 0006 = Phase 6; no 0005
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
                          RuleProposal, ComplianceRuleRecord (.applies_to(hs_code);
                          + decision_note), RuleMatch, RuleDrift, ReviewContext,
                          EmailReceipt, EmailAddress,
                          BulletinPublicationRecord, BulletinSnapshotRecord,
                          ChangeLogEntryRecord, BulletinSection, SectionChange, RuleKey;
                          annotated types CurrencyCode, ContainerId, HsCode, PortCode,
                          NonNegativeDecimal, CurrencyPair ('USD/INR'), FxRateValue
                          (> 0, fits NUMERIC(12,6)), ...
services/db.py            get_engine(), get_session_factory(), session_scope()
services/errors.py        CustomsCopilotError root (+ DatabaseUnavailableError) + Phase 1–6
                          errors (EmailDeliveryError, McpToolError(tool, error_type, message))
config/log_setup.py       configure_logging() — structlog JSON to stderr (servers, scheduler)
services/audit_service.py record(session, actor, action, entity_type, entity_id, payload)
services/shipping_service.py, demurrage_service.py, fx_service.py, compliance_service.py,
                          bulletin_service.py, notification_service.py   (APIs in §6)
integrations/email.py     EmailSender protocol, OutboxSender (.eml files), get_email_sender()
                          (EMAIL_BACKEND outbox | gmail)
integrations/gmail_client.py  GmailSender (gmail.send scope), load_credentials, authorize,
                          build_message; CLI authorize / send-test
agents/mcp_clients.py     open_toolbox(servers, actor, run_id, connector), McpToolbox.call,
                          stdio_connector (default), memory_connector (tests)
agents/llm.py             reasoning_model() / summary_model() — Groq or OpenAI per settings;
                          None without the provider's key
agents/state.py           WatchState, SectionSummary, WatchOutcome
agents/audit.py           record_run_event(actor, run_id, action, payload) — agent_run events
agents/regulatory_watch_agent.py  node regulatory_watch_agent, summarize, template_summary,
                          build_graph, run(deps, run_id), WatchDeps, SERVERS, AGENT
scheduler/watch_scheduler.py  run_scan(), scheduled_scan(), create_scheduler(), main()
mcp_servers/common.py     create_server(), run_tool(), caller_of(), configure_logging()
mcp_servers/<name>/server.py  six FastMCP servers (tool lists in §6), main() = stdio
alembic/env.py            URL from Settings; accepts config.attributes["connection"] (tests)
alembic/versions/0001_shipping.py   shipping.shipments, shipping.documents, audit.events,
                          enums shipping.shipment_status / shipping.doc_type
alembic/versions/0002_demurrage.py  btree_gist extension, demurrage.port_rates
alembic/versions/0003_fx.py         fx.spot_rates, fx.locked_rates, fx.gazetted_rates, enum
                          fx.lock_type, function fx.reject_mutation() + immutability triggers
alembic/versions/0006_decision_note.py  compliance.rules.decision_note + guard_rule() v2
                          (a rejection needs a reason; note immutable after the decision)
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
tests/test_services/test_notification_service.py; tests/test_integrations/test_email.py,
                          test_gmail_client.py (Google API mocked)
tests/test_agents/test_regulatory_watch_agent.py (exit criteria + edge cases + rulebook-drift
                          scenarios), test_mcp_clients.py (incl. a real stdio round trip),
                          test_llm.py
tests/test_mcp_servers/conftest.py (connect, wire, assert_parity, assert_tool_error, direct,
                          seeded), test_<server>.py x6 (each declares TOOLS_TESTED),
                          test_common.py (coverage, naming, DB drop, malformed args, no cache,
                          logging, stdio launch)
.github/workflows/ci.yml  job `checks` (py3.11+3.13: ruff, black, mypy, pytest -m "not db");
                          job `db` (compose Postgres: alembic upgrade+check, pytest -m db)
```
Empty packages awaiting later phases: `rag/`. (`agents/` has only the watch agent so far.)

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
- **Writes return what the DB stored:** if a column normalises the value (e.g.
  `NUMERIC(12,6)` turns `88.5` into `88.500000`), `session.refresh(row)` before building the
  record, so a write result is byte-identical to a later read (FX writes do this).
- **MCP tools** (Phase 5): one `@mcp.tool()` async function per operation, body =
  `await run_tool(ctx, "<name>", {args}, lambda s, c: svc.fn(s, ..., c.actor))`; inputs reuse
  the Pydantic `*Create/*Update/*Upsert` models; every tool has a typed return (structured
  output). Callers pass `_meta={"actor": ..., "run_id": ...}` on `call_tool`. New tools need a
  parity test and an entry in their test module's `TOOLS_TESTED`.
- **Agents** (Phase 6): reach data **only through MCP** via
  `open_toolbox(<the role's servers>, actor=<agent name>, run_id=...)` — the toolbox refuses
  other servers (`ServerNotAllowedError`) and passes actor/run_id as `_meta`, so the servers
  audit writes under the agent's name. Tool errors arrive as `McpToolError.error_type`
  (e.g. `DuplicateProposalError`). Control flow for guarantees is deterministic; the LLM only
  produces text, behind a guard + deterministic fallback. Agent-level outcomes/failures are
  `agent_run` audit events (`agents/audit.record_run_event`, own transaction). One exported
  LangGraph node per agent module; its state slice documented in the module docstring.
- **Tests and external services:** `tests/conftest.py::_no_external_services` (autouse) clears
  `openai_api_key`/`groq_api_key` and forces the outbox backend into a per-test dir. Agent
  tests run MCP servers in-memory (`memory_connector`) against the rolled-back test DB and
  inject fakes (`FakeListChatModel`, a recording sender via
  `monkeypatch.setattr(integrations.email, "get_email_sender", ...)`).
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

### Demurrage / compliance additions (Phase 5)
- `demurrage_service.charge_for_stay(session, port_code, container_type, as_of, days_in_port)`
  = `demurrage_charge(get_rate(...), days)`.
- `compliance_service.search_rules(session, query, hs_code=None, limit=5) -> list[RuleMatch]`
  — interim keyword scorer (share of query terms, stopwords dropped) over `list_approved`
  only; `RuleMatch(rule, score, method="keyword")`. Phase 9 replaces the scoring behind this
  same function.

### Notification service (Phase 5) — `services/notification_service.py`
- `compose_review_body(rule, diff_summary) -> str`;
  `send_review_email(session, *, to, subject, diff_summary, rule_id, sender, actor) ->
  EmailReceipt` — `RuleNotFoundError` before sending; any backend exception →
  `EmailDeliveryError` and **nothing audited**; success audits
  `notification.review_email_sent` (entity = the rule). The sender is injected:
  `integrations.email.get_email_sender()` picks it from `EMAIL_BACKEND` (`outbox` default →
  `.eml` in git-ignored `OUTBOX_PATH`; `gmail` currently raises "wired in Phase 6").

### MCP servers (Phase 5) — 43 tools (see `architecture.md` §5 for signatures)
- shipping_records_mcp: list_shipments, get_shipment, get_shipment_by_container,
  get_active_shipment, get_documents, create_shipment, update_shipment, update_status,
  create_document, update_document.
- demurrage_rates_mcp: get_rate, get_demurrage_charge, list_rates, list_ports, update_rate.
- fx_rates_mcp: resolve_rate, get_spot_rate, get_locked_rate, get_gazetted_rate, list_pairs,
  list_spot_rates, list_locked_rates, list_gazetted_rates, update_spot_rate,
  create_locked_rate, create_gazetted_rate.
- compliance_rulebook_mcp: search_rules, list_approved, list_pending, list_rules, get_rule,
  get_current_rule, propose_change, decide.
- customs_bulletin_mcp: get_current_snapshot, diff_since_last_scan, get_section_changes,
  get_change, list_changes, list_snapshots, get_publication, update_publication (publisher
  side — do not give these two to the watch agent). No capture tool (Option A).
- notifications_mcp: send_review_email.
- Errors: tool error text contains `"<ErrorType>: message"` (FastMCP prefixes "Error executing
  tool <name>: "); `DatabaseUnavailableError` for connection failures; `UnexpectedError` hides
  internals. Validation errors contain "validation error".
- Structured output: model → its JSON object; list / optional / union → `{"result": ...}`.

### Compliance additions (Phase 6)
- `decide(session, rule_id, decision, reviewer, note=None)` — a rejection needs a non-blank
  `note` (`InvalidDecisionError`; also enforced by the trigger); stored as `decision_note`,
  immutable afterwards, in the audit payload as `note`. (Checked after the not-pending check,
  so deciding an already-decided rule still raises `RuleAlreadyDecidedError` first.)
- `review_context(session, rule_id) -> ReviewContext(proposal, in_force, others, history)` —
  what a reviewer judges against: the approved version in force plus every other non-approved
  version since it; `history` = plain lines ("v3 (proposed …) was rejected by X on …:
  "reason"", "v2 … is still pending and is superseded by this proposal — reject v2 as
  superseded.", "A newer proposal, v5 …"). IST dates. Used by the Compliance page and email.
- `bulletin_drift(session) -> list[RuleDrift(rule_key, section, in_force, pending_same,
  rejected_same)]` — rules where the **latest scan** differs from the rule in force (text
  compared with `normalize_text`, plus effective date and HS codes); raises
  `BulletinSnapshotNotFoundError` before the first scan.
- MCP (compliance_rulebook_mcp, now 10 tools): `decide(..., note?)`,
  `get_review_context(rule_id)`, `list_bulletin_drift()`.

### Bulletin / notification additions (Phase 6)
- `bulletin_service.mark_emailed(session, change_id, actor)` (first email only; audit
  `bulletin_change.emailed`), `get_latest_sections(session)`,
  `unified_diff(old, new, old_label="previous scan", new_label="latest scan")`.
- `notification_service.compose_review_body(context: ReviewContext, diff_summary)`; the email
  shows the change **against the rule in force**, the history, the proposed text and the text
  in force; `send_review_email` stamps the source change `emailed_at`.

### Regulatory watch (Phase 6) — `agents/regulatory_watch_agent.py`, `scheduler/watch_scheduler.py`
- `run_scan(*, connector=None, model_factory=None, reviewer_email=None, run_id=None) ->
  WatchState | None` — capture a snapshot (the ONLY capture site; `None` if nothing
  published), then run the agent over stdio MCP servers (defaults looked up at call time so
  tests/dashboard can swap them).
- Agent flow: `diff_since_last_scan` (detection record; its `get_section_changes` links
  proposals via `source_diff_id` and flags removed rules as
  `regulatory_watch.removed_sections_flagged`) → `list_bulletin_drift` → per drift: skip if
  `pending_same` ("already pending as vN") or `rejected_same` ("rejected before as vN by X
  (reason)"), else summary **vs the rule in force** + `propose_change` → `send_review_email`
  per proposal (failure → `regulatory_watch.email_failed`, proposal kept) →
  `regulatory_watch.completed` (outcome, proposals, `summary_methods`, `summary_fallbacks`
  with reasons, `skipped`, removed, emails, failures).
- Outcomes: `proposed` | `no_change` (no new diff, nothing proposed) | `formatting_only` |
  `nothing_to_propose`. `WatchState` fields: run_id, change, summaries, proposals, skipped,
  removed_sections, emails, email_failures, outcome.
- Summaries: `summarize(change, model)` — LLM (summary tier) if its text has no number absent
  from the two texts; one corrective retry naming the offending numbers; else
  `template_summary` ("Compared with the rule in force, KEY: "…" -> "…"."). Prompt forbids
  arithmetic (gpt-oss-20b otherwise writes e.g. "an additional 5%" — invented and wrong,
  since SWS/IGST compound). Live check after the retry: 5/5 LLM summaries on 25%→30%.
- Scheduler: `create_scheduler(interval)` (AsyncIOScheduler, max_instances=1, coalesce);
  `scheduled_scan()` logs + audits `regulatory_watch.failed` and never raises.

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
- MCP tests: `async with connect(server.mcp) as client:` (in-memory
  `create_connected_server_and_client_session`); tool calls run the server's
  `session_scope()` on a worker thread against the same rolled-back test connection. Compare
  with `direct(lambda s: svc.fn(s, ...))` (fresh session each time — avoids identity-map
  staleness) via `assert_parity`; assert parity right after the call it checks (state moves).
  `structlog.testing.capture_logs()` captures the `mcp.tool_call` events.
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
- Phase 6: the watch agent is deterministic (LLM for summaries only); it proposes from the
  **bulletin-vs-rulebook drift**, not from the scan-to-scan diff (user decision after live
  testing: reviewers must see the change against the rule in force; changes made before the
  first scan are caught; rejected text is never re-raised). Rejections require a reason
  (`decision_note`, migration 0006). New MCP tools `get_review_context`,
  `list_bulletin_drift`. Customs Bulletin page gained "Run scheduled scan now" (fires the
  scheduler job — the scan path, not the save path, so Option A holds) and a "Latest scan vs
  the approved rulebook" table; the scan-to-scan diff is relabelled. LLM provider Groq; email
  via Gmail. Until the FastAPI app exists the scheduler runs as its own process. Reflected in
  `architecture.md` §1, §3.7, §5, §6, §7 and `docs/setup_gmail.md`.
- MCP (Phase 5): SDK pinned `mcp>=1.24,<2` (2.x exists, but `langchain-mcp-adapters` 0.3.x
  requires <2); FastMCP servers; tools beyond the §5 examples so each domain is writable via
  MCP (FR8); `as_of` instead of `date`; `search_rules` interim keyword retriever; reviewer email
  via an outbox backend until Gmail (Phase 6); caller identity via `_meta`. Reflected in
  `architecture.md` §1 and §5.
- Compliance/bulletin (Phase 4): the live bulletin is a new single-row table
  `bulletin.publication` (spec had only snapshots); snapshots gained `seq` (identity) for a
  reliable order; `compliance.rules` gained `change_summary`; `propose_change` takes
  `rule_key` and `effective_date` (spec signature omitted them) and `change_summary` instead
  of `diff_summary`; the bulletin has a sectioned text format so changes are attributable per
  rule; lifecycle and immutability enforced by triggers; `StaleProposalError` and
  `DuplicateProposalError` added. Reflected in `architecture.md` §3.7–3.8 and §5.
- mypy strict everywhere with `python_version` unset (numpy stubs need ≥3.12 syntax);
  dashboards/tests have relaxed `disallow_untyped_defs`.

## 10. Open items

- **Reviewer identity:** `decided_by` is whatever name is typed in the Compliance sidebar (no
  login). Fine for the demo; Phase 11 (approval gate) / Phase 12 (audit) should tie decisions to
  a real identity — raise with the user then.
- **Removed rules:** a rule dropped from the bulletin is only flagged
  (`regulatory_watch.removed_sections_flagged`); there is no repeal workflow.
- (Closed) A scan crashing between detection and proposal no longer loses the change: the
  next scan's rulebook-drift check proposes it.

### Open decision — raise with the user before Phase 8

`demurrage_service.get_rate` needs a **container type**, but `shipping.shipments` (spec §3.1)
has no `container_type` column, so the Phase 8 risk agent cannot price a shipment. Proposed
(already suggested to the user, not yet approved): add `container_type TEXT NOT NULL DEFAULT
'40ft'` to shipments in a small migration at the start of Phase 8 (or earlier), set per
fixture (all 40ft unless the user specifies otherwise), and expose it in `ShipmentCreate/
Update/Record` and the Shipping Ops page.

## 11. Gotchas already hit (don't rediscover them)

- **gpt-oss on Groq** needs `reasoning_format="parsed"` so the thinking stays out of
  `.content`; it tends to do arithmetic in summaries — see the retry/guard in §6.
- **LLM keyword names** (`model`, `api_key`, `timeout`) are pydantic aliases mypy can't see —
  pass them via a `dict[str, Any]` mapping (`agents/llm.py`).
- google-auth / googleapiclient / apscheduler / httplib2 are untyped: mypy override
  `ignore_missing_imports` in `pyproject.toml`; targeted `# type: ignore[no-untyped-call]` on
  `Credentials` methods.
- PowerShell 5.1 wraps a native program's stderr at console width when redirected — read JSON
  logs from bash or a file, not `2>` in PowerShell.
- The auto-mode permission classifier occasionally returns "no verdict" for shell commands;
  retry later or write the script to the scratchpad and run it. File edits are unaffected.
- A test module that also defines a helper module-level function reading `get_settings()`
  must monkeypatch the attribute on the module that *uses* it (`watch_scheduler.get_settings`).

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
- FastMCP runs *sync* tool functions on the event loop — tools are `async` and push the
  blocking service call to `anyio.to_thread.run_sync`. Exceptions raised in a tool become an
  `isError` result (the server survives). Lists are flattened into one TextContent per item —
  use `structuredContent` for parity, not the text blocks.
- The stdio MCP server's stdout is the protocol: log to stderr only. FastMCP's own INFO
  request logs are silenced with `FastMCP(..., log_level="WARNING")` (`create_server`).
- CI's unit job has no `.env`; a subprocess that builds `Settings` needs `POSTGRES_PASSWORD` in
  its env (the stdio test passes a placeholder — listing tools never connects).
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

## 12. Next: Phase 7 — Document Verification Agent (new chat)

Read `docs/implementation_plan.md` Phase 7, `docs/edge_cases.md` Phase 7 (and the Phase 1 and
Phase 3 items that name this agent), `docs/architecture.md` §6 (CaseState, shipment pipeline)
and `docs/conventions.md` §4. Create branch `phase-7-document-verification` from `main`.

- **Deliverable:** `agents/document_verification_agent.py`, calling `shipping_records_mcp`
  and `fx_rates_mcp` only (through `open_toolbox`).
- **Exit criterion:** run against all four Phase 1 fixtures; exactly `qty_mismatch` and
  `missing_document` are flagged, each with the specific field-level discrepancy (not a
  boolean) — e.g. invoice qty 800 vs packing list 760 (delta 40); commercial invoice missing.
- **Edge cases:** only a packing list → report `missing_document`, no quantity comparison
  against a nonexistent document; amended invoice (v2 / `superseded_by`) → use only the latest
  version (`get_documents` already hides superseded ones); currency like `"XYZ"` → flag
  `invalid_currency` and do **not** pass it to `resolve_rate`; conflicting qty across two
  documents → report both values and the delta; a cancelled shipment → the agent reads with
  `get_active_shipment` and stops on `ShipmentCancelledError`; a quotation whose `expires` is
  past → surface "quote expired" (Phase 3 edge case: `resolve_rate` returns `expires`; valid
  *through* that day).
- **Design notes:** start `agents/state.py::CaseState` (shipment_id, documents, fx_context,
  …) with this agent's slice; keep the checks deterministic (the reasoning-tier LLM, if used,
  only explains findings, with a fallback); give the agent the same MCP/audit/test patterns as
  the watch agent (§6); fixtures are the four seeded shipments (§8) and seeded FX (§8).
