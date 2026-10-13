"""Shipping Ops — every shipment, its papers and its stage; check a shipment with the agents and
take it through supervised clearance (Phase 11).

Every read and write goes through ``services.shipping_service`` (the same functions
``shipping_records_mcp`` calls), so the dashboard and the agents can never disagree about the
data. Nothing is deleted: shipments are cancelled/closed via status, documents are amended into
new versions. Widget keys are suffixed with the container id so switching shipments re-seeds
each form from the database.

A shipment is never moved to ``cleared`` here by hand: only a clearance run's filing gate, approved
by a registered reviewer (sidebar), files it (``agents.supervisor``).
"""

import asyncio
from datetime import date, datetime, time
from typing import Any

import streamlit as st

from agents import document_verification_agent, graph, supervisor
from agents.ist import today_ist
from agents.state import CaseState, ChecklistItem, Discrepancy
from dashboards import theme
from dashboards.ui import (
    RISK_WORDS,
    RUN_STATE_WORDS,
    SEVERITY_WORDS,
    agent_name,
    flash,
    fmt_day,
    fmt_ist,
    fmt_ist_day,
    issue_name,
    ist_to_utc,
    problem_text,
    run_action,
    split_ist,
    status_word,
    to_decimal,
)
from models.enums import ContainerType, DocType, FxStage, RuleStatus, RunState, ShipmentStatus
from models.schemas import (
    CaseRunRecord,
    DocumentAmend,
    DocumentCreate,
    DocumentRecord,
    ResolvedRate,
    ShipmentCreate,
    ShipmentRecord,
    ShipmentUpdate,
)
from services import compliance_service, governance_service
from services import shipping_service as svc
from services.db import session_scope
from services.errors import CustomsCopilotError, ShipmentCancelledError

# Explicitly typed option lists: inline literals make mypy pick Streamlit's `-> None` overload.
CONTAINER_TYPES: list[ContainerType] = list(ContainerType)
DOC_TYPES: list[DocType] = list(DocType)
START_STATUSES: list[ShipmentStatus] = [ShipmentStatus.QUOTED, ShipmentStatus.BOOKED]
STATUS_FILTERS: list[str] = ["all", *(s.value for s in ShipmentStatus)]
REVIEWERS: list[str] = [r.name for r in governance_service.reviewers()]
_OPEN_NEXT = "_open_container_next_run"
TABLE_OPEN_UP_TO = 6  # shipments; a longer list starts folded

DOC_WORDS = {
    DocType.INVOICE: "Commercial invoice",
    DocType.PACKING_LIST: "Packing list",
    DocType.BILL_OF_LADING: "Bill of lading",
    DocType.CERTIFICATE: "Certificate",
    DocType.OTHER: "Other document",
}
FIELD_WORDS = {
    "container_type": "container type",
    "cargo_type": "cargo",
    "hs_code": "HS code",
    "declared_currency": "currency",
    "declared_qty": "declared qty",
    "declared_value": "declared value",
    "origin_port": "origin port",
    "destination_port": "destination port",
    "etd": "departure",
    "eta": "arrival",
    "quote_date": "quote date",
    "quote_validity_days": "quote validity",
    "bol_date": "bill of lading date",
    "invoice_date": "invoice date",
}
STAGE_WORDS = {
    FxStage.QUOTATION: "Quotation",
    FxStage.BOL: "Bill of lading",
    FxStage.INVOICE: "Invoice",
    FxStage.PORT_CHARGES: "Port charges",
}
ROUTE: list[ShipmentStatus] = [
    ShipmentStatus.QUOTED,
    ShipmentStatus.BOOKED,
    ShipmentStatus.IN_TRANSIT,
    ShipmentStatus.DOCKED,
    ShipmentStatus.UNDER_REVIEW,
    ShipmentStatus.CLEARED,
    ShipmentStatus.CLOSED,
]

theme.setup_page(
    "Shipping Ops",
    "Every shipment to Nhava Sheva, its papers and where it is on its journey. Check a "
    "shipment with the agents, then take it through clearance.",
    icon=":material/directions_boat:",
)


# --------------------------------------------------------------------------- tables


