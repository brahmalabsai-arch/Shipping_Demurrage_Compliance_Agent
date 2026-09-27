
# Technical Problem Statement — Port & Customs Clearance Copilot
*(Build spec — replaces `problem_statement.md` as the working reference for implementation)*

## 1. System objective

Build a runnable, locally-deployable multi-agent system that ingests a simulated shipment's documents and lifecycle events, cross-validates them against three independently-editable synthetic data sources (port demurrage rates, foreign-exchange rate rules, and a compliance rulebook), and produces: (a) a machine-checked list of discrepancies, (b) a quantified hold-risk/cost estimate, and (c) an auditable, human-gated workflow for both regulatory-rule changes and final clearance approval. The system must expose each of its data domains as an MCP server and demonstrate at least one agent performing autonomous, scheduled tool-calling against an external (simulated) source with a human-notification side effect (Gmail).

## 2. Functional requirements

| # | Requirement | Verified by |
|---|---|---|
| FR1 | System stores shipment, document, and lifecycle-status data with full CRUD via a dashboard and identical read access via an MCP server | Phase 1/5 tests |
| FR2 | System resolves the correct FX rate for a given (currency pair, date, lifecycle stage) per the four documented industry rules, including quotation validity windows | Phase 3 tests |
| FR3 | System detects discrepancies between a shipment's attached documents (quantity, value, currency) | Phase 7 tests |
| FR4 | System estimates demurrage/detention cost exposure for a shipment using the port rate table and the correct gazetted FX rate | Phase 8 tests |
| FR5 | System answers compliance/duty questions using retrieval over an **approved-only** rule set, with citations | Phase 9 tests |
| FR6 | System runs a scheduled job that detects changes to a simulated external customs bulletin, summarizes them in plain language, emails a human reviewer via real Gmail (OAuth), and stages the change as `pending` without auto-approving it | Phase 6 tests |
| FR7 | No compliance rule reaches `approved` status, and no shipment reaches `cleared`/filed status, without an explicit, logged human decision | Phase 11 tests |
| FR8 | All five data domains are independently readable/writable through both a dashboard and a standards-compliant MCP server, backed by one shared service layer (no data drift) | Phase 5 tests |
| FR9 | System produces a before/after ROI comparison (demurrage exposure avoided) derived from actual configured rate/rule data, not hard-coded figures | Phase 12 tests |

## 3. Non-functional requirements

- **Auditability**: every agent action and every human decision is a row in `audit.events`, queryable after the fact.
- **Determinism at the data layer**: FX and demurrage lookups must be reproducible given the same inputs — no non-deterministic fallback behavior (raise, don't guess).
- **Local-first, no Docker**: entire stack runs natively on the developer machine — a locally installed PostgreSQL (15+) and an embedded, in-process ChromaDB, with all Python dependencies isolated in the project `.venv` — with no external dependency except the Gmail API call itself. `docker-compose.yml` exists solely for GitHub CI and is never part of the local workflow.
- **Reuse of proven patterns**: LangGraph HITL/checkpointing pattern from the existing delivery-control-tower project; BGE/BM25/RRF/reranking retrieval pattern from PolicyMind — both explicitly reused rather than re-invented, to keep implementation risk low within the certification timeline.
- **Secrets isolation**: Gmail credentials and OAuth tokens are never committed (see `spec_conventions.md`).

## 4. Explicit out-of-scope (MVP)

- Real integration with any government customs system, port TMS, or live market data feed.
- Multi-tenant/multi-organization support (single-operator demo only).
- Real legal filing capability — the "clearance" the system produces is a decision record, not an actual customs submission.
- Mobile UI (Streamlit dashboards, desktop-oriented, are sufficient for the capstone demo).

## 5. In-scope MVP stages (confirmed)

All four FX lifecycle stages are in MVP scope: **quotation** (spot rate + validity window), **bill of lading** (locked on-board-date rate), **invoicing** (commercial-invoice-date rate, and freight-invoice reuse of the BoL rate), and **port charges** (gazetted-period rate). This means the shipment status model includes a `quoted` stage prior to `booked`, and the Shipping Ops dashboard supports creating a shipment starting from a quotation.

## 6. Definition of done (capstone submission)

1. All 13 implementation phases pass their documented test criteria.
2. A live run of the JNPT auto-parts case-study scenario, triggered from Mission Control, produces the expected discrepancy flags, risk score, compliance answer, and checklist — visible end to end without manual code intervention.
3. A real email is received (via the OAuth-connected Gmail account) when a simulated bulletin change is scanned.
4. The ROI view produces a defensible, traceable number from the two comparison runs.
5. A clean checkout on a machine with PostgreSQL 15+ installed reaches a demoable state following only the written setup steps (venv install, `scripts/setup_local_db.py`, migrations, seed) — no Docker.
