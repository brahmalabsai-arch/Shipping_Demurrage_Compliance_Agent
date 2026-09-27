
# Technical Edge Cases — Port & Customs Clearance Copilot
*(Build spec — replaces `edge_cases.md` as the working reference for implementation)*

Each item below is phrased as a concrete pytest case: module/function under test, the condition, and the expected typed outcome. Organized by the phase that should implement the test.

## Phase 1 — `test_shipping_service.py`
- `create_shipment(...)` with a duplicate `container_id` → raises `DuplicateContainerError` (unique constraint surfaced as a typed exception, not a raw `IntegrityError`).
- `update_status(id, "cancelled")` followed by a call from an already-running agent pipeline referencing that shipment → agent node checks `status` at read time and short-circuits with a `ShipmentCancelledError` rather than completing analysis.
- A shipment with `eta=None` → `demurrage_risk_agent` handles this explicitly (returns `risk="indeterminate"`), does not raise or silently default to zero risk.
- Two `documents` rows for the same shipment with conflicting `qty` → `document_verification_agent` reports both values and the delta; does not silently prefer one.

## Phase 2 — `test_demurrage_service.py`
- `get_rate("UNKNOWN", "20ft", date)` → `PortRateNotFoundError`.
- `upsert_rate(..., free_days=0)` → accepted (valid business case), verified that `demurrage_risk_agent` correctly computes charges starting day 1.
- `upsert_rate` called with `effective_from` in the future while an active shipment already docked under the current rate → `get_rate(as_of=dock_date)` must return the rate active **on `as_of`**, not the latest row (test explicitly passes `as_of` and asserts the historical rate).
- Negative `daily_rate` or `free_days` in the Streamlit form → validated at the Pydantic model level (`ge=0` constraint), rejected before it reaches `upsert_rate`.

## Phase 3 — `test_fx_service.py`
- `resolve_rate(pair, date=<Saturday with no locked_rates row>, stage="bol")` → `RateNotFoundError` (no nearest-date fallback silently substituted, per the "raise, don't guess" NFR).
- `resolve_rate` for `stage="invoice"` where `invoice_date` and `bol_date` straddle a rate change → assert the freight-invoice path returns the **BoL-locked** rate while the commercial-invoice path returns the rate locked to its own `invoice_date` — two different correct answers from the same shipment, tested explicitly as two separate assertions.
- Two `gazetted_rates` rows with overlapping periods, one `revision_of` the other → `resolve_rate(stage="port_charges")` returns the revision, verified by asserting the returned `rate` matches the revision row, not the original.
- `resolve_rate(date=<before earliest fx.locked_rates row>)` → `RateNotFoundError`, not the earliest available rate.
- `resolve_rate(stage="quotation")` with `quote_validity_days=7` and a lookup performed 10 days later → returned `ResolvedRate.expires` is in the past; caller (`document_verification_agent` or dashboard) must surface this as "quote expired," tested as a separate assertion on the caller, not just on the resolver.

## Phase 4 — `test_compliance_service.py`, `test_bulletin_service.py`
- `bulletin_service.diff_since_last()` called twice with no new `capture_snapshot()` between calls → second call returns `None` (idempotent — no duplicate `change_log` rows).
- A bulletin edit that only changes whitespace/formatting → `diff_since_last()` still detects a text difference, but `is_substantive` (computed via a normalized-text comparison, e.g. ignoring whitespace/case) is `False`; `regulatory_watch_agent` checks this flag and does not email or propose a change for a non-substantive diff.
- Two substantive changes captured in a single snapshot → `propose_change` is called twice (one `ComplianceRule` row per distinct `rule_key` affected), not merged into one ambiguous row.
- `decide(rule_id, "approve", reviewer)` called on a rule whose `rule_key` already has a different `pending` rule proposed later (a second bulletin edit arrived before the first was reviewed) → the older proposal's approval must not silently supersede or conflict with the newer pending one; assert both rows remain independently queryable with correct `superseded_by` chaining once the newer one is also decided.
- `decide(rule_id, "reject", reviewer)` → status becomes `rejected` permanently; re-running `diff_since_last()` against the *same*, unchanged bulletin content must not re-propose the same rejected change (compare against `bulletin.change_log`, not just current `compliance.rules` state).

## Phase 6 — `test_regulatory_watch_agent.py`

**Design decision (locked): Option A — scan-time snapshot diffing only.** `bulletin_service.capture_snapshot()` is called *only* from the scheduled scan path (`watch_scheduler.py`), never from the bulletin dashboard's save action. `diff_since_last()` therefore only ever compares the two most recent *scan-time* snapshots. Any change made and reverted on the bulletin between two scheduled scans produces no snapshot in between and is invisible to the system — this is intentional, not a gap to close later.

*Rationale:* this mirrors the real constraint the system is simulating — a customs bulletin is an external, arms-length source, and in reality you have no access to its internal edit history, only whatever is published at the moments you happen to check it. Capturing a snapshot on every dashboard edit would give the simulation more visibility than a real integration ever would, which would make the Regulatory Watch Agent's behavior artificially better than what it's meant to demonstrate. It also keeps the two write-paths clean (dashboard edits the bulletin; only the scheduler decides what counts as "checked"), which is the intended shape for the tool-calling showcase — the agent's schedule *is* its only view of the world. This is a deliberately accepted blind spot, bounded by the configured scan interval: a shorter interval narrows the window, but the miss-a-flicker behavior itself is permanent by design, not a bug to fix in a later phase.