def _shipment_row(s: ShipmentRecord) -> dict[str, Any]:
    return {
        "Container": s.container_id,
        "Type": s.container_type.value,
        "Stage": status_word(s.status),
        "Cargo": s.cargo_type,
        "HS code": s.hs_code,
        "Qty": str(s.declared_qty),
        "Value": f"{s.declared_value:,} {s.declared_currency}",
        "Route": f"{s.origin_port} to {s.destination_port}",
        "Departed": fmt_ist_day(s.etd),
        "Arrives": fmt_ist_day(s.eta),
        "Quote valid until": fmt_day(s.quote_expiry_date),
    }


def _document_row(d: DocumentRecord) -> dict[str, Any]:
    return {
        "Document": DOC_WORDS.get(d.doc_type, d.doc_type.value),
        "Version": d.version,
        "Qty": "-" if d.qty is None else str(d.qty),
        "Value": "-" if d.value is None else f"{d.value:,} {d.currency or ''}".strip(),
        "Issued": fmt_day(d.issued_date),
        "Current": "Yes" if d.superseded_by is None else "Replaced",
    }


def _doc_label(d: DocumentRecord) -> str:
    return f"{DOC_WORDS.get(d.doc_type, d.doc_type.value)} v{d.version} (issued {d.issued_date})"


# --------------------------------------------------------------------------- details & stage


def _optional_datetime(label: str, current: datetime | None, key: str) -> datetime | None:
    day_default, time_default = split_ist(current) if current else (None, time(0, 0))
    col_day, col_time = st.columns(2)
    day = col_day.date_input(f"{label} date (IST)", value=day_default, key=f"{key}_date")
    at = col_time.time_input(f"{label} time (IST)", value=time_default, key=f"{key}_time")
    return ist_to_utc(day, at) if day else None


def render_edit_form(s: ShipmentRecord, actor: str) -> None:
    cid = s.container_id
    with st.form(f"edit_{cid}"):
        st.markdown("**Edit the booking**")
        c1, c2, c3 = st.columns(3)
        values: dict[str, Any] = {
            "container_type": c1.selectbox(
                "Container type",
                CONTAINER_TYPES,
                index=CONTAINER_TYPES.index(s.container_type),
                format_func=lambda x: x.value,
                key=f"edit_container_type_{cid}",
            ),
            "cargo_type": c1.text_input("Cargo", s.cargo_type, key=f"edit_cargo_type_{cid}"),
            "hs_code": c2.text_input("HS code", s.hs_code, key=f"edit_hs_code_{cid}"),
            "declared_currency": c3.text_input(
                "Currency (ISO 4217)", s.declared_currency, key=f"edit_currency_{cid}"
            ),
            "declared_qty": to_decimal(
                c1.number_input(
                    "Declared qty",
                    min_value=0.0,
                    value=float(s.declared_qty),
                    key=f"edit_declared_qty_{cid}",
                )
            ),
            "declared_value": to_decimal(
                c2.number_input(
                    "Declared value",
                    min_value=0.0,
                    value=float(s.declared_value),
                    format="%.2f",
                    key=f"edit_declared_value_{cid}",
                )
            ),
            "origin_port": c1.text_input("Origin port", s.origin_port, key=f"edit_origin_{cid}"),
            "destination_port": c2.text_input(
                "Destination port", s.destination_port, key=f"edit_destination_{cid}"
            ),
        }
        values["etd"] = _optional_datetime("Departure", s.etd, f"edit_etd_{cid}")
        values["eta"] = _optional_datetime("Arrival (clear if unknown)", s.eta, f"edit_eta_{cid}")
        q1, q2, q3, q4 = st.columns(4)
        values["quote_date"] = q1.date_input("Quote date", s.quote_date, key=f"edit_qd_{cid}")
        values["quote_validity_days"] = q2.number_input(
            "Quote valid for (days)",
            min_value=7,
            max_value=14,
            value=s.quote_validity_days,
            step=1,
            key=f"edit_qv_{cid}",
        )
        values["bol_date"] = q3.date_input("On-board date", s.bol_date, key=f"edit_bol_{cid}")
        values["invoice_date"] = q4.date_input(
            "Invoice date", s.invoice_date, key=f"edit_inv_{cid}"
        )
        submitted = st.form_submit_button("Save changes", key=f"save_{cid}")

    if submitted:
        current = s.model_dump()
        changed = {k: v for k, v in values.items() if v != current[k]}
        if not changed:
            st.info("Nothing changed.")
            return
        run_action(
            lambda session: svc.update_shipment(
                session, s.id, ShipmentUpdate.model_validate(changed), actor
            ),
            f"Saved {cid}: {', '.join(sorted(FIELD_WORDS.get(k, k) for k in changed))}.",
        )


