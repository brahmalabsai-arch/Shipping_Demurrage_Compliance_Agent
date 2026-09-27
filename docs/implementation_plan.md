
# Technical Implementation Plan — Port & Customs Clearance Copilot
*(Build spec — replaces `implementation_plan.md` as the working reference for implementation)*

Same 13-phase sequencing as the conceptual plan; each phase now lists concrete deliverables, files, and pytest-verifiable exit criteria.

## Phase 0 — Foundations
**Deliverables:** repo skeleton (§2 of `spec_architecture.md`), `scripts/setup_local_db.py` (idempotent role + database creation on the native Postgres), embedded ChromaDB configured via `CHROMA_PATH`, `.env.example`, `pyproject.toml` (ruff + black + mypy config), Alembic initialized, GitHub Actions smoke workflow (its integration job uses a CI-only `docker-compose.yml` for Postgres).
**Exit criteria:** `pytest -m integration` connects to the local Postgres (15+, UTC sessions); the embedded ChromaDB persists and reopens a collection; `pytest` runs with no config errors; `ruff check .` and `mypy .` pass on the empty skeleton. No Docker is required locally.

## Phase 1 — Shipping Records
**Deliverables:** `models/db_models.py::Shipment, Document`; Alembic migration `0001_shipping`; `services/shipping_service.py` (CRUD + `list_by_status`); `dashboards/pages/1_Shipping_Ops.py`; seed script `scripts/seed_shipments.py` creating the 4 fixture shipments (clean / quantity-mismatch / missing-document / already-delayed).
**Exit criteria:** `pytest tests/test_services/test_shipping_service.py` covers create/read/update/status-transition; dashboard round-trips a manual edit; seed script is idempotent (safe to re-run).

## Phase 2 — Demurrage & Port Charges
**Deliverables:** `models/db_models.py::PortRate`; migration `0002_demurrage`; `services/demurrage_service.py::get_rate, upsert_rate`; `dashboards/pages/2_Demurrage.py`.
**Exit criteria:** lookup for an unconfigured port raises `PortRateNotFoundError` (test asserts the exception type, not just a falsy return); dashboard edit is visible to `get_rate` in the same test run without a restart.

## Phase 3 — Currency & Exchange Rate Service
**Deliverables:** `models/db_models.py::SpotRate, LockedRate, GazettedRate`; migration `0003_fx`; `services/fx_service.py::resolve_rate` implementing all four stages (§8 of `spec_architecture.md`); `dashboards/pages/3_FX_Rates.py` with three editable tables.
**Exit criteria:** parametrized pytest covering all four stages against fixture data, including: a BoL date resolving to its exact locked rate (not spot); a port-charge date resolving to the correct gazetted period even when a spot rate for that day differs; a gazetted period with a `revision_of` correctly preferring the revision; an unresolvable date raising `RateNotFoundError`; a quotation validity window correctly computed and an expired quote flagged.

## Phase 4 — Compliance Rulebook + Customs Bulletin
**Deliverables:** `models/db_models.py::ComplianceRule, BulletinSnapshot, ChangeLogEntry`; migration `0004_compliance`; `services/compliance_service.py::list_approved, list_pending, propose_change, decide`; `services/bulletin_service.py::capture_snapshot, diff_since_last`; `dashboards/pages/4_Compliance.py` and `5_Customs_Bulletin.py`.
**Exit criteria:** `propose_change` always creates a `pending` row, never `approved`; `decide("approve", ...)` is the only code path that flips status (verified by a test asserting no other function touches `status`); editing the bulletin dashboard and calling `diff_since_last` twice without an intervening capture returns `None` the second time (idempotent detection).

