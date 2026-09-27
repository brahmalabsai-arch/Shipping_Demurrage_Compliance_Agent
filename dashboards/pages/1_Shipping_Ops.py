"""Shipping Ops — create, edit and advance shipments; attach and amend their documents.

Every read and write goes through ``services.shipping_service`` (the same functions
``shipping_records_mcp`` calls), so the dashboard and the agents can never disagree about the
data. Nothing is deleted: shipments are cancelled/closed via status, documents are amended into
new versions. Widget keys are suffixed with the container id so switching shipments re-seeds
each form from the database.
"""

from datetime import date, datetime, time
from typing import Any

import streamlit as st

from dashboards.ui import (
    fmt_ist,
    ist_to_utc,
    run_action,
    show_flash,
    split_ist,
    to_decimal,
)
from models.enums import DocType, ShipmentStatus
from models.schemas import (
    DocumentAmend,
    DocumentCreate,
    DocumentRecord,
    ShipmentCreate,
    ShipmentRecord,
    ShipmentUpdate,
)
from services import shipping_service as svc
from services.db import session_scope

st.set_page_config(page_title="Shipping Ops", layout="wide")

# Explicitly typed option lists: inline literals make mypy pick Streamlit's `-> None` overload.
DOC_TYPES: list[DocType] = list(DocType)
START_STATUSES: list[ShipmentStatus] = [ShipmentStatus.QUOTED, ShipmentStatus.BOOKED]
STATUS_FILTERS: list[str] = ["all", *(s.value for s in ShipmentStatus)]


# --------------------------------------------------------------------------- tables


def _shipment_row(s: ShipmentRecord) -> dict[str, Any]:
    return {
        "Container": s.container_id,
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
    allowed = sorted(svc.allowed_transitions(s.status))
    st.subheader("Lifecycle status")
    if not allowed:
        st.caption(f"`{s.status.value}` is terminal — no further moves.")
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


def render_shipments(actor: str) -> None:
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


st.title("Shipping Ops")
show_flash()
operator = st.sidebar.text_input(
    "Operator (recorded in the audit trail)", "ops-dashboard", key="actor"
)
tab_list, tab_new = st.tabs(["Shipments", "New shipment"])
with tab_list:
    render_shipments(operator.strip() or "ops-dashboard")
with tab_new:
    render_create_form(operator.strip() or "ops-dashboard")