def render_status_controls(s: ShipmentRecord, actor: str) -> None:
    cid = s.container_id
    allowed = sorted(svc.allowed_transitions(s.status) - {ShipmentStatus.CLEARED})
    st.markdown("**Move to the next stage**")
    if ShipmentStatus.CLEARED in svc.allowed_transitions(s.status):
        st.caption("Cleared is reached only by filing, on the Clearance tab.")
    if not allowed:
        st.caption(f"Nothing to move to from {status_word(s.status).lower()}.")
        return
    col_pick, col_go = st.columns([3, 1], vertical_alignment="bottom")
    target = col_pick.selectbox(
        f"From {status_word(s.status).lower()} to",
        allowed,
        format_func=status_word,
        key=f"next_status_{cid}",
    )
    if col_go.button("Move", key=f"apply_status_{cid}"):
        run_action(
            lambda session: svc.update_status(session, s.id, target, actor),
            f"{cid} moved from {status_word(s.status).lower()} to {status_word(target).lower()}.",
        )


# --------------------------------------------------------------------------- papers


def render_documents(s: ShipmentRecord, actor: str) -> None:
    cid = s.container_id
    show_history = st.checkbox("Show replaced versions", key=f"doc_history_{cid}")
    with session_scope() as session:
        docs = svc.get_documents(session, s.id, include_superseded=show_history)
    theme.table([_document_row(d) for d in docs], empty="No papers attached yet.")

    add_col, amend_col = st.columns(2)
    with add_col, st.form(f"add_doc_{cid}", clear_on_submit=True):
        st.markdown("**Attach a document**")
        doc_type = st.selectbox(
            "Document", DOC_TYPES, format_func=lambda x: DOC_WORDS.get(x, x.value),
            key=f"add_doc_type_{cid}",
        )  # fmt: skip
        qty = st.number_input("Qty", min_value=0.0, value=None, key=f"add_doc_qty_{cid}")
        value = st.number_input(
            "Value", min_value=0.0, value=None, format="%.2f", key=f"add_doc_value_{cid}"
        )
        currency = st.text_input("Currency (blank if none)", key=f"add_doc_currency_{cid}")
        issued = st.date_input("Issued on", value=date.today(), key=f"add_doc_issued_{cid}")
        if st.form_submit_button("Attach", key=f"add_doc_submit_{cid}"):
            data = {
                "doc_type": doc_type,
                "qty": to_decimal(qty),
                "value": to_decimal(value),
                "currency": currency.strip() or None,
                "issued_date": issued,
            }
            run_action(
                lambda session: svc.add_document(
                    session, s.id, DocumentCreate.model_validate(data), actor
                ),
                f"Attached a {DOC_WORDS.get(doc_type, doc_type.value).lower()} to {cid}.",
            )

    current_docs = [d for d in docs if d.superseded_by is None]
    with amend_col:
        if not current_docs:
            return
        st.markdown("**Amend a document** (keeps the old version)")
        # The picker sits outside the form: a form only reruns on submit, so a picker inside it
        # would leave the fields showing (and submitting) the previous document's values.
        by_label = {_doc_label(d): d for d in current_docs}
        picked = by_label[st.selectbox("Document", list(by_label), key=f"amend_pick_{cid}")]
        with st.form(f"amend_doc_{cid}_{picked.id}"):
            new_qty = st.number_input(
                "Qty",
                min_value=0.0,
                value=None if picked.qty is None else float(picked.qty),
                key=f"amend_qty_{cid}_{picked.id}",
            )
            new_value = st.number_input(
                "Value",
                min_value=0.0,
                value=None if picked.value is None else float(picked.value),
                format="%.2f",
                key=f"amend_value_{cid}_{picked.id}",
            )
            new_currency = st.text_input(
                "Currency", picked.currency or "", key=f"amend_currency_{cid}_{picked.id}"
            )
            new_issued = st.date_input(
                "Issued on", picked.issued_date, key=f"amend_issued_{cid}_{picked.id}"
            )
            if st.form_submit_button("Issue amended version", key=f"amend_submit_{cid}"):
                proposed = {
                    "qty": to_decimal(new_qty),
                    "value": to_decimal(new_value),
                    "currency": new_currency.strip() or None,
                    "issued_date": new_issued,
                }
                changed = {k: v for k, v in proposed.items() if v != getattr(picked, k)}
                if not changed:
                    st.info("Nothing changed, so nothing to amend.")
                else:
                    run_action(
                        lambda session: svc.amend_document(
                            session, picked.id, DocumentAmend.model_validate(changed), actor
                        ),
                        f"Issued version {picked.version + 1} of the "
                        f"{DOC_WORDS.get(picked.doc_type, picked.doc_type.value).lower()} "
                        f"for {cid}.",
                    )