- Run the scheduled job with zero bulletin changes → assert zero rows in `compliance.rules` and zero calls to the (mocked) Gmail client.
- Bulletin changed, then reverted, before the next scheduled scan captures a snapshot → `diff_since_last()` returns `None` (no snapshot exists for the transient state, since only the scheduler captures snapshots); assert zero rows in `compliance.rules` and zero emails for this case — this is the expected, documented behavior, not a failure.
- Mock `gmail_client.send` to raise `EmailDeliveryError` → assert the `pending` `ComplianceRule` row still commits and `audit.events` records the failed send attempt.
- No reviewer action taken for an extended period (simulate by just not calling `decide`) → rule stays `pending` indefinitely; assert no scheduled process ever auto-transitions it.
- Call `decide(..., "reject", ...)` then re-run the *unchanged* bulletin scan → assert no duplicate proposal is created (covered jointly with the Phase 4 case above; test it again here at the agent level, not just the service level).

## Phase 5 — `test_mcp_servers/`
- Simulate the Postgres connection dropping mid-call → MCP tool handler surfaces a typed MCP error response, not an unhandled exception that kills the server process.
- Feed a malformed argument (wrong type) to an MCP tool → Pydantic validation at the tool boundary rejects it with a clear schema-validation error before it reaches the service layer.
- Call every read tool on every server immediately after a direct service-layer write (same test, no server restart) → results match exactly, proving the shared service-layer means there is no caching layer to go stale.

## Phase 7 — `test_document_verification_agent.py`
- Shipment with only a `packing_list` attached (no invoice) → agent reports `missing_document` explicitly, does not attempt a quantity comparison against a nonexistent document.
- An amended invoice (`version=2`, `superseded_by` chain) → agent verification uses only the latest, non-superseded version.
- `currency` field containing an invalid ISO code (e.g. `"XYZ"`) → agent flags `invalid_currency`, does not pass it through to `fx_service.resolve_rate` (which would otherwise raise an unrelated-looking error).

## Phase 8 — `test_demurrage_risk_agent.py`
- Shipment already 5 days past `free_days` at analysis time → returned cost estimate equals `5 * daily_rate` converted at the port-charges-stage gazetted rate active on the analysis date; assert to the cent against a hand-computed fixture value.
- Compounding case: document mismatch flagged AND FX rate unresolved for the port-charges date → agent returns a partial result with an explicit `data_gaps` list rather than raising or silently omitting the missing piece.

## Phase 9 — `test_compliance_rag_agent.py`
- Query with no matching approved rule → agent responds with an explicit "not found in current rulebook" rather than a generated guess (test asserts the response contains no fabricated HS code/rate not present in the fixture rulebook).
- A rule with both an old `approved` version and a newer `pending` version for the same `rule_key` → retrieval returns only the approved version's content; a separate check confirms the pending version's `content` string is absent from the Chroma collection entirely (proves it was never embedded, not just filtered).

## Phase 10 — `test_clearance_checklist_agent.py`
- Simulate `demurrage_risk_agent` raising/timing out → checklist still generates from the other two agents' outputs, with a `missing_inputs: ["demurrage_risk"]` field, not a total failure.
- Two upstream agents produce contradictory urgency signals → checklist surfaces both explicitly (e.g. a `conflicts` list) rather than arbitrarily picking one.

## Phase 11 — `test_supervisor.py`
- Approve a compliance change, then call a (new) `revoke_decision` path → assert this creates a new, distinct audit event rather than mutating the original `decided_at`/`decided_by` fields in place.
- Concurrent `interrupt()` resumption: fire two decisions against two different pending interrupts for the same `run_id` in quick succession → assert the checkpointer correctly serializes/resumes both without cross-writing state.
- Attempt to advance a shipment to `cleared` while it has an unresolved `data_gaps` or `conflicts` entry from Phase 10 → gate blocks with a typed `IncompleteCaseError`, filing call never reaches the MCP layer.

## Phase 12 — `test_roi_calculator.py`
- Run the "before/after" comparison including one fixture shipment with zero issues either way → assert it contributes zero to the avoided-cost total in both runs (guards against an inflated ROI number).
- Freeze rate/rule inputs explicitly at the start of the comparison run (pass an explicit snapshot/version parameter) → mutate the live dashboard mid-run in the test → assert the in-flight comparison is unaffected (proves inputs were actually locked, not read live twice).

## Phase 13 — end-to-end / packaging
- Fresh native environment (new `.venv`, `scripts/setup_local_db.py`, `alembic upgrade head`), empty database → running only the documented seed command reaches a demoable state (no manual SQL, no hidden fixture step).
- Run the full JNPT demo scenario twice back-to-back without any reset step → assert the second run's `audit.events` are cleanly distinguishable by `run_id` and no state from run 1 leaks into run 2's checklist or ROI output.
