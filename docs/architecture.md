
# Technical Architecture Spec — Port & Customs Clearance Copilot
*(Build spec — replaces `architecture.md` as the working reference for implementation)*

## 1. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language/runtime | Python 3.11+ | Matches existing stack; async support needed for FastAPI + MCP |
| API/service layer | FastAPI | Already in stack; also hosts the APScheduler process for the Regulatory Watch Agent |
| Agent orchestration | LangGraph (`StateGraph`) | Already in stack; native support for conditional edges, persistence, and `interrupt()`-based human-in-the-loop — directly reusable from the last-mile delivery control tower project's HITL pattern |
| LLM tool-calling | LangChain | Already in stack; tool binding for agents |
| Relational data store | PostgreSQL 15+ (native local install; developed against 18.x) | Structured, relational data (shipments, rates, rules) with real transactional integrity; matches ongoing SQL study track |
| Vector store (RAG) | ChromaDB, embedded (`chromadb.PersistentClient` at `CHROMA_PATH`) | Already in stack; holds embedded compliance rule chunks; in-process, so no server to run |
| Embeddings / retrieval | BGE embeddings + BM25 + Reciprocal Rank Fusion + cross-encoder reranking | Reuses the exact retrieval pipeline already mastered (PolicyMind-equivalent) |
| ORM / migrations | SQLAlchemy 2.0 + Alembic | Type-safe models, versioned schema changes across 13 phases |
| Data validation | Pydantic v2 | Shared schema layer between service functions, MCP tool I/O, and FastAPI |
| MCP servers | Official `mcp` Python SDK | Standards-compliant; each server run as its own stdio/SSE process |
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
superseded_by  UUID NULL FK -> rules.id
source_diff_id UUID NULL FK -> bulletin.change_log.id
created_at     TIMESTAMPTZ DEFAULT now()
decided_by     TEXT NULL      -- human reviewer identifier
decided_at     TIMESTAMPTZ NULL
```

### 3.8 `bulletin.snapshots` / `bulletin.change_log`
```
snapshots:  id UUID PK, content TEXT, captured_at TIMESTAMPTZ
change_log: id UUID PK, prior_snapshot_id UUID, new_snapshot_id UUID,
            diff_summary TEXT, is_substantive BOOLEAN,
            detected_at TIMESTAMPTZ, emailed_at TIMESTAMPTZ NULL
```

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

Each server is a thin `mcp.server.Server` instance whose tool handlers call straight into the matching service module. Tool schemas below (Pydantic-typed, illustrative signatures):

**`shipping_records_mcp`**
- `get_shipment(shipment_id: str) -> ShipmentRecord`
- `list_shipments(status: str | None) -> list[ShipmentRecord]`
- `get_documents(shipment_id: str) -> list[DocumentRecord]`
- `update_status(shipment_id: str, status: str) -> ShipmentRecord`

**`demurrage_rates_mcp`**
- `get_rate(port_code: str, container_type: str, as_of: date) -> PortRate`

**`fx_rates_mcp`**
- `resolve_rate(pair: str, date: date, stage: Literal["quotation","bol","invoice","port_charges"]) -> ResolvedRate` — implements the four-rule engine (§8)
- `get_spot_rate(pair: str, as_of: date) -> SpotRate`

**`compliance_rulebook_mcp`**
- `search_rules(query: str, hs_code: str | None) -> list[RuleChunk]` (calls the RAG retriever, approved rules only — enforced at the SQL layer with `WHERE status='approved'`, not just at the prompt layer)
- `list_pending() -> list[Rule]`
- `propose_change(diff_summary: str, content: str, hs_codes: list[str], source_diff_id: str) -> Rule` (status forced to `pending`)
- `decide(rule_id: str, decision: Literal["approve","reject"], reviewer: str) -> Rule` (only path to `approved`)

**`customs_bulletin_mcp`**
- `get_current_snapshot() -> BulletinSnapshot`
- `diff_since_last_scan() -> ChangeLogEntry | None`

**`notifications_mcp`**
- `send_review_email(to: str, subject: str, diff_summary: str, rule_id: str) -> EmailReceipt`

## 6. Agent layer — LangGraph design

A single top-level `StateGraph` with a shared Pydantic state object (`CaseState`: shipment_id, documents, fx_context, risk_score, compliance_findings, checklist, pending_human_decisions, audit_trail). Two entry points:

- **Shipment pipeline** (triggered per shipment, or on status change): `document_verification_agent → demurrage_risk_agent → compliance_rag_agent → clearance_checklist_agent`, fan-out/fan-in via LangGraph parallel branches where independent, joined at `clearance_checklist_agent`.
- **Regulatory watch pipeline** (triggered by the scheduler, not a shipment): `regulatory_watch_agent` alone, ending either at a no-op or at a `propose_change` MCP call + email.

**Human-in-the-loop**: implemented with LangGraph's native `interrupt()` inside `supervisor.py` at exactly two points — before `compliance_rulebook_mcp.decide()` is allowed to fire, and before a shipment can transition to `cleared`/filed. Graph state is persisted via `PostgresSaver` (LangGraph checkpointer, same Postgres instance), so an interrupted run survives process restarts and resumes cleanly when a human acts through the dashboard — this reuses the HITL pattern already proven in the delivery control tower project.

## 7. Gmail integration (OAuth)

- Google Cloud Console project, OAuth consent screen in **Testing** mode, `brahmalabs.ai@gmail.com` added as a test user.
- Scope: `https://www.googleapis.com/auth/gmail.send` only (least privilege — no read/modify).
- `google-auth-oauthlib.InstalledAppFlow` for the one-time local browser consent; resulting `token.json` (refresh token) cached under a git-ignored `secrets/` directory, auto-refreshed by `google.auth.transport.requests.Request()` on expiry.
- `integrations/gmail_client.py` exposes `send(to, subject, body)`, called only from `notifications_mcp`.
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