# --------------------------------------------------------------------------- analysis


def _discrepancy_row(d: Discrepancy) -> dict[str, Any]:
    return {
        "Problem": issue_name(d.code),
        "What we found": d.message,
        "Difference": "-" if d.delta is None else str(d.delta),
    }


def _rate_row(r: ResolvedRate) -> dict[str, Any]:
    stage = STAGE_WORDS.get(r.stage, r.stage.value)
    if r.invoice_kind is not None:
        stage = f"{r.invoice_kind.value.capitalize()} invoice"
    return {
        "Stage": stage,
        "Date": fmt_day(r.as_of),
        "Rate": f"{r.pair} {r.rate.normalize()}",
        "How it was chosen": r.basis,
        "Valid until": fmt_day(r.expires) if r.expires else "-",
    }


def render_verification(s: ShipmentRecord) -> None:
    """Run the Document Verification agent (over its MCP servers) and show what it found."""
    cid = s.container_id
    with theme.expander("Check the papers only (quicker)", key=f"verify_open_{cid}"):
        st.caption(
            "The document check alone: compares the invoice, packing list and bill of lading "
            "with each other and with the booking, and finds the exchange rate for each date. "
            "Nothing is changed."
        )
        if st.button("Check papers", key=f"verify_{cid}"):
            try:
                with st.spinner("Checking the papers..."):
                    st.session_state[f"verification_{cid}"] = asyncio.run(
                        document_verification_agent.verify_shipment(s.id)
                    )
            except ShipmentCancelledError as exc:
                st.warning(f"Check stopped: {problem_text(exc)}")
            except Exception as exc:  # show any failure to the operator
                st.error(f"The check could not finish: {problem_text(exc)}")
        state: CaseState | None = st.session_state.get(f"verification_{cid}")
        if state is None:
            return
        explanation = state.document_explanation
        summary = explanation.text if explanation else ""
        heading = f"As of {fmt_day(state.as_of)}: "
        if state.document_outcome == "clear":
            st.success(heading + summary)
        else:
            st.warning(heading + summary)
            theme.table([_discrepancy_row(d) for d in state.document_discrepancies])
        fx = state.fx_context
        if fx is not None and fx.rates:
            st.markdown("**Exchange rates the papers convert at**")
            theme.table([_rate_row(r) for r in fx.rates])
        elif fx is not None and fx.skipped_reason:
            st.caption(f"Exchange rates: {fx.skipped_reason}")


def _show_risk(state: CaseState) -> None:
    words, _ = RISK_WORDS.get(state.risk or "", ("Risk unknown", "quiet"))
    reasons = " ".join(r if r.endswith(".") else f"{r}." for r in state.risk_reasons)
    heading = f"{words} as of {fmt_day(state.as_of)}."
    if state.risk in ("high", "medium"):
        st.warning(f"{heading} {reasons}")
    elif state.risk == "none":
        st.success(f"{heading} {reasons}")
    else:
        st.info(f"{heading} {reasons}")
    e = state.demurrage_estimate
    if e is not None:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Days in port", e.days_in_port)
        c2.metric("Days charged", e.chargeable_days)
        c3.metric(f"Demurrage ({e.currency})", f"{e.amount:,}")
        c4.metric("Demurrage (INR)", "-" if e.amount_inr is None else f"{e.amount_inr:,}")
        st.caption(e.basis)
    for gap in state.data_gaps:
        st.warning(f"Missing data: {gap}")


