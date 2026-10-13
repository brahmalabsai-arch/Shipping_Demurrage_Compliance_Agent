"""Demurrage — port tariff periods: view, price a stay, and add or revise a tariff.

Reads and writes go through ``services.demurrage_service`` — the same functions
``demurrage_rates_mcp`` and the risk agent use — with no caching, so an edit here is what the
next ``get_rate`` call sees. Tariff history is never overwritten: a revision starting on a new
day opens a new period and closes the previous one.
"""

from datetime import date
from typing import Any

import streamlit as st

from dashboards import theme
from dashboards.ui import fmt_day, problem_text, run_action, to_decimal
from models.enums import ContainerType
from models.schemas import PortRateRecord, PortRateUpsert
from services import demurrage_service as svc
from services.db import session_scope
from services.errors import CustomsCopilotError

# Explicitly typed option lists: inline literals make mypy pick Streamlit's `-> None` overload.
CONTAINER_TYPES: list[ContainerType] = list(ContainerType)
NEW_PERIOD = "(a new tariff period)"

theme.setup_page(
    "Demurrage",
    "How many days a container may stay in port for free, and what each day after that "
    "costs. A new tariff never overwrites an old one: it starts a new period.",
    icon=":material/hourglass_top:",
)


def _row(r: PortRateRecord) -> dict[str, Any]:
    return {
        "Port": r.port_code,
        "Container": r.container_type.value,
        "Free days": r.free_days,
        "Then per day": f"{r.daily_rate:,.2f} {r.currency}",
        "From": fmt_day(r.effective_from),
        "Until": fmt_day(r.effective_to) if r.effective_to else "No end date",
    }


def _label(r: PortRateRecord) -> str:
    return f"{r.port_code} {r.container_type.value} from {r.effective_from.isoformat()}"


def render_table(rates: list[PortRateRecord], ports: list[str]) -> None:
    theme.section("Tariff periods", "Each period applies to the boxes that arrive within it.")
    port_filter = st.selectbox("Port", ["all", *ports], key="rate_port_filter",
                               format_func=lambda p: "All ports" if p == "all" else p)  # fmt: skip
    shown = rates if port_filter == "all" else [r for r in rates if r.port_code == port_filter]
    theme.table([_row(r) for r in shown],
                empty="No tariffs yet. Add one below, or load the demo data with "
                "python -m scripts.seed.")  # fmt: skip


def render_lookup(ports: list[str]) -> None:
    theme.section("Price a stay", "The tariff in force on a day, and what a stay of so many days "
                                  "costs under it.")  # fmt: skip
    with st.form("rate_lookup"):
        c1, c2, c3, c4 = st.columns(4)
        port = c1.selectbox("Port", ports or ["INNSA"], key="lookup_port")
        ctype = c2.selectbox("Container type", CONTAINER_TYPES, key="lookup_ctype",
                             format_func=lambda x: x.value)  # fmt: skip
        as_of = c3.date_input("Day in port", date.today(), key="lookup_date")
        days = c4.number_input("Days in port", min_value=0, value=0, step=1, key="lookup_days")
        submitted = st.form_submit_button("Price it", key="lookup_submit")

    if not submitted:
        return
    try:
        with session_scope() as session:
            rate = svc.get_rate(session, port, ctype, as_of)
        charge = svc.demurrage_charge(rate, int(days))
    except CustomsCopilotError as exc:
        st.error(problem_text(exc))
        return
    until = fmt_day(rate.effective_to) if rate.effective_to else "no end date"
    st.success(
        f"{rate.port_code} {rate.container_type.value} on {fmt_day(as_of)}: "
        f"{rate.free_days} free days, then {rate.daily_rate:,.2f} {rate.currency} a day "
        f"(the period from {fmt_day(rate.effective_from)} to {until})."
    )
    st.metric(
        f"Demurrage for {charge.days_in_port} days",
        f"{charge.amount:,} {charge.currency}",
        help=f"{charge.chargeable_days} charged days x {rate.daily_rate} {rate.currency}",
    )


def render_upsert_form(rates: list[PortRateRecord], actor: str) -> None:
    theme.section(
        "Add or revise a tariff",
        "The same port, container and start day corrects that period. A new start day begins a "
        "new period and closes the one before it. Leave the end empty to run until the next "
        "period.",
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
            format_func=lambda x: x.value,
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
            "Charge per day after that",
            value=float(base.daily_rate) if base else 0.0,
            format="%.2f",
            key=f"rate_daily_{suffix}",
        )
        eff_from = c1.date_input(
            "Applies from",
            base.effective_from if base else date.today(),
            key=f"rate_from_{suffix}",
        )
        eff_to = c2.date_input(
            "Applies until (inclusive, optional)",
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
            f"Saved the {port} {ctype.value} tariff from {fmt_day(eff_from)}.",
        )


operator = st.sidebar.text_input("Your name (for the audit trail)", "ops-dashboard", key="actor")
with session_scope() as db:
    all_rates = svc.list_rates(db)
    known_ports = svc.list_ports(db)

render_table(all_rates, known_ports)
render_lookup(known_ports)
render_upsert_form(all_rates, operator.strip() or "ops-dashboard")
