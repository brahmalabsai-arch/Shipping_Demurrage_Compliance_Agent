"""Demurrage — port tariff periods: view, add or revise, and look up / price a stay.

Reads and writes go through ``services.demurrage_service`` — the same functions
``demurrage_rates_mcp`` and the risk agent use — with no caching, so an edit here is what the
next ``get_rate`` call sees. Tariff history is never overwritten: a revision starting on a new
day opens a new period and closes the previous one.
"""

from datetime import date
from typing import Any

import streamlit as st

from dashboards.ui import run_action, show_flash, to_decimal
from models.enums import ContainerType
from models.schemas import PortRateRecord, PortRateUpsert
from services import demurrage_service as svc
from services.db import session_scope
from services.errors import CustomsCopilotError

st.set_page_config(page_title="Demurrage", layout="wide")

# Explicitly typed option lists: inline literals make mypy pick Streamlit's `-> None` overload.
CONTAINER_TYPES: list[ContainerType] = list(ContainerType)
NEW_PERIOD = "(new tariff period)"


def _row(r: PortRateRecord) -> dict[str, Any]:
    return {
        "Port": r.port_code,
        "Container": r.container_type.value,
        "Free days": r.free_days,
        "Daily rate": f"{r.daily_rate:,} {r.currency}",
        "Effective from": r.effective_from.isoformat(),
        "Effective to": r.effective_to.isoformat() if r.effective_to else "open-ended",
    }


def _label(r: PortRateRecord) -> str:
    return f"{r.port_code} {r.container_type.value} from {r.effective_from.isoformat()}"


def render_table(rates: list[PortRateRecord], ports: list[str]) -> None:
    st.subheader("Tariff periods")
    port_filter = st.selectbox("Port", ["all", *ports], key="rate_port_filter")
    shown = rates if port_filter == "all" else [r for r in rates if r.port_code == port_filter]
    if shown:
        st.dataframe([_row(r) for r in shown], hide_index=True, width="stretch")
    else:
        st.info("No tariffs configured — add one below, or run `python -m scripts.seed`.")


def render_upsert_form(rates: list[PortRateRecord], actor: str) -> None:
    st.subheader("Add or revise a tariff")
    st.caption(
        "Same port + container + *effective from* as an existing period corrects that period. "
        "A new *effective from* starts a new period and closes the one in force the day before. "
        "Leave *effective to* empty to run until the next scheduled period (or open-ended)."
    )
    by_label = {_label(r): r for r in rates}
    choice = st.selectbox("Start from", [NEW_PERIOD, *by_label], key="rate_template")
    base = by_label.get(choice)
    suffix = "new" if base is None else str(base.id)

    with st.form(f"upsert_rate_{suffix}"):
        c1, c2, c3 = st.columns(3)
        port = c1.text_input(
            "Port (UN/LOCODE)", base.port_code if base else "INNSA", key=f"rate_port_{suffix}"
        )
        ctype = c2.selectbox(
            "Container type",
            CONTAINER_TYPES,
            index=CONTAINER_TYPES.index(base.container_type) if base else 0,
            key=f"rate_ctype_{suffix}",
        )
        currency = c3.text_input(
            "Currency (ISO 4217)", base.currency if base else "USD", key=f"rate_ccy_{suffix}"
        )
        # No min_value on purpose: negative input must be rejected by the Pydantic model
        # (edge cases, Phase 2), and the error is shown inline.
        free_days = c1.number_input(
            "Free days", value=base.free_days if base else 5, step=1, key=f"rate_free_{suffix}"
        )
        daily_rate = c2.number_input(
            "Daily rate",
            value=float(base.daily_rate) if base else 0.0,
            format="%.2f",
            key=f"rate_daily_{suffix}",
        )
        eff_from = c1.date_input(
            "Effective from",
            base.effective_from if base else date.today(),
            key=f"rate_from_{suffix}",
        )
        eff_to = c2.date_input(
            "Effective to (inclusive, optional)",
            base.effective_to if base else None,
            key=f"rate_to_{suffix}",
        )
        submitted = st.form_submit_button("Save tariff", key=f"rate_submit_{suffix}")

    if submitted:
        data = {
            "port_code": port,
            "container_type": ctype,
            "currency": currency,
            "free_days": int(free_days),
            "daily_rate": to_decimal(daily_rate),
            "effective_from": eff_from,
            "effective_to": eff_to,
        }
        run_action(
            lambda session: svc.upsert_rate(session, PortRateUpsert.model_validate(data), actor),
            f"Saved tariff {port} {ctype} from {eff_from}.",
        )


def render_lookup(ports: list[str]) -> None:
    st.subheader("Look up a rate and price a stay")
    with st.form("rate_lookup"):
        c1, c2, c3, c4 = st.columns(4)
        port = c1.selectbox("Port", ports or ["INNSA"], key="lookup_port")
        ctype = c2.selectbox("Container type", CONTAINER_TYPES, key="lookup_ctype")
        as_of = c3.date_input("Date in port", date.today(), key="lookup_date")
        days = c4.number_input("Days in port", min_value=0, value=0, step=1, key="lookup_days")
        submitted = st.form_submit_button("Look up", key="lookup_submit")

    if not submitted:
        return
    try:
        with session_scope() as session:
            rate = svc.get_rate(session, port, ctype, as_of)
        charge = svc.demurrage_charge(rate, int(days))
    except CustomsCopilotError as exc:
        st.error(f"{type(exc).__name__}: {exc}")
        return
    st.success(
        f"{rate.port_code} {rate.container_type.value} on {as_of}: "
        f"{rate.free_days} free days, then {rate.daily_rate:,} {rate.currency}/day "
        f"(period {rate.effective_from} → {rate.effective_to or 'open-ended'})."
    )
    st.metric(
        f"Demurrage for {charge.days_in_port} days",
        f"{charge.amount:,} {charge.currency}",
        help=f"{charge.chargeable_days} chargeable days x {rate.daily_rate} {rate.currency}",
    )


st.title("Demurrage & Port Charges")
show_flash()
operator = st.sidebar.text_input(
    "Operator (recorded in the audit trail)", "ops-dashboard", key="actor"
)
with session_scope() as db:
    all_rates = svc.list_rates(db)
    known_ports = svc.list_ports(db)

render_table(all_rates, known_ports)
render_upsert_form(all_rates, operator.strip() or "ops-dashboard")
render_lookup(known_ports)
