"""Mission Control — run the JNPT case study end to end, watch the audit trail live, and see the
before/after ROI of the agents (Phase 12).

* **Case study** runs the analysis pipeline (``graph.analyse_shipment``: verification →
  demurrage & risk ‖ compliance → checklist) for every shipment that is not cancelled, one
  ``run_id`` each, and lists the open supervised clearance runs (decided on Shipping Ops).
* **Audit trail** reads ``audit_service.query_events`` (filters: run, actor, action, entity),
  newest first, optionally refreshing itself every few seconds.
* **ROI** captures named, frozen snapshots of the data (``roi_service.capture_snapshot``) with
  the scenario assumptions, and compares two of them (``roi.calculator.compare``) — every
  figure behind the total is shown, so it can be rebuilt by hand from the Demurrage and FX
  Rates pages.
"""

import asyncio
import json
from typing import Any

import streamlit as st

from agents import graph
from agents.ist import today_ist
from agents.state import CaseState
from dashboards.ui import fmt_ist, run_action, show_flash
from models.enums import ShipmentStatus
from models.schemas import DEFAULT_HOLD_DAYS, AuditEventRecord, RoiAssumptions, ShipmentRecord
from roi.calculator import RoiComparison, RoiLine, compare
from services import audit_service, governance_service, roi_service, shipping_service
from services.db import session_scope
from services.errors import CustomsCopilotError

st.set_page_config(page_title="Mission Control", layout="wide")

ANY = "(any)"
COMBINE: list[str] = ["max", "sum"]
REFRESH_SECONDS = 5


# --------------------------------------------------------------------------- case study


def _analysis_row(shipment: ShipmentRecord, outcome: CaseState | str) -> dict[str, Any]:
    if isinstance(outcome, str):
        return {"Container": shipment.container_id, "Status": shipment.status.value,
                "Run": "—", "Documents": outcome, "Risk": "—", "Demurrage (INR)": "—",
                "Rulebook": "—", "Checklist": "—"}  # fmt: skip
    estimate = outcome.demurrage_estimate
    found = sum(f.found for f in outcome.compliance_findings)
    checklist = outcome.checklist
    codes = ", ".join(d.code for d in outcome.document_discrepancies) or "clear"
    return {
        "Container": shipment.container_id,
        "Status": shipment.status.value,
        "Run": outcome.run_id[:8],
        "Documents": codes,
        "Risk": outcome.risk or "—",
        "Demurrage (INR)": (
            "—" if estimate is None or estimate.amount_inr is None else f"{estimate.amount_inr:,}"
        ),
        "Rulebook": f"{found} answered, {len(outcome.compliance_findings) - found} not found",
        "Checklist": "—" if checklist is None else checklist.summary,
    }


def render_case_study() -> None:
    st.subheader("JNPT case study — every agent, every shipment")
    st.caption(
        "Runs the analysis pipeline for each shipment that is not cancelled (one run id each, "
        "today's IST date): document verification, then demurrage & risk and the compliance "
        "agent in parallel, then the clearance checklist. Read-only; every step is audited — "
        "watch it in the *Audit trail* tab. Takes ~30-60 s per shipment (MCP servers start, "
        "the rulebook models load, LLM calls)."
    )
    with session_scope() as session:
        shipments = [s for s in shipping_service.list_shipments(session)
                     if s.status is not ShipmentStatus.CANCELLED]  # fmt: skip
    if st.button(f"Analyse all {len(shipments)} shipments", key="analyse_all", type="primary"):
        results: list[tuple[ShipmentRecord, CaseState | str]] = []
        progress = st.progress(0.0, text="Starting...")
        for done, shipment in enumerate(shipments):
            progress.progress(done / len(shipments), text=f"Analysing {shipment.container_id}...")
            try:
                results.append((shipment, asyncio.run(graph.analyse_shipment(shipment.id))))
            except Exception as exc:  # one failure must not hide the other results
                results.append((shipment, f"failed: {type(exc).__name__}: {exc}"))
        progress.progress(1.0, text="Done.")
        st.session_state["mc_analysis"] = results
    analysed: list[tuple[ShipmentRecord, CaseState | str]] | None = st.session_state.get(
        "mc_analysis"
    )
    if analysed:
        st.dataframe([_analysis_row(s, o) for s, o in analysed], hide_index=True,
                     width="stretch")  # fmt: skip
        st.caption("Copy a run id into the *Audit trail* filter to see that run step by step.")

    st.subheader("Supervised clearance runs")
    with session_scope() as session:
        runs = governance_service.list_runs(session)[:20]
        containers = {s.id: s.container_id for s in shipping_service.list_shipments(session)}
    if not runs:
        st.info("No clearance runs yet — start one on Shipping Ops → a shipment → Clearance run.")
        return
    st.dataframe(
        [{"Container": containers.get(r.shipment_id, str(r.shipment_id)), "Run": r.run_id[:8],
          "State": r.state.value, "Open": "yes" if r.state.is_open else "",
          "Analysis date": r.as_of.isoformat(), "Updated": fmt_ist(r.updated_at),
          "Detail": r.detail or ""} for r in runs],
        hide_index=True, width="stretch",
    )  # fmt: skip
    st.caption("Runs waiting at a gate are decided and resumed on Shipping Ops → Clearance run.")