GAP_WORDS = {"tariff": "port tariff", "eta": "arrival date", "fx_rate": "customs exchange rate"}


def _item_what(item: ChecklistItem) -> str:
    """A checklist item's title in the words the rest of the page uses."""
    if item.category == "document":
        return issue_name(item.title.replace(" ", "_"))
    if item.category == "data_gap":
        gap = item.title.split(": ", 1)[-1]
        return f"Missing data: {GAP_WORDS.get(gap, gap)}"
    if item.category == "demurrage":
        level = item.title.rsplit(" ", 1)[-1]
        return f"Demurrage: {RISK_WORDS.get(level, (level, 'quiet'))[0].lower()}"
    return "Rulebook answer" if item.title.endswith("answered") else "Not in the rulebook"


def _item_row(item: ChecklistItem) -> dict[str, Any]:
    return {
        "Priority": SEVERITY_WORDS.get(item.severity, item.severity),
        "What": _item_what(item),
        "Detail": item.detail,
        "Rulebook says": item.guidance or "",
        "Rules and data used": ", ".join(item.references),
        "Found by": agent_name(item.source_agent),
    }


def _checklist_sentence(state: CaseState) -> str:
    checklist = state.checklist
    assert checklist is not None
    counts = {k: sum(i.severity == k for i in checklist.items) for k in SEVERITY_WORDS}
    parts = f"{counts['blocker']} must-fix, {counts['action']} to do, {counts['info']} notes"
    verdict = "Ready to file" if checklist.ready_for_clearance else "Not ready to file yet"
    return f"{verdict}: {parts}."


def _show_checklist(state: CaseState) -> None:
    checklist = state.checklist
    if checklist is None:
        return
    st.markdown("**Clearance checklist**")
    if checklist.ready_for_clearance:
        st.success(_checklist_sentence(state))
    else:
        st.info(_checklist_sentence(state))
    for missing in checklist.missing_inputs:
        failure = next((f for f in state.agent_failures if f.agent.startswith(missing)), None)
        reason = f" ({failure.error})" if failure else ""
        st.warning(f"{agent_name(missing + '_agent')} did not finish{reason}; the checklist is "
                   "built without it.")  # fmt: skip
    for conflict in checklist.conflicts:
        st.warning(f"The agents disagree: {conflict.description} " + " ".join(conflict.signals))
    theme.table([_item_row(i) for i in checklist.items])


def render_risk(s: ShipmentRecord) -> None:
    """Run the whole pipeline and show the risk and the clearance checklist."""
    cid = s.container_id
    st.caption(
        "Every agent in turn: the papers are checked; then, side by side, the time in port is "
        "priced (tariff on the arrival day, the arrival day counts as day one, customs rate on "
        "today's date) and the approved rulebook is asked; the checklist brings it together. "
        "Nothing is changed. About a minute."
    )
    if st.button("Analyse shipment", key=f"assess_{cid}", type="primary"):
        try:
            with st.spinner("The agents are working..."):
                st.session_state[f"risk_{cid}"] = asyncio.run(graph.analyse_shipment(s.id))
        except ShipmentCancelledError as exc:
            st.warning(f"Analysis stopped: {problem_text(exc)}")
        except Exception as exc:  # show any failure to the operator
            st.error(f"The analysis could not finish: {problem_text(exc)}")
    state: CaseState | None = st.session_state.get(f"risk_{cid}")
    if state is not None:
        _show_risk(state)
        _show_checklist(state)


# --------------------------------------------------------------------------- clearance runs


