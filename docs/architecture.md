
# Technical Architecture Spec — Port & Customs Clearance Copilot
*(Build spec — replaces `architecture.md` as the working reference for implementation)*

## 1. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language/runtime | Python 3.11+ | Matches existing stack; async support needed for FastAPI + MCP |
| API/service layer | FastAPI | Already in stack; also hosts the APScheduler process for the Regulatory Watch Agent |
| Agent orchestration | LangGraph (`StateGraph`) | Already in stack; native support for conditional edges, persistence, and `interrupt()`-based human-in-the-loop — directly reusable from the last-mile delivery control tower project's HITL pattern |
| LLM tool-calling | LangChain + **OpenAI** (`langchain-openai`): `OPENAI_REASONING_MODEL` (default gpt-5.5) for agent reasoning, `OPENAI_SUMMARY_MODEL` (default gpt-5.4-mini) for summaries | Already in stack; tool binding for agents. No key → deterministic fallbacks |
| Relational data store | PostgreSQL 15+ (native local install; developed against 18.x) | Structured, relational data (shipments, rates, rules) with real transactional integrity; matches ongoing SQL study track |
| Vector store (RAG) | ChromaDB, embedded (`chromadb.PersistentClient` at `CHROMA_PATH`) | Already in stack; holds embedded compliance rule chunks; in-process, so no server to run |
| Embeddings / retrieval | BGE embeddings + BM25 + Reciprocal Rank Fusion + cross-encoder reranking | Reuses the exact retrieval pipeline already mastered (PolicyMind-equivalent) |
| ORM / migrations | SQLAlchemy 2.0 + Alembic | Type-safe models, versioned schema changes across 13 phases |
| Data validation | Pydantic v2 | Shared schema layer between service functions, MCP tool I/O, and FastAPI |
| MCP servers | Official `mcp` Python SDK, **1.x** (`FastMCP`; pinned `<2` because `langchain-mcp-adapters` supports 1.x only) | Standards-compliant; each server run as its own stdio process (`python -m mcp_servers.<name>.server`) |
| MCP↔LangGraph bridge | `langchain-mcp-adapters` | Exposes MCP tools as LangChain-compatible tools inside each agent node |
| Scheduler | APScheduler (`AsyncIOScheduler`), embedded in the FastAPI app | Drives the Regulatory Watch Agent's periodic scan; no separate infra (Celery/Redis) needed at this scale |
| Email | Gmail API v1 (`google-api-python-client`) + OAuth 2.0 (`google-auth-oauthlib`) | Real send, as decided — see §7 |
| Dashboards | Streamlit (multipage app) | Fastest path to a working, editable UI per data domain, in the same language as everything else; one page per synthetic world plus a combined Mission Control page |
| Containerization | None locally — Docker Compose in GitHub CI only | Local runs are native (saves disk; no Docker Desktop/WSL2). `docker-compose.yml` provides Postgres for the CI integration job only |
| Testing | pytest + pytest-asyncio | Unit tests per service/agent; fixtures seed Postgres via a test schema |
| Logging/audit | `structlog` (JSON logs) + a dedicated `audit_log` Postgres table | Every agent action and human decision is queryable, not just log-scraped |

## 2. Repository layout

