"""Shipping Ops — create, edit and advance shipments; attach and amend their documents; run
the Document Verification agent, or the pipeline with the Demurrage & Risk agent, on a shipment;
and drive supervised clearance runs through their human gates (Phase 11).

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
from agents.state import CaseState, ChecklistItem, Discrepancy
from dashboards.ui import (
    flash,
    fmt_ist,
    ist_to_utc,
    run_action,
    show_flash,
    split_ist,
    to_decimal,
)
from models.enums import ContainerType, DocType, RuleStatus, RunState, ShipmentStatus
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

st.set_page_config(page_title="Shipping Ops", layout="wide")

# Explicitly typed option lists: inline literals make mypy pick Streamlit's `-> None` overload.
CONTAINER_TYPES: list[ContainerType] = list(ContainerType)
DOC_TYPES: list[DocType] = list(DocType)
START_STATUSES: list[ShipmentStatus] = [ShipmentStatus.QUOTED, ShipmentStatus.BOOKED]
STATUS_FILTERS: list[str] = ["all", *(s.value for s in ShipmentStatus)]
REVIEWERS: list[str] = [r.name for r in governance_service.reviewers()]


# --------------------------------------------------------------------------- tables


def _shipment_row(s: ShipmentRecord) -> dict[str, Any]:
    return {
        "Container": s.container_id,
        "Type": s.container_type.value,
        "Status": s.status.value,
        "Cargo": s.cargo_type,
        "HS code": s.hs_code,
        "Qty": str(s.declared_qty),
        "Value": f"{s.declared_value:,} {s.declared_currency}",
        "Route": f"{s.origin_port} → {s.destination_port}",
        "ETD": fmt_ist(s.etd),
        "ETA": fmt_ist(s.eta),
        "Quote expires": s.quote_expiry_date.isoformat() if s.quote_expiry_date else "—",
        "Updated": fmt_ist(s.updated_at),
    }


def _document_row(d: DocumentRecord) -> dict[str, Any]:
    return {
        "Type": d.doc_type.value,
        "Version": d.version,
        "Qty": "—" if d.qty is None else str(d.qty),
        "Value": "—" if d.value is None else f"{d.value:,} {d.currency or ''}".strip(),
        "Issued": d.issued_date.isoformat(),
        "Current": "yes" if d.superseded_by is None else "superseded",
    }


def _doc_label(d: DocumentRecord) -> str:
    return f"{d.doc_type.value} v{d.version} (issued {d.issued_date.isoformat()})"


# --------------------------------------------------------------------------- shipment forms


def _optional_datetime(label: str, current: datetime | None, key: str) -> datetime | None:
    day_default, time_default = split_ist(current) if current else (None, time(0, 0))
    col_day, col_time = st.columns(2)
    day = col_day.date_input(f"{label} date (IST)", value=day_default, key=f"{key}_date")
    at = col_time.time_input(f"{label} time (IST)", value=time_default, key=f"{key}_time")
    return ist_to_utc(day, at) if day else None


def render_edit_form(s: ShipmentRecord, actor: str) -> None:
    cid = s.container_id
    with st.form(f"edit_{cid}"):
        st.subheader(f"Edit {cid}")
        c1, c2, c3 = st.columns(3)
        values: dict[str, Any] = {
            "container_type": c1.selectbox(
                "Container type",
                CONTAINER_TYPES,
                index=CONTAINER_TYPES.index(s.container_type),
                format_func=lambda x: x.value,
                key=f"edit_container_type_{cid}",
            ),
            "cargo_type": c1.text_input("Cargo type", s.cargo_type, key=f"edit_cargo_type_{cid}"),
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
        values["etd"] = _optional_datetime("ETD", s.etd, f"edit_etd_{cid}")
        values["eta"] = _optional_datetime("ETA (clear date if unknown)", s.eta, f"edit_eta_{cid}")
        q1, q2, q3, q4 = st.columns(4)
        values["quote_date"] = q1.date_input("Quote date", s.quote_date, key=f"edit_qd_{cid}")
        values["quote_validity_days"] = q2.number_input(
            "Quote validity (days)",
            min_value=7,
            max_value=14,
            value=s.quote_validity_days,
            step=1,
            key=f"edit_qv_{cid}",
        )
        values["bol_date"] = q3.date_input("BoL on-board date", s.bol_date, key=f"edit_bol_{cid}")
        values["invoice_date"] = q4.date_input(
            "Invoice date", s.invoice_date, key=f"edit_inv_{cid}"
        )
        submitted = st.form_submit_button("Save changes", key=f"save_{cid}")

    if submitted:
        current = s.model_dump()
        changed = {k: v for k, v in values.items() if v != current[k]}
        if not changed:
            st.info("No changes to save.")
            return
        run_action(
            lambda session: svc.update_shipment(
                session, s.id, ShipmentUpdate.model_validate(changed), actor
            ),
            f"Saved {cid}: {', '.join(sorted(changed))}.",
        )


def render_status_controls(s: ShipmentRecord, actor: str) -> None:
    cid = s.container_id
    allowed = sorted(svc.allowed_transitions(s.status) - {ShipmentStatus.CLEARED})
    st.subheader("Lifecycle status")
    if ShipmentStatus.CLEARED in svc.allowed_transitions(s.status):
        st.caption("`cleared` is reached only by filing — see *Clearance run* below.")
    if not allowed:
        st.caption(f"No manual move from `{s.status.value}`.")
        return
    col_pick, col_go = st.columns([3, 1])
    target = col_pick.selectbox(
        f"Move from `{s.status.value}` to",
        allowed,
        format_func=lambda x: x.value,
        key=f"next_status_{cid}",
    )
    col_go.write("")
    if col_go.button("Apply status", key=f"apply_status_{cid}"):
        run_action(
            lambda session: svc.update_status(session, s.id, target, actor),
            f"{cid}: {s.status.value} → {target.value}.",
        )


def render_documents(s: ShipmentRecord, actor: str) -> None:
    cid = s.container_id
    st.subheader("Documents")
    show_history = st.checkbox("Show superseded versions", key=f"doc_history_{cid}")
    with session_scope() as session:
        docs = svc.get_documents(session, s.id, include_superseded=show_history)
    if docs:
        st.dataframe([_document_row(d) for d in docs], hide_index=True, width="stretch")
    else:
        st.info("No documents attached.")

    add_col, amend_col = st.columns(2)
    with add_col, st.form(f"add_doc_{cid}", clear_on_submit=True):
        st.markdown("**Attach document**")
        doc_type = st.selectbox(
            "Type", DOC_TYPES, format_func=lambda x: x.value, key=f"add_doc_type_{cid}"
        )
        qty = st.number_input("Qty", min_value=0.0, value=None, key=f"add_doc_qty_{cid}")
        value = st.number_input(
            "Value", min_value=0.0, value=None, format="%.2f", key=f"add_doc_value_{cid}"
        )
        currency = st.text_input("Currency (blank if n/a)", key=f"add_doc_currency_{cid}")
        issued = st.date_input("Issued date", value=date.today(), key=f"add_doc_issued_{cid}")
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
                f"Attached {doc_type.value} to {cid}.",
            )

    current_docs = [d for d in docs if d.superseded_by is None]
    with amend_col:
        if not current_docs:
            return
        with st.form(f"amend_doc_{cid}"):
            st.markdown("**Amend document** (creates a new version)")
            by_label = {_doc_label(d): d for d in current_docs}
            picked = by_label[st.selectbox("Document", list(by_label), key=f"amend_pick_{cid}")]
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
                "Issued date", picked.issued_date, key=f"amend_issued_{cid}_{picked.id}"
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
                    st.info("No changes — nothing to amend.")
                else:
                    run_action(
                        lambda session: svc.amend_document(
                            session, picked.id, DocumentAmend.model_validate(changed), actor
                        ),
                        f"Issued {picked.doc_type.value} v{picked.version + 1} for {cid}.",
                    )


def _discrepancy_row(d: Discrepancy) -> dict[str, Any]:
    return {
        "Finding": d.code,
        "Field": d.field,
        "Detail": d.message,
        "Delta": "—" if d.delta is None else str(d.delta),
    }


def _rate_row(r: ResolvedRate) -> dict[str, Any]:
    return {
        "Stage": r.stage.value if r.invoice_kind is None else f"{r.stage.value} ({r.invoice_kind})",
        "Date": r.as_of.isoformat(),
        "Rate": f"{r.pair} {r.rate}",
        "Source": r.source.value,
        "Basis": r.basis,
        "Valid through": r.expires.isoformat() if r.expires else "—",
    }


def render_verification(s: ShipmentRecord) -> None:
    """Run the Document Verification agent (over its MCP servers) and show what it found."""
    cid = s.container_id
    st.subheader("Document verification")
    if s.status is ShipmentStatus.CANCELLED:
        st.caption("Cancelled shipments are not verified.")
        return
    st.caption(
        "Runs the Document Verification agent: cross-checks the current documents with each "
        "other and the shipment record (quantity, value, currency, dates, missing documents) "
        "and resolves the FX rate for each lifecycle date. Read-only; the run is audited."
    )
    if st.button("Verify documents", key=f"verify_{cid}"):
        try:
            with st.spinner("Verifying documents..."):
                st.session_state[f"verification_{cid}"] = asyncio.run(
                    document_verification_agent.verify_shipment(s.id)
                )
        except ShipmentCancelledError as exc:
            st.warning(f"Verification stopped: {exc}")
        except Exception as exc:  # show any failure to the operator
            st.error(f"Verification failed: {type(exc).__name__}: {exc}")
    state: CaseState | None = st.session_state.get(f"verification_{cid}")
    if state is None:
        return
    explanation = state.document_explanation
    summary = explanation.text if explanation else ""
    heading = f"As of {state.as_of.isoformat()} (run {state.run_id[:8]}): "
    if state.document_outcome == "clear":
        st.success(heading + summary)
    else:
        st.warning(heading + summary)
        st.dataframe(
            [_discrepancy_row(d) for d in state.document_discrepancies],
            hide_index=True,
            width="stretch",
        )
    fx = state.fx_context
    if fx is not None and fx.rates:
        st.dataframe([_rate_row(r) for r in fx.rates], hide_index=True, width="stretch")
    elif fx is not None and fx.skipped_reason:
        st.caption(f"FX: {fx.skipped_reason}")


def _show_risk(state: CaseState) -> None:
    heading = f"Risk: {state.risk} — as of {state.as_of.isoformat()} (run {state.run_id[:8]})"
    reasons = "; ".join(state.risk_reasons)
    if state.risk in ("high", "medium"):
        st.warning(f"{heading}. {reasons}")
    elif state.risk == "none":
        st.success(f"{heading}. {reasons}")
    else:
        st.info(f"{heading}. {reasons}")
    e = state.demurrage_estimate
    if e is not None:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Days in port", e.days_in_port)
        c2.metric("Chargeable days", e.chargeable_days)
        c3.metric(f"Demurrage ({e.currency})", f"{e.amount:,}")
        c4.metric("Demurrage (INR)", "—" if e.amount_inr is None else f"{e.amount_inr:,}")
        st.caption(e.basis)
    for gap in state.data_gaps:
        st.warning(f"Data gap — {gap}")


def _item_row(item: ChecklistItem) -> dict[str, Any]:
    return {
        "Severity": item.severity,
        "Category": item.category,
        "Item": item.title,
        "Detail": item.detail,
        "Rulebook guidance": item.guidance or "",
        "References": ", ".join(item.references),
        "Raised by": item.source_agent,
    }


def _show_checklist(state: CaseState) -> None:
    checklist = state.checklist
    if checklist is None:
        return
    st.markdown("**Clearance checklist**")
    if checklist.ready_for_clearance:
        st.success(checklist.summary)
    else:
        st.info(checklist.summary)
    for missing in checklist.missing_inputs:
        failure = next((f for f in state.agent_failures if f.agent.startswith(missing)), None)
        reason = f" ({failure.kind}: {failure.error})" if failure else ""
        st.warning(f"Missing input — {missing}{reason}; the checklist is built without it.")
    for conflict in checklist.conflicts:
        st.warning(f"Conflict — {conflict.description} " + " | ".join(conflict.signals))
    st.dataframe([_item_row(i) for i in checklist.items], hide_index=True, width="stretch")


def render_risk(s: ShipmentRecord) -> None:
    """Run the whole pipeline and show the risk and the clearance checklist."""
    cid = s.container_id
    st.subheader("Shipment analysis — demurrage, risk and clearance checklist")
    if s.status is ShipmentStatus.CANCELLED:
        st.caption("Cancelled shipments are not assessed.")
        return
    st.caption(
        "Runs every agent: document verification; then, in parallel, demurrage & risk (the "
        "tariff in force on the arrival day, arrival day = day 1, converted at the gazetted rate "
        "on today's date) and the compliance agent (questions to the approved rulebook); then "
        "the clearance checklist that combines them. Read-only; audited. Takes ~30 s or more "
        "(the rulebook models load, then LLM calls)."
    )
    if st.button("Analyse shipment", key=f"assess_{cid}"):
        try:
            with st.spinner("Running the agents..."):
                st.session_state[f"risk_{cid}"] = asyncio.run(graph.analyse_shipment(s.id))
        except ShipmentCancelledError as exc:
            st.warning(f"Analysis stopped: {exc}")
        except Exception as exc:  # show any failure to the operator
            st.error(f"Analysis failed: {type(exc).__name__}: {exc}")
    state: CaseState | None = st.session_state.get(f"risk_{cid}")
    if state is not None:
        _show_risk(state)
        _show_checklist(state)


# --------------------------------------------------------------------------- clearance runs


def _run_row(run: CaseRunRecord) -> dict[str, Any]:
    return {
        "Run": run.run_id[:8],
        "State": run.state.value,
        "Analysis date": run.as_of.isoformat(),
        "Started": fmt_ist(run.started_at),
        "By": run.started_by,
        "Updated": fmt_ist(run.updated_at),
        "Detail": run.detail or "",
    }


def _drive(label: str, action: Any) -> None:
    """Run one supervisor call (async; opens the MCP servers), then rerun to show the result."""
    try:
        with st.spinner(label):
            result = asyncio.run(action)
    except CustomsCopilotError as exc:
        st.error(f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # show any failure to the operator
        st.error(f"Clearance run failed: {type(exc).__name__}: {exc}")
    else:
        flash(f"Run {result.run.run_id[:8]}: {result.run.state.value}. {result.run.detail or ''}")
        st.rerun()


def _render_compliance_wait(run: CaseRunRecord) -> None:
    st.info("Waiting at the **compliance gate**: a proposed rule change covering these goods "
            "must be decided on the *Compliance* page first.")  # fmt: skip
    with session_scope() as session:
        rules = [compliance_service.get_rule(session, rule_id) for rule_id in run.awaiting_rule_ids]
    for rule in rules:
        col_rule, col_go = st.columns([3, 1])
        col_rule.write(f"{rule.rule_key} v{rule.version} — {rule.status.value}")
        if rule.status is RuleStatus.PENDING:
            continue
        if col_go.button("Resume run", key=f"resume_{run.run_id}_{rule.id}"):
            _drive("Resuming the run (re-asking the rulebook)...",
                   supervisor.resume_after_compliance_decision(run.run_id, rule.id))  # fmt: skip


def _render_filing_wait(run: CaseRunRecord, reviewer: str | None) -> None:
    st.success("Waiting at the **filing gate**: the checklist is ready for clearance. A "
               "registered reviewer approves (files the shipment as `cleared`) or rejects "
               "(final for this run).")  # fmt: skip
    try:
        case = asyncio.run(supervisor.inspect_run(run.run_id)).case
    except Exception as exc:  # show any failure to the operator
        st.error(f"Could not read the run: {type(exc).__name__}: {exc}")
        return
    _show_checklist(case)
    if reviewer is None:
        st.warning("Choose yourself as *Reviewer* in the sidebar to decide.")
        return
    note = st.text_input("Note (required to reject)", "", key=f"filing_note_{run.run_id}")
    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("Approve filing", key=f"approve_filing_{run.run_id}", type="primary"):
        _drive("Filing the shipment...",
               supervisor.resume_after_filing_approval(run.run_id, "approve", reviewer, note))  # fmt: skip
    if b2.button("Reject filing", key=f"reject_filing_{run.run_id}"):
        _drive("Recording the rejection...",
               supervisor.resume_after_filing_approval(run.run_id, "reject", reviewer, note))  # fmt: skip


def _render_abandon(run: CaseRunRecord, reviewer: str | None) -> None:
    with st.expander("Abandon this run"):
        st.caption("For a run left `running` by a crashed process, or one that should not go on. "
                   "It ends as `failed`; the shipment can then start a new run.")  # fmt: skip
        reason = st.text_input("Reason", "", key=f"abandon_reason_{run.run_id}")
        if st.button("Abandon run", key=f"abandon_{run.run_id}", disabled=reviewer is None):
            try:
                supervisor.abandon_clearance_run(run.run_id, reviewer or "", reason)
            except CustomsCopilotError as exc:
                st.error(f"{type(exc).__name__}: {exc}")
            else:
                flash(f"Run {run.run_id[:8]} abandoned.")
                st.rerun()


def render_clearance(s: ShipmentRecord, actor: str, reviewer: str | None) -> None:
    """Supervised clearance runs: the pipeline behind the compliance and filing gates."""
    cid = s.container_id
    st.subheader("Clearance run")
    st.caption(
        "A supervised run analyses the shipment (every agent), then waits for people: first for "
        "a decision on any pending rule change covering its HS code (Compliance page), then — if "
        "the checklist is ready and the shipment is docked or under review — for a reviewer to "
        "approve the filing, which moves it to `cleared`. Each wait survives restarts."
    )
    with session_scope() as session:
        runs = governance_service.list_runs(session, s.id)
    open_run = next((r for r in runs if r.state.is_open), None)
    if runs:
        st.dataframe([_run_row(r) for r in runs], hide_index=True, width="stretch")
    if open_run is None:
        if s.status is ShipmentStatus.CANCELLED:
            st.caption("Cancelled shipments are not cleared.")
        elif st.button("Start clearance run", key=f"clearance_start_{cid}"):
            _drive("Running the agents (~30 s or more)...",
                   supervisor.start_clearance_run(s.id, started_by=actor))  # fmt: skip
        return
    if open_run.state is RunState.AWAITING_COMPLIANCE:
        _render_compliance_wait(open_run)
    elif open_run.state is RunState.AWAITING_FILING:
        _render_filing_wait(open_run, reviewer)
    else:
        st.warning(f"Run {open_run.run_id[:8]} is still `running` — in progress elsewhere, or "
                   "interrupted by a crash.")  # fmt: skip
    _render_abandon(open_run, reviewer)


def render_create_form(actor: str) -> None:
    with st.form("create_shipment", clear_on_submit=False):
        st.subheader("New shipment")
        c1, c2, c3 = st.columns(3)
        data: dict[str, Any] = {
            "container_id": c1.text_input("Container id (e.g. MSKU1234567)", key="new_container"),
            "status": c2.selectbox(
                "Starts as",
                START_STATUSES,
                format_func=lambda x: x.value,
                key="new_status",
            ),
            "container_type": c1.selectbox(
                "Container type (prices demurrage)",
                CONTAINER_TYPES,
                format_func=lambda x: x.value,
                key="new_container_type",
            ),
            "cargo_type": c3.text_input("Cargo type", key="new_cargo_type"),
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
        data["etd"] = _optional_datetime("ETD", None, "new_etd")
        data["eta"] = _optional_datetime("ETA (optional)", None, "new_eta")
        q1, q2 = st.columns(2)
        data["quote_date"] = q1.date_input(
            "Quote date (required when quoted)", value=None, key="new_quote_date"
        )
        data["quote_validity_days"] = q2.number_input(
            "Quote validity (7-14 days)",
            min_value=7,
            max_value=14,
            value=None,
            step=1,
            key="new_quote_validity",
        )
        submitted = st.form_submit_button("Create shipment", key="create_submit")

    if submitted:
        if data["etd"] is None:
            st.error("ETD date is required.")
            return
        payload = {k: v for k, v in data.items() if v is not None and v != ""}
        run_action(
            lambda session: svc.create_shipment(
                session, ShipmentCreate.model_validate(payload), actor
            ),
            f"Created {data['container_id']}.",
        )


# --------------------------------------------------------------------------- page


def render_shipments(actor: str, reviewer: str | None) -> None:
    status_filter = st.selectbox("Filter by status", STATUS_FILTERS, key="status_filter")
    status = None if status_filter == "all" else ShipmentStatus(status_filter)
    with session_scope() as session:
        shipments = svc.list_shipments(session, status)
    if not shipments:
        st.info("No shipments — create one, or run `python -m scripts.seed_shipments`.")
        return

    st.dataframe([_shipment_row(s) for s in shipments], hide_index=True, width="stretch")
    by_container = {s.container_id: s for s in shipments}
    selected = st.selectbox("Select shipment", list(by_container), key="selected_container")
    shipment = by_container[selected]

    render_edit_form(shipment, actor)
    render_status_controls(shipment, actor)
    render_documents(shipment, actor)
    render_verification(shipment)
    render_risk(shipment)
    render_clearance(shipment, actor, reviewer)


st.title("Shipping Ops")
show_flash()
operator = st.sidebar.text_input(
    "Operator (recorded in the audit trail)", "ops-dashboard", key="actor"
)
filing_reviewer: str | None = st.sidebar.selectbox(
    "Reviewer (filing decisions)", REVIEWERS, index=None, key="reviewer",
    placeholder="registered reviewers (REVIEWERS)",
)  # fmt: skip
if not REVIEWERS:
    st.sidebar.warning("No reviewers configured — set REVIEWERS in .env.")
tab_list, tab_new = st.tabs(["Shipments", "New shipment"])
with tab_list:
    render_shipments(operator.strip() or "ops-dashboard", filing_reviewer)
with tab_new:
    render_create_form(operator.strip() or "ops-dashboard")
