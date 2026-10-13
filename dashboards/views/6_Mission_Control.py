"""Mission Control — run the JNPT case study end to end, watch the audit trail live, and see the
before/after saving of the agents (Phase 12).

* **Case study** runs the analysis pipeline (``graph.analyse_shipment``: verification →
  demurrage & risk ‖ compliance → checklist) for every shipment that is not cancelled, one
  ``run_id`` each, and lists the supervised clearance runs (decided on Shipping Ops).
* **Audit trail** reads ``audit_service.query_events`` (filters: run, actor, action, entity),
  newest first, optionally refreshing itself every few seconds.
* **Savings** captures named, frozen snapshots of the data (``roi_service.capture_snapshot``)
  with the scenario assumptions, and compares two of them (``roi.calculator.compare``); every
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
from dashboards import theme
from dashboards.ui import (
    ISSUE_NAMES,
    RISK_WORDS,
    RUN_STATE_WORDS,
    action_words,
    agent_name,
    entity_word,
    fmt_day,
    fmt_ist,
    issue_name,
    problem_text,
    run_action,
    status_word,
)
from models.enums import ShipmentStatus
from models.schemas import DEFAULT_HOLD_DAYS, AuditEventRecord, RoiAssumptions, ShipmentRecord
from roi.calculator import RoiComparison, RoiLine, compare
from services import audit_service, governance_service, roi_service, shipping_service
from services.db import session_scope
from services.errors import CustomsCopilotError

ANY = "(anyone)"
ANYTHING = "(anything)"
COMBINE: list[str] = ["max", "sum"]
REFRESH_SECONDS = 5

theme.setup_page(
    "Mission Control",
    "Run every agent over every shipment, watch each step land in the audit trail, and see "
    "what catching document problems before a box docks is worth.",
    icon=":material/monitoring:",
)


# --------------------------------------------------------------------------- case study


def _analysis_row(shipment: ShipmentRecord, outcome: CaseState | str) -> dict[str, Any]:
    if isinstance(outcome, str):
        return {"Container": shipment.container_id, "Stage": status_word(shipment.status),
                "Papers": outcome, "Risk": "-", "Demurrage so far (INR)": "-",
                "Rulebook": "-", "Ready to file": "-"}  # fmt: skip
    estimate = outcome.demurrage_estimate
    found = sum(f.found for f in outcome.compliance_findings)
    checklist = outcome.checklist
    issues = ", ".join(issue_name(d.code) for d in outcome.document_discrepancies)
    blockers = sum(i.severity == "blocker" for i in checklist.items) if checklist else 0
    if checklist is None:
        ready = "-"
    elif checklist.ready_for_clearance:
        ready = "Yes"
    else:
        ready = f"Not yet: {blockers} must-fix" if blockers else "Not yet"
    return {
        "Container": shipment.container_id,
        "Stage": status_word(shipment.status),
        "Papers": issues or "In order",
        "Risk": RISK_WORDS.get(outcome.risk or "", ("-", "quiet"))[0],
        "Demurrage so far (INR)": (
            "-" if estimate is None or estimate.amount_inr is None else f"{estimate.amount_inr:,}"
        ),
        "Rulebook": f"{found} answered, {len(outcome.compliance_findings) - found} not covered",
        "Ready to file": ready,
    }


def render_case_study() -> None:
    theme.section(
        "Analyse every shipment",
        "For each shipment the papers are checked first; then, side by side, its time in port "
        "is priced and the approved rulebook is asked; the checklist brings it together. "
        "Nothing is changed. About a minute per shipment.",
    )
    with session_scope() as session:
        shipments = [s for s in shipping_service.list_shipments(session)
                     if s.status is not ShipmentStatus.CANCELLED]  # fmt: skip
    c1, c2 = st.columns([1, 3], vertical_alignment="bottom")
    as_of = c1.date_input("Analysis date", today_ist(), key="analysis_as_of",
                          help="The demo data is priced for 2026 (customs FX rates to "
                          "31 Oct 2026). Pick 28 Sep 2026 for the figures in the demo script.")  # fmt: skip
    if c2.button(f"Analyse all {len(shipments)} shipments", key="analyse_all", type="primary"):
        results: list[tuple[ShipmentRecord, CaseState | str]] = []
        progress = st.progress(0.0, text="Starting...")
        for done, shipment in enumerate(shipments):
            progress.progress(done / len(shipments), text=f"Analysing {shipment.container_id}...")
            try:
                case = asyncio.run(graph.analyse_shipment(shipment.id, as_of=as_of))
                results.append((shipment, case))
            except Exception as exc:  # one failure must not hide the other results
                results.append((shipment, f"Could not finish: {problem_text(exc)}"))
        progress.empty()
        st.session_state["mc_analysis"] = results
    analysed: list[tuple[ShipmentRecord, CaseState | str]] | None = st.session_state.get(
        "mc_analysis"
    )
    if analysed:
        theme.table([_analysis_row(s, o) for s, o in analysed])
        with theme.expander("Run ids, to follow a run in the audit trail", key="run_ids_open"):
            for shipment, outcome in analysed:
                if isinstance(outcome, CaseState):
                    st.markdown(f"**{shipment.container_id}**")
                    st.code(outcome.run_id, language=None)

    theme.section("Supervised clearance runs",
                  "Runs that stop for a person: a rule decision, then the filing approval. "
                  "Decided on Shipping Ops.")  # fmt: skip
    with session_scope() as session:
        runs = governance_service.list_runs(session)[:20]
        containers = {s.id: s.container_id for s in shipping_service.list_shipments(session)}
    if not runs:
        st.info("No clearance runs yet. Start one on Shipping Ops, in a shipment's Clearance run.")
        return
    theme.table([
        {"Container": containers.get(r.shipment_id, str(r.shipment_id)),
         "Where it stands": RUN_STATE_WORDS.get(r.state.value, (r.state.value, "quiet"))[0],
         "Analysis date": fmt_day(r.as_of), "Last update": fmt_ist(r.updated_at),
         "Started by": r.started_by, "Note": r.detail or ""}
        for r in runs
    ])  # fmt: skip


# --------------------------------------------------------------------------- audit trail


def _event_row(event: AuditEventRecord) -> dict[str, Any]:
    run = event.payload.get("run_id") or (
        str(event.entity_id) if event.entity_type == "agent_run" else ""
    )
    return {
        "When": fmt_ist(event.created_at),
        "Who": agent_name(event.actor),
        "What happened": action_words(event.action),
        "About": entity_word(event.entity_type),
        "Run": str(run)[:8],
        "Details": json.dumps(event.payload, ensure_ascii=False)[:220],
    }


def render_audit_trail() -> None:
    theme.section("Audit trail",
                  "Every agent step and every human decision, newest first. Nothing in it can "
                  "be edited.")  # fmt: skip
    with session_scope() as session:
        actors: list[str] = [ANY, *audit_service.list_actors(session)]
        entity_types: list[str] = [ANYTHING, *audit_service.list_entity_types(session)]
    c1, c2, c3 = st.columns([2, 2, 1])
    actor = c1.selectbox("Who", actors, key="audit_actor",
                         format_func=lambda a: a if a == ANY else agent_name(a))  # fmt: skip
    entity_type = c2.selectbox("About", entity_types, key="audit_entity",
                               format_func=lambda e: e if e == ANYTHING else entity_word(e))  # fmt: skip
    limit = int(c3.number_input("Rows", min_value=10, max_value=1000, value=50, step=10,
                                key="audit_limit"))  # fmt: skip
    with theme.expander("Follow one run, or one kind of event", key="audit_filters_open"):
        f1, f2 = st.columns(2)
        run_id = f1.text_input("Run id (full)", "", key="audit_run",
                               help="From the case study's run ids.").strip()  # fmt: skip
        prefix = f2.text_input("Event code starts with", "", key="audit_action",
                               help="For example filing. or compliance_rule.").strip()  # fmt: skip
    live = st.toggle(f"Refresh every {REFRESH_SECONDS} seconds", value=False, key="audit_live")

    def table() -> None:
        with session_scope() as session:
            events = audit_service.query_events(
                session,
                run_id=run_id or None,
                actor=None if actor == ANY else actor,
                action_prefix=prefix or None,
                entity_type=None if entity_type == ANYTHING else entity_type,
                limit=limit,
            )
        st.caption(f"{len(events)} event(s).")
        theme.table([_event_row(e) for e in events], empty="No events match these filters.")

    st.fragment(run_every=REFRESH_SECONDS if live else None)(table)()


# --------------------------------------------------------------------------- savings


HOLD_LABELS = {code: ISSUE_NAMES.get(code, code) for code in DEFAULT_HOLD_DAYS}


def render_capture() -> None:
    with st.form("roi_capture"):
        st.markdown("**Freeze the data as it is now.** A snapshot keeps the shipments, their "
                    "papers, the tariff and customs rate that price each one, and the rules in "
                    "force, so a comparison can be re-run on exactly the same inputs.")  # fmt: skip
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Snapshot name", "", key="roi_name")
        as_of = c2.date_input("Analysis date", today_ist(), key="roi_as_of",
                              help="The customs exchange rate is taken on this date.")  # fmt: skip
        note = c3.text_input("Note (optional)", "", key="roi_note")
        st.markdown("**Assumptions**: how long a box stays in port.")
        c4, c5 = st.columns(2)
        base = int(c4.number_input("Days in port when the papers are in order", min_value=1,
                                   max_value=60, value=3, key="roi_base"))  # fmt: skip
        combine = c5.selectbox("When a box has several problems", COMBINE, key="roi_combine",
                               format_func=lambda c: "They are fixed at the same time"
                               if c == "max" else "They are fixed one after another")  # fmt: skip
        st.caption("Extra days customs holds a box when each problem is found only at the port:")
        cols = st.columns(4)
        holds = {
            code: int(cols[i % 4].number_input(HOLD_LABELS[code], min_value=0, max_value=120,
                                                value=days, key=f"roi_hold_{code}"))
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
        "Problems found late": ", ".join(issue_name(c) for c in line.issues) or "None",
        "Without agents: days / charged": (
            "-" if b is None else f"{b.stay_days} / {b.chargeable_days}"
        ),
        "Without (INR)": "-" if b is None else f"{b.amount_inr:,}",
        "With agents: days / charged": "-" if a is None else f"{a.stay_days} / {a.chargeable_days}",
        "With (INR)": "-" if a is None else f"{a.amount_inr:,}",
        "Daily rate": "-" if b is None else f"{b.currency} {b.daily_rate}",
        "Customs FX": "-" if b is None or b.fx_rate is None else str(b.fx_rate.normalize()),
        "Avoided (INR)": f"{line.avoided_inr:,}",
        "Not priced": line.excluded or "",
    }


def show_comparison(result: RoiComparison) -> None:
    with st.container(key=theme.focus_key("saving")):
        theme.figure(
            f"INR {result.total_avoided_inr:,} avoided",
            f"Demurrage the agents save on these shipments by catching problems before the "
            f"boxes dock. Priced as of {result.before_snapshot.as_of}.",
        )
        c1, c2, c3 = st.columns(3)
        c1.metric("Without the agents (INR)", f"{result.total_before_inr:,}")
        c2.metric("With the agents (INR)", f"{result.total_after_inr:,}")
        c3.metric("Demurrage avoided (INR)", f"{result.total_avoided_inr:,}")
    theme.table([_line_row(line) for line in result.lines])
    for item in result.excluded:
        st.warning(f"Not priced: {item}")
    with theme.expander("How each figure was built", key="roi_basis_open"):
        for line in result.lines:
            if line.before is None or line.after is None:
                continue
            st.markdown(f"**{line.container_id}**: problems found late: "
                        f"{', '.join(issue_name(c) for c in line.issues) or 'none'}")  # fmt: skip
            st.code(f"without agents  {line.before.basis}\nwith agents     {line.after.basis}\n"
                    f"avoided         INR {line.before.amount_inr} - {line.after.amount_inr} = "
                    f"INR {line.avoided_inr}", language=None)  # fmt: skip
            if line.before.fx_basis:
                st.caption(f"Customs rate used: {line.before.fx_basis}")
        st.caption("Snapshots compared (name and fingerprint, to re-run on the same inputs):")
        st.code(json.dumps({"before": result.before_snapshot.model_dump(mode="json"),
                            "after": result.after_snapshot.model_dump(mode="json"),
                            "assumptions": result.assumptions_before.model_dump()}, indent=2),
                language="json")  # fmt: skip


def render_savings() -> None:
    theme.section(
        "What catching problems early is worth",
        "Without the agents, a document problem is found by customs at the port and the box "
        "is held. With them, it is fixed before the box docks. Both stays are priced with the "
        "tariff and customs rate frozen in a snapshot, so later edits never change a result.",
    )
    render_capture()
    with session_scope() as session:
        snapshots = roi_service.list_snapshots(session)
    if not snapshots:
        st.info("No snapshots yet. Capture one above.")
        return
    theme.section("Snapshots")
    theme.table([
        {"Name": s.name, "Priced as of": fmt_day(s.as_of), "Captured": fmt_ist(s.captured_at),
         "By": s.captured_by, "Shipments": len(s.content.shipments),
         "Not priced": sum(bool(i.gaps) for i in s.content.shipments),
         "Fingerprint": s.content_sha256[:12], "Note": s.note or ""}
        for s in snapshots
    ])  # fmt: skip
    names: list[str] = [s.name for s in snapshots]
    c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom")
    before = c1.selectbox("Without the agents (before)", names, key="roi_before")
    after = c2.selectbox("With the agents (after)", names, key="roi_after")
    if c3.button("Compare", key="roi_compare", type="primary"):
        by_name = {s.name: s for s in snapshots}
        try:
            st.session_state["roi_result"] = compare(by_name[before], by_name[after])
        except CustomsCopilotError as exc:
            st.error(problem_text(exc))
    result: RoiComparison | None = st.session_state.get("roi_result")
    if result is not None:
        show_comparison(result)


# --------------------------------------------------------------------------- page

case_tab, audit_tab, savings_tab = theme.tabs(
    ["Case study", "Audit trail", "Savings"], key="mission_tabs"
)
with case_tab:
    render_case_study()
with audit_tab:
    render_audit_trail()
with savings_tab:
    render_savings()