```
capstone-customs-copilot/
├── docker-compose.yml                # GitHub CI only (Postgres) — not used locally
├── .env.example
├── scripts/                          # run as modules: python -m scripts.<name>
│   ├── setup_local_db.py             # one-time local role + app DB + test DB creation
│   ├── seed.py                       # runs every domain seed (the one documented seed command)
│   ├── seed_shipments.py             # idempotent seed of the 4 fixture shipments
│   └── seed_port_rates.py            # idempotent seed of INNSA/INMUN demurrage tariffs
├── alembic/                          # schema migrations
├── services/                         # single source of truth — see §4
│   ├── shipping_service.py
│   ├── demurrage_service.py
│   ├── fx_service.py
│   ├── compliance_service.py
│   ├── bulletin_service.py
│   └── audit_service.py
├── models/                           # SQLAlchemy + Pydantic schemas
│   ├── db_models.py
│   └── schemas.py
├── mcp_servers/
│   ├── shipping_records_mcp/server.py
│   ├── demurrage_rates_mcp/server.py
│   ├── fx_rates_mcp/server.py
│   ├── compliance_rulebook_mcp/server.py
│   ├── customs_bulletin_mcp/server.py
│   └── notifications_mcp/server.py
├── agents/
│   ├── state.py                      # shared LangGraph state schema
│   ├── graph.py                      # top-level StateGraph wiring all agents
│   ├── regulatory_watch_agent.py
│   ├── document_verification_agent.py
│   ├── demurrage_risk_agent.py
│   ├── compliance_rag_agent.py
│   ├── clearance_checklist_agent.py
│   └── supervisor.py
├── rag/
│   ├── ingest.py                     # embeds approved rules into Chroma on approval
│   └── retriever.py                  # BM25 + dense + RRF + cross-encoder rerank
├── scheduler/
│   └── watch_scheduler.py            # APScheduler job registration
├── integrations/
│   └── gmail_client.py               # OAuth flow + send()
├── dashboards/
│   ├── Home.py
│   └── pages/
│       ├── 1_Shipping_Ops.py
│       ├── 2_Demurrage.py
│       ├── 3_FX_Rates.py
│       ├── 4_Compliance.py
│       ├── 5_Customs_Bulletin.py
│       └── 6_Mission_Control.py
├── tests/
│   ├── test_services/
│   ├── test_mcp_servers/
│   └── test_agents/
└── docs/
    ├── simple_docs/                  # plain-language versions (gitignored)
    └── *.md                          # this technical spec set
```

## 3. Data layer — schema

All tables live in one PostgreSQL database (`customs_copilot`), separated by schema prefix per domain for clarity (`shipping.*`, `demurrage.*`, `fx.*`, `compliance.*`, `bulletin.*`, `audit.*`).

### 3.1 `shipping.shipments`
```
id                UUID PK
container_id      TEXT UNIQUE
status            ENUM('quoted','booked','in_transit','docked','under_review','cleared','held','closed','cancelled')
                  -- 'cancelled' is terminal; reachable only before docking. Allowed moves:
                  -- services/shipping_service.py::TRANSITIONS
cargo_type        TEXT
hs_code           TEXT
declared_qty      NUMERIC
declared_value    NUMERIC
declared_currency CHAR(3)            -- ISO 4217
origin_port       TEXT
destination_port  TEXT
etd                TIMESTAMPTZ        -- estimated/actual departure
eta                TIMESTAMPTZ NULL
quote_date         DATE NULL
quote_validity_days INTEGER NULL     -- 7-14, per FX quotation-stage rule
quote_expiry_date  DATE NULL
bol_date           DATE NULL         -- "on board" date, locks BoL-stage FX rate
invoice_date       DATE NULL
created_at         TIMESTAMPTZ DEFAULT now()
updated_at         TIMESTAMPTZ DEFAULT now()
```

### 3.2 `shipping.documents`
```
id             UUID PK
shipment_id    UUID FK -> shipments.id
doc_type       ENUM('invoice','packing_list','bill_of_lading','certificate','other')
version        INTEGER              -- supports amended/re-issued docs
qty            NUMERIC NULL
value          NUMERIC NULL
currency       CHAR(3) NULL
issued_date    DATE
superseded_by  UUID NULL FK -> documents.id
created_at     TIMESTAMPTZ DEFAULT now()
```

### 3.3 `demurrage.port_rates`
```
id              UUID PK
port_code       TEXT
container_type  TEXT
currency        CHAR(3)
free_days       INTEGER
daily_rate      NUMERIC
effective_from  DATE
effective_to    DATE NULL        -- inclusive; NULL = open-ended
-- EXCLUDE USING gist (port_code =, container_type =, daterange(effective_from, effective_to,'[]') &&)
--   → periods for one tariff line never overlap, so a lookup matches at most one row.
-- CHECK free_days >= 0 AND daily_rate >= 0; CHECK effective_to >= effective_from.
-- container_type ∈ {20ft, 40ft, 40hc, 20rf, 40rf} (validated in Pydantic; column is TEXT).
```

### 3.4 `fx.spot_rates` (quotation stage)
```
id           UUID PK
pair         TEXT      -- 'BASE/QUOTE', e.g. 'USD/INR' = INR per 1 USD
rate         NUMERIC(12,6)
as_of        TIMESTAMPTZ
source       TEXT      -- e.g. 'simulated-spot-feed'
-- UNIQUE (pair, as_of); CHECK rate > 0. Same (pair, as_of) re-entered = audited in-place correction.
```