# --------------------------------------------------------------------------- audit trail


def _event_row(event: AuditEventRecord) -> dict[str, Any]:
    run = event.payload.get("run_id") or (
        str(event.entity_id) if event.entity_type == "agent_run" else ""
    )
    return {
        "When": fmt_ist(event.created_at),
        "Actor": event.actor,
        "Action": event.action,
        "Entity": f"{event.entity_type} {str(event.entity_id)[:8]}",
        "Run": str(run)[:8],
        "Payload": json.dumps(event.payload, ensure_ascii=False)[:300],
    }


def render_audit_trail() -> None:
    st.subheader("Audit trail")
    with session_scope() as session:
        actors: list[str] = [ANY, *audit_service.list_actors(session)]
        entity_types: list[str] = [ANY, *audit_service.list_entity_types(session)]
    c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 2, 1])
    run_id = c1.text_input("Run id (full)", "", key="audit_run").strip()
    actor = c2.selectbox("Actor", actors, key="audit_actor")
    prefix = c3.text_input("Action starts with", "", key="audit_action").strip()
    entity_type = c4.selectbox("Entity type", entity_types, key="audit_entity")
    limit = int(c5.number_input("Rows", min_value=10, max_value=1000, value=200, step=50,
                                key="audit_limit"))  # fmt: skip
    live = st.toggle(f"Refresh every {REFRESH_SECONDS} s", value=False, key="audit_live")

    def table() -> None:
        with session_scope() as session:
            events = audit_service.query_events(
                session,
                run_id=run_id or None,
                actor=None if actor == ANY else actor,
                action_prefix=prefix or None,
                entity_type=None if entity_type == ANY else entity_type,
                limit=limit,
            )
        st.caption(f"{len(events)} event(s), newest first.")
        if events:
            st.dataframe([_event_row(e) for e in events], hide_index=True, width="stretch")

    st.fragment(run_every=REFRESH_SECONDS if live else None)(table)()


# --------------------------------------------------------------------------- ROI


def render_capture() -> None:
    with st.form("roi_capture"):
        st.markdown("**Capture a snapshot** — freezes the shipments, their current documents, "
                    "the tariff and gazetted FX rate pricing each one, and the rulebook in "
                    "force, with these assumptions.")  # fmt: skip
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Name", "", key="roi_name")
        as_of = c2.date_input("Analysis date (FX)", today_ist(), key="roi_as_of")
        note = c3.text_input("Note", "", key="roi_note")
        c4, c5 = st.columns(2)
        base = int(c4.number_input("Days in port with papers in order", min_value=1,
                                   max_value=60, value=3, key="roi_base"))  # fmt: skip
        combine = c5.selectbox("Several issues", COMBINE, key="roi_combine",
                               format_func=lambda c: "longest hold (in parallel)" if c == "max"
                               else "holds add up (one after another)")  # fmt: skip
        st.markdown("Customs hold, in days, when an issue is found only at the port:")
        cols = st.columns(4)
        holds = {
            code: int(cols[i % 4].number_input(code, min_value=0, max_value=120, value=days,
                                                key=f"roi_hold_{code}"))
            for i, (code, days) in enumerate(DEFAULT_HOLD_DAYS.items())
        }  # fmt: skip
        submitted = st.form_submit_button("Capture snapshot", key="roi_capture_submit")
    if submitted:
        assumptions = RoiAssumptions(base_clearance_days=base, hold_days=holds,
                                     combine="sum" if combine == "sum" else "max")  # fmt: skip
        run_action(
            lambda session: roi_service.capture_snapshot(
                session, name, as_of, assumptions, "mission-control", note
            ),
            f"Captured ROI snapshot {name.strip()!r}.",
        )