def _run_row(run: CaseRunRecord) -> dict[str, Any]:
    return {
        "Started": fmt_ist(run.started_at),
        "Where it stands": RUN_STATE_WORDS.get(run.state.value, (run.state.value, "quiet"))[0],
        "Analysis date": fmt_day(run.as_of),
        "Started by": run.started_by,
        "Last update": fmt_ist(run.updated_at),
        "Note": run.detail or "",
    }


def _drive(label: str, action: Any) -> None:
    """Run one supervisor call (async; opens the MCP servers), then rerun to show the result."""
    try:
        with st.spinner(label):
            result = asyncio.run(action)
    except CustomsCopilotError as exc:
        st.error(problem_text(exc))
    except Exception as exc:  # show any failure to the operator
        st.error(f"The clearance run could not continue: {problem_text(exc)}")
    else:
        words = RUN_STATE_WORDS.get(result.run.state.value, (result.run.state.value, "quiet"))[0]
        flash(f"{words}. {result.run.detail or ''}".strip())
        st.rerun()


def _render_compliance_wait(run: CaseRunRecord) -> None:
    with st.container(key=theme.focus_key(f"compliance_{run.run_id}")):
        st.markdown("#### Waiting for a rule decision")
        st.markdown("A proposed rule change covers these goods. A reviewer decides it on the "
                    "Compliance page; then resume the run here.")  # fmt: skip
        with session_scope() as session:
            rules = [compliance_service.get_rule(session, rule_id)
                     for rule_id in run.awaiting_rule_ids]  # fmt: skip
        for rule in rules:
            col_rule, col_go = st.columns([3, 1], vertical_alignment="center")
            tone: theme.Tone = "warn" if rule.status is RuleStatus.PENDING else "ok"
            col_rule.markdown(f"{rule.rule_key}, version {rule.version} "
                              + theme.chip(rule.status.value.capitalize(), tone),
                              unsafe_allow_html=True)  # fmt: skip
            if rule.status is RuleStatus.PENDING:
                continue
            if col_go.button("Resume run", key=f"resume_{run.run_id}_{rule.id}", type="primary"):
                _drive("Resuming the run and asking the rulebook again...",
                       supervisor.resume_after_compliance_decision(run.run_id, rule.id))  # fmt: skip


def _render_filing_wait(run: CaseRunRecord, reviewer: str | None) -> None:
    with st.container(key=theme.focus_key(f"filing_{run.run_id}")):
        st.markdown("#### Ready to file: waiting for approval")
        st.success("The checklist is ready. Approving files the shipment and moves it to "
                   "cleared; rejecting ends this run for good.")  # fmt: skip
        try:
            case = asyncio.run(supervisor.inspect_run(run.run_id)).case
        except Exception as exc:  # show any failure to the operator
            st.error(f"Could not read the run: {problem_text(exc)}")
            return
        _show_checklist(case)
        if reviewer is None:
            st.warning("Choose yourself as Reviewer in the sidebar to decide.")
            return
        note = st.text_input("Note (required to reject)", "", key=f"filing_note_{run.run_id}")
        b1, b2, _ = st.columns([1, 1, 3])
        if b1.button("Approve filing", key=f"approve_filing_{run.run_id}", type="primary"):
            _drive("Filing the shipment...",
                   supervisor.resume_after_filing_approval(run.run_id, "approve", reviewer, note))  # fmt: skip
        if b2.button("Reject filing", key=f"reject_filing_{run.run_id}"):
            _drive("Recording the rejection...",
                   supervisor.resume_after_filing_approval(run.run_id, "reject", reviewer, note))  # fmt: skip


def _render_abandon(run: CaseRunRecord, reviewer: str | None) -> None:
    with theme.expander("Abandon this run", key=f"abandon_open_{run.run_id}"):
        st.caption("For a run left running by a closed window, or one that should not go on. "
                   "It ends, and the shipment can start a new run.")  # fmt: skip
        reason = st.text_input("Reason", "", key=f"abandon_reason_{run.run_id}")
        if st.button("Abandon run", key=f"abandon_{run.run_id}", disabled=reviewer is None):
            try:
                supervisor.abandon_clearance_run(run.run_id, reviewer or "", reason)
            except CustomsCopilotError as exc:
                st.error(problem_text(exc))
            else:
                flash("Run abandoned.")
                st.rerun()