### 3.5 `fx.locked_rates` (BoL / invoice stage — historical, immutable once written)
```
id           UUID PK
pair         TEXT
rate         NUMERIC(12,6)
lock_date    DATE       -- the BoL "on board" date or invoice generation date
lock_type    ENUM('bol','commercial_invoice')
-- UNIQUE (pair, lock_type, lock_date); CHECK rate > 0.
-- Trigger fx.reject_mutation() rejects every UPDATE/DELETE (immutability enforced in Postgres).
```

### 3.6 `fx.gazetted_rates` (port charges / customs stage)
```
id              UUID PK
pair            TEXT
rate            NUMERIC(12,6)
period_start    DATE
period_end      DATE        -- inclusive
publishing_authority TEXT   -- simulated customs/central-bank name
revision_of     UUID NULL FK -> gazetted_rates.id   -- handles mid-period corrections
-- Append-only (same trigger as locked_rates): a wrong rate is corrected by a revision row.
-- EXCLUDE gist (pair =, daterange &&) WHERE revision_of IS NULL      → originals never overlap.
-- EXCLUDE gist (revision_of =, daterange &&) WHERE revision_of IS NOT NULL
--                                           → revisions of one row never overlap each other.
-- A revision is for the same pair and lies within the revised row's period (service check);
-- it may cover the whole period or only part of it, and may itself be revised.
```

### 3.7 `compliance.rules`
```
id             UUID PK
rule_key       TEXT           -- stable identifier across versions, e.g. 'HSN-8708-duty'
version        INTEGER
status         ENUM('approved','pending','rejected')
content        TEXT           -- the rule text, embedded into Chroma on approval
hs_codes       TEXT[]
effective_date DATE
change_summary TEXT NULL      -- what changed and why, shown to the reviewer
superseded_by  UUID NULL FK -> rules.id
source_diff_id UUID NULL FK -> bulletin.change_log.id
created_at     TIMESTAMPTZ DEFAULT now()
decided_by     TEXT NULL      -- human reviewer identifier
decided_at     TIMESTAMPTZ NULL
decision_note  TEXT NULL      -- the reviewer's reason; required to reject (migration 0006)
-- UNIQUE (rule_key, version); CHECK version >= 1; hs_codes '{}' = applies to all goods.
-- UNIQUE INDEX (rule_key) WHERE status='approved' AND superseded_by IS NULL
--   → at most one version in force per rule.
-- Trigger compliance.guard_rule(): inserts must be pending + undecided; status changes once,
--   from pending, together with decided_by/decided_at; only an approved row gets
--   superseded_by (once); rule text/metadata immutable; no deletes.
-- Approving vN supersedes the version in force; approving an older proposal after a newer
--   version was approved raises StaleProposalError. Pending siblings are never touched.
-- Reviewers judge a proposal against the RULE IN FORCE, never against the previous bulletin
--   scan: compliance_service.review_context(rule_id) = the version in force + the history
--   since it (other proposals: pending/superseded, or rejected with reason), shared by the
--   Compliance page and the review email. compliance_service.bulletin_drift() lists rules where
--   the latest scanned bulletin differs (beyond whitespace/case) from the rule in force, noting
--   an identical pending or rejected version.
```

### 3.8 `bulletin.publication` / `bulletin.snapshots` / `bulletin.change_log`
```
publication: id UUID PK, singleton BOOLEAN UNIQUE CHECK(singleton), content TEXT,
             updated_at TIMESTAMPTZ, updated_by TEXT
             -- the live simulated bulletin (single row), edited only by the dashboard;
             -- publishing never captures a snapshot (Option A, edge_cases.md Phase 6)
snapshots:   id UUID PK, seq BIGINT IDENTITY UNIQUE, content TEXT, captured_at TIMESTAMPTZ
             -- captured only by the scheduled scan; append-only (trigger); seq orders them
change_log:  id UUID PK, prior_snapshot_id UUID FK, new_snapshot_id UUID FK,
             diff_summary TEXT, is_substantive BOOLEAN,
             detected_at TIMESTAMPTZ, emailed_at TIMESTAMPTZ NULL
             -- UNIQUE (prior_snapshot_id, new_snapshot_id) → idempotent detection;
             -- only emailed_at may be set, once (trigger)
```
Bulletin format: free text, then one section per rule — `## <rule_key>`, then
`Effective: YYYY-MM-DD` and `HS codes: <codes>|all` lines, then the rule text. Publishing
validates it. `diff_since_last` compares the two most recent snapshots; a change is
substantive unless every section (and the text around them) differs only in whitespace/case.
`bulletin_service.section_changes(change_id)` attributes a change to rule keys
(added / modified / removed), so the watch agent raises one proposal per changed rule.

