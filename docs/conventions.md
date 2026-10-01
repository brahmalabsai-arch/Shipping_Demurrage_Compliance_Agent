
# Technical Conventions — Port & Customs Clearance Copilot
*(Build spec — replaces `conventions.md` as the working reference for implementation)*

## 1. Code style & tooling
- Formatting: `black` (line length 100); linting: `ruff`; type checking: `mypy --strict` on `services/`, `agents/`, `mcp_servers/` (dashboards may be looser).
- Every public function in `services/` and `agents/` has a full type signature and a docstring stating intent (why), not restating the signature.
- Pydantic v2 models are the canonical schema for: MCP tool inputs/outputs, LangGraph state, and FastAPI request/response bodies. SQLAlchemy models are the canonical schema for persistence only — never pass a SQLAlchemy row directly across a service-layer boundary; convert to Pydantic first.

## 2. Naming
- Modules/files/functions: `snake_case`. Classes: `PascalCase`. Pydantic models describing a domain object: `<Noun>Record` (e.g. `ShipmentRecord`) to distinguish from the SQLAlchemy `<Noun>` ORM class.
- MCP tool names are verb-first and consistent across all six servers: `get_`, `list_`, `create_`, `update_`, `propose_`, `decide_`, `send_`, `resolve_` (plus `search_`, `diff_`, and the two governed acts of Phase 11, `revoke_` and `clear_`). No server invents a synonym for an action another server already named (e.g., never `fetch_` in one server and `get_` in another for the same kind of operation).
- Custom exceptions live in `services/errors.py`, one per failure mode, named `<Reason>Error` (e.g. `RateNotFoundError`, `PortRateNotFoundError`, `EmailDeliveryError`) — never a bare `Exception` or `ValueError` for a domain-level failure, so tests can assert on type.
- Alembic migration files are prefixed with a zero-padded sequence matching the phase that introduced them (`0001_shipping`, `0002_demurrage`, …), so the migration history itself documents the phased build.

## 3. Data conventions
- All timestamps stored `TIMESTAMPTZ` in UTC; converted to `Asia/Kolkata` only at the dashboard presentation layer (`zoneinfo`, never manual offset math).
- All monetary values: `NUMERIC` (never `FLOAT`) paired with an explicit ISO 4217 `CHAR(3)` currency column — never a bare number.
- Lifecycle-relevant dates (`bol_date`, `invoice_date`, gazetted `period_start/end`) are `DATE`, not `TIMESTAMPTZ` — the FX rules key off the calendar day.
- Nothing is hard-deleted. Status/version columns and `superseded_by` foreign keys model history; a `DELETE` statement against `shipping.*`, `fx.*`, or `compliance.*` in application code is a code-review-blocking smell.
- Every service function that mutates state writes a corresponding `audit.events` row in the same transaction (not a separate, best-effort call afterward) — use a SQLAlchemy session-level hook or an explicit `audit_service.record(...)` call inside the same `with session.begin():` block.

## 4. Agent & MCP conventions
- Every agent module exports exactly one LangGraph node function with a documented input/output slice of `CaseState` — no agent reads or writes state fields outside its declared slice.
- An agent only imports the MCP client wrapper for the servers its role legitimately needs (enforced by import-linter rule or simple code review — e.g. `compliance_rag_agent.py` must never import `shipping_records_mcp`).
- Every MCP tool call is wrapped to log: tool name, arguments (redact none — this is synthetic data), result or exception, timing. Logged via `structlog`, correlated by a `run_id` threaded through `CaseState`.
- `compliance_rulebook_mcp.decide()` is the only function anywhere in the codebase permitted to set `status='approved'`. This is enforced by a unit test that greps/AST-scans the codebase for any other `status =` assignment against `ComplianceRule`, or at minimum a code-review checklist item. (Phase 11: `revoke_decision` may additionally move the version in force `approved -> revoked`, never anything else — same scan, plus the trigger.)
- Likewise `shipping_service.clear_shipment` (with an approved filing decision) is the only path to `cleared`; `update_status` refuses it and a trigger enforces it.

## 5. Human-in-the-loop conventions
- Every `interrupt()` call in `supervisor.py` is paired with a specific, named resumption function (`resume_after_compliance_decision`, `resume_after_filing_approval`) — never a generic "continue" — so it's always clear from the code what unblocks a given interrupt.
- A rejection is a distinct terminal state (`status='rejected'`), not a re-queue and not a deletion.
- A resume value never carries decision authority: the compliance gate re-reads the rule from the rulebook; the filing gate accepts only the id of a recorded `governance.filing_decisions` row of the same run. Anything else is refused and the gate waits again.
- Each gate node calls `interrupt()` once per execution (a refused answer routes back to the gate) — never several `interrupt()` calls in a loop inside one node.
- Human decisions are taken by registered reviewers (`REVIEWERS` setting): the dashboards offer a select, not free text, and the supervisor's resume functions raise `UnknownReviewerError` otherwise.

## 6. Secrets & environment
- `.env` (real values) is git-ignored; `.env.example` (keys only, no values) is committed.
- Gmail `credentials.json` and the generated `token.json` live under `secrets/`, which is fully git-ignored.
- `docs/simple_docs/` (the plain-language versions) is git-ignored per your instruction — add both to `.gitignore` in the same Phase 0 commit:
  ```
  .env
  secrets/
  docs/simple_docs/
  ```
- Config is loaded via `pydantic-settings` (`Settings(BaseSettings)`), never `os.environ[...]` scattered through the codebase.

## 7. Testing conventions
- One test module per service/agent/MCP server, mirroring the source tree under `tests/`.
- Fixtures (the 4 seed shipments, sample rate tables) live in `tests/fixtures/` as plain Python factories or JSON, loaded into a disposable test schema/transaction per test (rollback after each test — no shared mutable test DB state).
- Every custom exception has at least one test asserting it's raised under the documented condition (this is what makes the edge-case list in `spec_edge_cases.md` actually enforced, not just documented).

## 8. Git conventions
- Conventional commits: `feat(shipping): add status transition validation`, `fix(fx): correct gazetted period boundary check`, `docs: update spec_architecture`, `test(compliance): cover pending-rejection edge case`.
- One branch per phase (`phase-3-fx-service`), merged to `main` only when that phase's exit criteria (in `spec_implementation_plan.md`) pass; tag `main` at each merge (`phase-3-complete`).
- `docs/simple_docs/` never appears in any commit diff — verify with `git log --all --full-history -- docs/simple_docs/` returning empty before each push.

## 9. ROI reporting convention
- The ROI calculator (Phase 12) must accept the fixture/rate snapshot as an explicit input parameter and return both the final number and the intermediate figures it was built from (per-shipment demurrage days avoided × daily rate × resolved FX rate) — never a single opaque output number with no reconstructable trail.
- *As built:* snapshots are named, append-only `roi.snapshots` rows (SHA-256 checked on read); the scenario assumptions (base days in port, hold days per issue type, how several holds combine) are part of the snapshot and shown with every result — no figure is hidden in code; the calculator (`roi.calculator.compare`) never reads live data.