def _line_row(line: RoiLine) -> dict[str, Any]:
    b, a = line.before, line.after
    return {
        "Container": line.container_id,
        "Issues (no agents)": ", ".join(line.issues) or "none",
        "Before: days / chargeable": "—" if b is None else f"{b.stay_days} / {b.chargeable_days}",
        "Before (INR)": "—" if b is None else f"{b.amount_inr:,}",
        "After: days / chargeable": "—" if a is None else f"{a.stay_days} / {a.chargeable_days}",
        "After (INR)": "—" if a is None else f"{a.amount_inr:,}",
        "Daily rate": "—" if b is None else f"{b.currency} {b.daily_rate}",
        "FX": "—" if b is None or b.fx_rate is None else str(b.fx_rate),
        "Avoided (INR)": f"{line.avoided_inr:,}",
        "Excluded": line.excluded or "",
    }


def show_comparison(result: RoiComparison) -> None:
    c1, c2, c3 = st.columns(3)
    c1.metric("Exposure without the agents (INR)", f"{result.total_before_inr:,}")
    c2.metric("Exposure with the agents (INR)", f"{result.total_after_inr:,}")
    c3.metric("Demurrage avoided (INR)", f"{result.total_avoided_inr:,}")
    st.dataframe([_line_row(line) for line in result.lines], hide_index=True, width="stretch")
    for item in result.excluded:
        st.warning(f"Not priced — {item}")
    with st.expander("How each figure was built"):
        for line in result.lines:
            if line.before is None or line.after is None:
                continue
            st.markdown(f"**{line.container_id}** — issues: {', '.join(line.issues) or 'none'}")
            st.text(f"before: {line.before.basis}\nafter:  {line.after.basis}\n"
                    f"avoided: INR {line.before.amount_inr} - {line.after.amount_inr} = "
                    f"INR {line.avoided_inr}")  # fmt: skip
            if line.before.fx_basis:
                st.caption(f"FX: {line.before.fx_basis}")
        st.json({"before snapshot": result.before_snapshot.model_dump(mode="json"),
                 "after snapshot": result.after_snapshot.model_dump(mode="json"),
                 "assumptions (before)": result.assumptions_before.model_dump()})  # fmt: skip


def render_roi() -> None:
    st.subheader("Before/after ROI")
    st.caption(
        "Without the agents a document issue is found only by customs at the port and holds the "
        "box; with them it is caught and fixed before the box docks. Both stays are priced with "
        "the tariff in force on the arrival day and the gazetted port-charges rate on the "
        "analysis date. The comparison reads only the frozen snapshots — edits made afterwards "
        "do not change it."
    )
    render_capture()
    with session_scope() as session:
        snapshots = roi_service.list_snapshots(session)
    if not snapshots:
        st.info("No snapshots yet — capture one above.")
        return
    st.dataframe(
        [{"Name": s.name, "Analysis date": s.as_of.isoformat(), "Captured": fmt_ist(s.captured_at),
          "By": s.captured_by, "Shipments": len(s.content.shipments),
          "Gaps": sum(bool(i.gaps) for i in s.content.shipments),
          "SHA-256": s.content_sha256[:12], "Note": s.note or ""} for s in snapshots],
        hide_index=True, width="stretch",
    )  # fmt: skip
    names: list[str] = [s.name for s in snapshots]
    c1, c2 = st.columns(2)
    before = c1.selectbox("Before (agents disabled)", names, key="roi_before")
    after = c2.selectbox("After (agents enabled)", names, key="roi_after")
    if st.button("Compare", key="roi_compare", type="primary"):
        by_name = {s.name: s for s in snapshots}
        try:
            st.session_state["roi_result"] = compare(by_name[before], by_name[after])
        except CustomsCopilotError as exc:
            st.error(f"{type(exc).__name__}: {exc}")
    result: RoiComparison | None = st.session_state.get("roi_result")
    if result is not None:
        show_comparison(result)


# --------------------------------------------------------------------------- page

st.title("Mission Control")
show_flash()
case_tab, audit_tab, roi_tab = st.tabs(["Case study", "Audit trail", "ROI"])
with case_tab:
    render_case_study()
with audit_tab:
    render_audit_trail()
with roi_tab:
    render_roi()