### 3.9 `audit.events` (created in migration `0001`, since every Phase 1 write is audited)
```
id           UUID PK
actor        TEXT         -- agent name or human identifier
action       TEXT
entity_type  TEXT
entity_id    UUID
payload      JSONB        -- inputs/outputs/tool calls, per conventions.md
created_at   TIMESTAMPTZ DEFAULT now()
```

## 4. Service layer — single source of truth

Service functions take the caller's SQLAlchemy `Session` and only flush; the caller owns the transaction via `services/db.py::session_scope()`, so a change and its `audit.events` row commit atomically. Every domain gets one `services/<domain>_service.py` module of plain, typed, synchronous-or-async Python functions operating directly on the SQLAlchemy models (e.g. `create_shipment()`, `get_shipment(id)`, `list_pending_rules()`, `approve_rule(rule_id, reviewer)`, `resolve_fx_rate(pair, date, stage)`). **Both the MCP server tool handlers and the Streamlit dashboards call these same functions** — never a duplicate query. This is the mechanism that satisfies the "no drift between dashboard and MCP layer" convention: there is structurally only one code path to the data.

## 5. MCP layer — servers and tool contracts

Each server (`mcp_servers/<name>/server.py`) is a `FastMCP` instance whose tools are one-line
calls into the matching service module, through `mcp_servers/common.py::run_tool`, which:

- reads the caller from the request's `_meta`: `actor` (the audit-trail actor for writes;
  default `mcp:<server>`) and `run_id` (correlates an agent run's calls);
- runs the service call in its own `session_scope()` transaction on a worker thread;
- logs one structlog `mcp.tool_call` event per call (tool, arguments, result or error,
  duration, actor, run_id) — JSON to stderr, since stdout carries the stdio protocol;
- returns domain failures as tool errors whose text contains `"<ErrorType>: message"`, a lost
  database as `DatabaseUnavailableError` (the server keeps serving), anything else as
  `UnexpectedError` without internals.

Arguments are validated by FastMCP against the typed signature before any service runs. Every
tool declares its return type, so results travel as **structured content** (a model as its
JSON object; lists / optional values as `{"result": ...}`) — the Phase 5 parity tests compare
that payload byte-for-byte (canonical JSON) with the direct service call. Tool names are
verb-first (`get_ list_ create_ update_ propose_ decide send_ resolve_`, plus the contract's
`search_` and `diff_`) and unique across servers, because agents load several servers at once.
Dates are `as_of` everywhere (the contract's `resolve_rate(pair, date, stage)` is
`resolve_rate(pair, as_of, stage, ...)`). Each server mirrors its dashboard's operations
(FR8: every domain readable *and* writable through both).

**`shipping_records_mcp`** — `list_shipments(status?)`, `get_shipment(shipment_id)`,
`get_shipment_by_container(container_id)`, `get_active_shipment(shipment_id)` (fails with
`ShipmentCancelledError` — agents use this), `get_documents(shipment_id, include_superseded?)`,
`create_shipment(shipment: ShipmentCreate)`, `update_shipment(shipment_id, changes:
ShipmentUpdate)`, `update_status(shipment_id, status)`, `create_document(shipment_id, document:
DocumentCreate)`, `update_document(document_id, changes: DocumentAmend)` (re-issues a new
version).

**`demurrage_rates_mcp`** — `get_rate(port_code, container_type, as_of) -> PortRateRecord`,
`get_demurrage_charge(port_code, container_type, as_of, days_in_port) -> DemurrageCharge`,
`list_rates(port_code?)`, `list_ports()`, `update_rate(rate: PortRateUpsert)` (add a period or
correct the one starting that day).

**`fx_rates_mcp`** — `resolve_rate(pair, as_of, stage, quote_validity_days?, invoice_kind?,
bol_date?) -> ResolvedRate` (the four-rule engine, §8), `get_spot_rate(pair, as_of)`,
`get_locked_rate(pair, lock_type, lock_date)`, `get_gazetted_rate(pair, as_of)`,
`list_pairs()`, `list_spot_rates(pair?)`, `list_locked_rates(pair?)`,
`list_gazetted_rates(pair?)`, `update_spot_rate(rate)`, `create_locked_rate(rate)`,
`create_gazetted_rate(rate)` (with `revision_of` = a correction).

**`compliance_rulebook_mcp`** — `search_rules(query, hs_code?, limit=5) -> list[RuleMatch]`
(approved rules only, via `list_approved`'s `WHERE status='approved'`; an interim keyword
scorer until the Phase 9 hybrid retriever replaces it behind the same service function),
`list_approved(hs_code?)`, `list_pending()`, `list_rules(rule_key?, status?)`,
`get_rule(rule_id)`, `get_current_rule(rule_key)`, `propose_change(proposal: RuleProposal)`
(rule_key, content, hs_codes, effective_date, change_summary?, source_diff_id? — status forced
to `pending`; there is no status field), `decide(rule_id, decision: "approve"|"reject",
reviewer, note?)` (only path to `approved`; a named reviewer is required, and a reason to
reject), `get_review_context(rule_id) -> ReviewContext`, `list_bulletin_drift() ->
list[RuleDrift]` (Phase 6).

**`customs_bulletin_mcp`** — `get_current_snapshot()` (latest *scan*),
`diff_since_last_scan() -> ChangeLogEntryRecord | None` (idempotent),
`get_section_changes(change_id) -> list[SectionChange]`, `get_change(change_id)`,
`list_changes()`, `list_snapshots(limit?)`; publisher side for operators (not given to the
watch agent): `get_publication()`, `update_publication(content)`. Capturing a snapshot is
deliberately not a tool — only the scheduler captures (Option A).

**`notifications_mcp`** — `send_review_email(to, subject, diff_summary, rule_id) ->
EmailReceipt`, via `services/notification_service.py` and the sender chosen by
`EMAIL_BACKEND`: `outbox` (default; `.eml` files in the git-ignored `OUTBOX_PATH`) or `gmail`
(Phase 6, `integrations/gmail_client.py`). A backend failure is `EmailDeliveryError` and is not
audited as sent.

## 6. Agent layer — LangGraph design

A single top-level `StateGraph` with a shared Pydantic state object (`CaseState`: shipment_id, documents, fx_context, risk_score, compliance_findings, checklist, pending_human_decisions, audit_trail). Two entry points:

- **Shipment pipeline** (triggered per shipment, or on status change): `document_verification_agent → demurrage_risk_agent → compliance_rag_agent → clearance_checklist_agent`, fan-out/fan-in via LangGraph parallel branches where independent, joined at `clearance_checklist_agent`.
- **Regulatory watch pipeline** (triggered by the scheduler, not a shipment): `regulatory_watch_agent` alone, ending either at a no-op or at a `propose_change` MCP call + email.

  *As built (Phase 6):* `scheduler/watch_scheduler.py::run_scan` captures a snapshot (service
  call — the only capture site), then runs a one-node `StateGraph[WatchState]`
  (`agents/state.py`). The agent reaches data only through MCP (`agents/mcp_clients.py`:
  `open_toolbox` connects to the role's servers only — stdio subprocesses in production,
  in-memory in tests — passes `_meta` actor/run_id and raises `McpToolError` with the server's
  error type). Its control flow is **deterministic** (the guarantees — idempotency, one
  proposal per `rule_key`, never approve — are not judgement calls): `diff_since_last_scan` →
  records the detection (and `get_section_changes` links proposals to it and flags removed
  rules as `regulatory_watch.removed_sections_flagged` — no repeal workflow yet); then
  `list_bulletin_drift` → for every rule where the scanned bulletin differs from the **rule in
  force** (this also catches a change made before the first scan), unless that exact text is
  already pending or was rejected before (never re-raised), a summary *against the rule in
  force* + `propose_change` → `send_review_email` per proposal (the email compares with the rule
  in force and lists the history since it). The LLM (summary tier) only writes the
  plain-language summary; a reply that is empty, fails, or contains a number absent from the
  two texts is replaced by a deterministic template summary. Agent-level outcomes are audited as `agent_run` events
  (`regulatory_watch.completed`, `.email_failed`, `.failed`) by `agents/audit.py`.
  Until the FastAPI app exists the scheduler runs as its own process
  (`python -m scheduler.watch_scheduler`, or `--once`); the Customs Bulletin page's
  "Run scheduled scan now" fires the same job.

**Human-in-the-loop**: implemented with LangGraph's native `interrupt()` inside `supervisor.py` at exactly two points — before `compliance_rulebook_mcp.decide()` is allowed to fire, and before a shipment can transition to `cleared`/filed. Graph state is persisted via `PostgresSaver` (LangGraph checkpointer, same Postgres instance), so an interrupted run survives process restarts and resumes cleanly when a human acts through the dashboard — this reuses the HITL pattern already proven in the delivery control tower project.

## 7. Gmail integration (OAuth)

- Google Cloud Console project, OAuth consent screen in **Testing** mode, `brahmalabs.ai@gmail.com` added as a test user.
- Scope: `https://www.googleapis.com/auth/gmail.send` only (least privilege — no read/modify).
- `google-auth-oauthlib.InstalledAppFlow` for the one-time local browser consent; resulting `token.json` (refresh token) cached under a git-ignored `secrets/` directory, auto-refreshed by `google.auth.transport.requests.Request()` on expiry.
- `integrations/gmail_client.py` exposes `GmailSender.send(to, subject, body)`, reached only through `notifications_mcp` → `notification_service` → `integrations.email.get_email_sender()` when `EMAIL_BACKEND=gmail` (default `outbox`: `.eml` files, no network). Sends never open a browser: `python -m integrations.gmail_client authorize` does the one-time consent, `send-test` checks delivery. Step-by-step setup: `docs/setup_gmail.md`. Review emails go to `REVIEWER_EMAIL`; a successful send stamps `bulletin.change_log.emailed_at` (first email only).
- Failure handling: a send failure raises `EmailDeliveryError`, caught by `regulatory_watch_agent`, which still commits the `pending` rule change to `compliance.rules` and logs the failure to `audit.events` (per the edge-case requirement that a failed email must never lose the detected change).

## 8. FX resolution engine (`fx_service.resolve_rate`)

Pseudocode for the four-rule logic, all in MVP scope:

```
def resolve_rate(session, pair, as_of, stage, *, quote_validity_days=None,
                 invoice_kind=None, bol_date=None):
    if stage == "quotation":
        rate = latest spot_rates row for pair with as_of before the end of that IST calendar day
        expiry = as_of + quote_validity_days (7-14, caller-supplied)
        return ResolvedRate(rate, source="spot", locked=False, expires=expiry)

    if stage == "bol":
        rate = locked_rates row where pair=pair, lock_type='bol', lock_date=as_of
        if not found: raise RateNotFoundError  # no silent fallback

    if stage == "invoice":
        # invoice_kind='commercial': lock to its own invoice date
        rate = locked_rates row where lock_type='commercial_invoice', lock_date=as_of
        # invoice_kind='freight': re-use the BoL-date rate — caller passes bol_date
        rate = locked_rates row where lock_type='bol', lock_date=bol_date

    if stage == "port_charges":
        rate = gazetted original where pair=pair and period_start <= as_of <= period_end,
               then descend to the deepest revision_of row whose period also contains as_of
```

Every branch raises a typed exception (`RateNotFoundError`) rather than defaulting — enforced by the edge-case tests in `spec_edge_cases.md`. Stage context is strict: missing context (validity days for a quotation, `invoice_kind`, `bol_date` for a freight invoice) or context the stage does not use raises `InvalidRateRequestError`. `ResolvedRate` carries `source_id` (the `fx.*` row used) and a human-readable `basis`, so every conversion is traceable. A quote is valid *through* `expires`; callers (dashboard, document verification agent) surface "quote expired" via `fx_service.is_quote_expired(resolved, on)`.

## 9. Dashboards

Streamlit multipage app (`streamlit run dashboards/Home.py`). Each page imports the matching service module directly (in-process, same source of truth as §4) — no separate REST hop needed for the dashboards themselves. Mission Control (page 6) additionally triggers the LangGraph graph directly (via `agents/graph.py`) and renders `audit.events` as a live-updating table (Streamlit `st.rerun` on a short poll interval) plus the before/after ROI comparison (two runs of the same fixture set with the compliance/FX/demurrage data frozen at two different edited states).

## 10. Deployment (native, no Docker)

All processes run natively from the project `.venv`: PostgreSQL is the locally installed server (role + database created once by `scripts/setup_local_db.py`); ChromaDB is embedded in-process, persisting to `CHROMA_PATH`; the `api` process (FastAPI + APScheduler + MCP servers as subprocesses) runs via `uvicorn`; the dashboards run via `streamlit run dashboards/Home.py`. `.env` holds the Postgres connection keys, Chroma path, Gmail secrets path — none of it committed (see `spec_conventions.md` §6). `docker-compose.yml` is used only by the GitHub Actions integration job to provide a throwaway Postgres.