## Phase 5 — MCP Server Layer
**Deliverables:** all six servers under `mcp_servers/`, each a thin adapter over the matching service module, per the tool contracts in `spec_architecture.md` §5.
**Exit criteria:** an `mcp` reference client (e.g. `mcp dev` or a small pytest harness using the SDK's client session) calls every tool on every server and the results match direct service-layer calls byte-for-byte on the same fixture data — this is the concrete test that proves no dashboard/MCP drift.

## Phase 6 — Regulatory Watch Agent
**Deliverables:** `agents/regulatory_watch_agent.py`; `scheduler/watch_scheduler.py` (APScheduler job, configurable interval via `.env`); `integrations/gmail_client.py` with the OAuth flow wired and tested against the real Gmail account.
**Exit criteria:** editing the bulletin dashboard then manually firing the scheduler job results in: a `pending` row in `compliance.rules`, a real email delivered to the configured reviewer address, and an `audit.events` row for both the detection and the send. Running the job again with no bulletin change produces zero rows and zero emails. Simulating a Gmail send failure (mock the client to raise) still results in the `pending` row being committed, with the failure logged.

## Phase 7 — Document Verification Agent
**Deliverables:** `agents/document_verification_agent.py`, calling `shipping_records_mcp` and `fx_rates_mcp`.
**Exit criteria:** run against all four Phase-1 fixtures; exactly the mismatch and missing-document fixtures are flagged, with the specific field-level discrepancy identified (not just a boolean).

## Phase 8 — Demurrage & Risk Agent
**Deliverables:** `agents/demurrage_risk_agent.py`.
**Exit criteria:** cost estimate for the already-delayed fixture matches an independently hand-calculated figure (rate × days-over-free-allowance, converted at the correct gazetted rate) to the cent.

## Phase 9 — Compliance Agent (RAG)
**Deliverables:** `rag/ingest.py` (embeds a rule into Chroma only on transition to `approved`, triggered from `compliance_service.decide`); `rag/retriever.py` (BM25 + dense + RRF + cross-encoder rerank, matching the PolicyMind pipeline); `agents/compliance_rag_agent.py`.
**Exit criteria:** a golden set of ≥10 Q/A pairs against known approved rules scores acceptably on retrieval precision (reuse the RAGAS-based evaluation approach already used for PolicyMind, per the existing learning track); a question targeting a still-`pending` rule's content returns no match from Chroma (proving pending content was never embedded, not just filtered post-hoc).

## Phase 10 — Clearance Checklist Agent
**Deliverables:** `agents/clearance_checklist_agent.py`, LangGraph fan-in node combining the three upstream agents' outputs into a structured `Checklist` Pydantic model.
**Exit criteria:** full JNPT case-study fixture run produces a checklist containing every discrepancy raised upstream, each attributed to its source agent, with no duplicates.

## Phase 11 — Supervisor + Governance Gate
**Deliverables:** `agents/supervisor.py` (top-level graph wiring + `interrupt()` calls); `agents/state.py`; LangGraph `PostgresSaver` checkpointer configured against the same Postgres instance.
**Exit criteria:** a run interrupted at the compliance-approval gate can be resumed after a separate process (simulating a dashboard action) calls `compliance_service.decide(...)`, and the graph continues correctly from checkpoint; a run interrupted at the filing gate cannot be advanced by any path other than an explicit approval call; a rejection at either gate is a terminal, logged state — never silently retried as approved.

## Phase 12 — Mission Control, Audit Trail, ROI
**Deliverables:** `dashboards/pages/6_Mission_Control.py`; `services/audit_service.py::query_events`; ROI calculator function taking two fixture-set snapshots (rates/rules frozen) and computing exposure avoided.
**Exit criteria:** running the ROI calculator against a "before" (agents disabled, all fixture issues manifest) and "after" (agents enabled, issues caught pre-dock) scenario over the same 4 seeded shipments produces a number that can be manually reconstructed from the underlying demurrage/FX figures shown on their respective dashboards.

## Phase 13 — Packaging & Demo Readiness
**Deliverables:** `README.md` with setup steps; `docs/DEMO_SCRIPT.md` walking the JNPT scenario; final review that `docs/simple_docs/` is git-ignored and no secrets are tracked (`git status` / `git-secrets` scan).
**Exit criteria:** a clean checkout on a second machine (with only Python and PostgreSQL 15+ installed) reaches the demoable Mission Control state using only `README.md`, with zero undocumented manual steps.