def render_clearance(s: ShipmentRecord, actor: str, reviewer: str | None) -> None:
    """Supervised clearance runs: the pipeline behind the compliance and filing gates."""
    cid = s.container_id
    st.caption(
        "A clearance run checks the shipment with every agent, then stops for people: first "
        "for a decision on any pending rule change that covers its goods (Compliance page), "
        "then, if the checklist is ready and the box is docked or under review, for a reviewer "
        "to approve the filing. Each wait survives closing the browser."
    )
    with session_scope() as session:
        runs = governance_service.list_runs(session, s.id)
    open_run = next((r for r in runs if r.state.is_open), None)
    if open_run is None:
        if s.status is ShipmentStatus.CANCELLED:
            st.caption("Cancelled shipments are not cleared.")
        else:
            col_date, col_go = st.columns([1, 3], vertical_alignment="bottom")
            as_of = col_date.date_input("Analysis date", today_ist(),
                                        key=f"clearance_as_of_{cid}")  # fmt: skip
            if col_go.button("Start clearance run", key=f"clearance_start_{cid}", type="primary"):
                _drive("The agents are checking the shipment (about a minute)...",
                       supervisor.start_clearance_run(s.id, started_by=actor, as_of=as_of))  # fmt: skip
    elif open_run.state is RunState.AWAITING_COMPLIANCE:
        _render_compliance_wait(open_run)
    elif open_run.state is RunState.AWAITING_FILING:
        _render_filing_wait(open_run, reviewer)
    else:
        st.warning("A run for this shipment is still running: in progress elsewhere, or "
                   "interrupted when a window closed.")  # fmt: skip
    if open_run is not None:
        _render_abandon(open_run, reviewer)
    if runs:
        st.markdown("**Runs for this shipment**")
        theme.table([_run_row(r) for r in runs])


# --------------------------------------------------------------------------- new shipment


def render_create_form(actor: str) -> None:
    with st.form("create_shipment", clear_on_submit=False):
        st.markdown("**Register a shipment** as a quotation or a booking.")
        c1, c2, c3 = st.columns(3)
        data: dict[str, Any] = {
            "container_id": c1.text_input(
                "Container number (e.g. MSKU1234567)", key="new_container"
            ),
            "status": c2.selectbox(
                "Starts as",
                START_STATUSES,
                format_func=status_word,
                key="new_status",
            ),
            "container_type": c1.selectbox(
                "Container type (sets the demurrage tariff)",
                CONTAINER_TYPES,
                format_func=lambda x: x.value,
                key="new_container_type",
            ),
            "cargo_type": c3.text_input("Cargo", key="new_cargo_type"),
            "hs_code": c1.text_input("HS code (digits)", key="new_hs_code"),
            "declared_qty": to_decimal(
                c2.number_input("Declared qty", min_value=0.0, key="new_qty")
            ),
            "declared_value": to_decimal(
                c3.number_input("Declared value", min_value=0.0, format="%.2f", key="new_value")
            ),
            "declared_currency": c1.text_input("Currency (ISO 4217)", "USD", key="new_currency"),
            "origin_port": c2.text_input("Origin port (UN/LOCODE)", key="new_origin"),
            "destination_port": c3.text_input("Destination port", "INNSA", key="new_destination"),
        }
        data["etd"] = _optional_datetime("Departure", None, "new_etd")
        data["eta"] = _optional_datetime("Arrival (optional)", None, "new_eta")
        q1, q2 = st.columns(2)
        data["quote_date"] = q1.date_input(
            "Quote date (needed for a quotation)", value=None, key="new_quote_date"
        )
        data["quote_validity_days"] = q2.number_input(
            "Quote valid for (7-14 days)",
            min_value=7,
            max_value=14,
            value=None,
            step=1,
            key="new_quote_validity",
        )
        submitted = st.form_submit_button("Create shipment", key="create_submit")

    if submitted:
        if data["etd"] is None:
            st.error("Please give the departure date.")
            return
        payload = {k: v for k, v in data.items() if v is not None and v != ""}
        run_action(
            lambda session: svc.create_shipment(
                session, ShipmentCreate.model_validate(payload), actor
            ),
            f"Created {data['container_id']}.",
            then=lambda: st.session_state.update({_OPEN_NEXT: data["container_id"]}),
        )


# --------------------------------------------------------------------------- page


def render_shipment(shipment: ShipmentRecord, actor: str, reviewer: str | None) -> None:
    cid = shipment.container_id
    theme.section(cid, f"{shipment.cargo_type}, HS {shipment.hs_code}, {shipment.container_type.value} "
                       f"container from {shipment.origin_port} to {shipment.destination_port}.")  # fmt: skip
    here = ROUTE.index(shipment.status) if shipment.status in ROUTE else None
    if shipment.status is ShipmentStatus.HELD:
        here = ROUTE.index(ShipmentStatus.DOCKED)
    theme.route([(status_word(s), None) for s in ROUTE], here=here)
    if shipment.status in (ShipmentStatus.HELD, ShipmentStatus.CANCELLED):
        tone: theme.Tone = "warn" if shipment.status is ShipmentStatus.HELD else "quiet"
        theme.chips([(status_word(shipment.status), tone)])
    analysis, clearance, papers, details = theme.tabs(
        ["Analysis", "Clearance", "Papers", "Details"], key=f"shipment_tabs_{cid}"
    )
    with analysis:
        if shipment.status is ShipmentStatus.CANCELLED:
            st.caption("Cancelled shipments are not analysed.")
        else:
            render_risk(shipment)
            render_verification(shipment)
    with clearance:
        render_clearance(shipment, actor, reviewer)
    with papers:
        render_documents(shipment, actor)
    with details:
        render_status_controls(shipment, actor)
        render_edit_form(shipment, actor)


def render_shipments(actor: str, reviewer: str | None) -> None:
    status_filter = st.selectbox(
        "Show", STATUS_FILTERS, key="status_filter",
        format_func=lambda v: "All shipments" if v == "all" else status_word(v),
    )  # fmt: skip
    status = None if status_filter == "all" else ShipmentStatus(status_filter)
    with session_scope() as session:
        shipments = svc.list_shipments(session, status)
    if not shipments:
        st.info("No shipments here. Create one on the New shipment tab, or load the demo data "
                "with python -m scripts.seed.")  # fmt: skip
        return

    by_container = {s.container_id: s for s in shipments}
    selected = st.selectbox(
        "Open shipment (type to search by container or cargo)",
        list(by_container),
        key="selected_container",
        format_func=lambda cid: _pick_label(by_container[cid]),
    )
    # The list sits under the picker, folded once it is long, so a shipment opens near the top.
    with theme.expander(f"All {len(shipments)} shipment(s) as a table", key="ship_table_open",
                        expanded=len(shipments) <= TABLE_OPEN_UP_TO):  # fmt: skip
        theme.table([_shipment_row(s) for s in shipments])
    render_shipment(by_container[selected], actor, reviewer)


def _pick_label(s: ShipmentRecord) -> str:
    # Nothing that changes as the shipment moves (its stage): Streamlit keys a select box by its
    # option labels, so a changed label would reset the picker to the first shipment.
    return f"{s.container_id} · {s.cargo_type}"


# A shipment just created opens on the Shipments tab (set before those widgets exist).
if (opened := st.session_state.pop(_OPEN_NEXT, None)) is not None:
    st.session_state.update(ops_tabs="Shipments", status_filter="all", selected_container=opened)

operator = st.sidebar.text_input("Your name (for the audit trail)", "ops-dashboard", key="actor")
filing_reviewer: str | None = st.sidebar.selectbox(
    "Reviewer (for filing decisions)", REVIEWERS, index=None, key="reviewer",
    placeholder="Choose a registered reviewer",
)  # fmt: skip
if not REVIEWERS:
    st.sidebar.warning("No reviewers are set up. Add REVIEWERS to .env.")
tab_list, tab_new = theme.tabs(["Shipments", "New shipment"], key="ops_tabs")
with tab_list:
    render_shipments(operator.strip() or "ops-dashboard", filing_reviewer)
with tab_new:
    render_create_form(operator.strip() or "ops-dashboard")
