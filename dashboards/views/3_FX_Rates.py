"""FX Rates — find the rate for a lifecycle stage, and edit the spot / locked / gazetted tables.

Reads and writes go through ``services.fx_service`` — the same functions ``fx_rates_mcp`` and the
agents use — with no caching, so an edit here is what the next ``resolve_rate`` call sees.
Locked and gazetted rates are append-only: a lock can never change, and a gazetted rate is
corrected by publishing a revision of it.
"""

from datetime import date, time
from typing import Any

import streamlit as st

from dashboards import theme
from dashboards.ui import fmt_day, fmt_ist, ist_to_utc, problem_text, run_action, to_decimal
from models.enums import FxStage, InvoiceKind, LockType
from models.schemas import (
    GazettedRateCreate,
    GazettedRateRecord,
    LockedRateCreate,
    LockedRateRecord,
    ResolvedRate,
    SpotRateRecord,
    SpotRateUpsert,
)
from services import fx_service as svc
from services.db import session_scope
from services.errors import CustomsCopilotError

# Explicitly typed option lists: inline literals make mypy pick Streamlit's `-> None` overload.
FX_STAGES: list[FxStage] = list(FxStage)
LOCK_TYPES: list[LockType] = list(LockType)
INVOICE_KINDS: list[InvoiceKind] = list(InvoiceKind)
NEW_NOTIFICATION = "(a new notification)"
DEFAULT_PAIR = "USD/INR"

STAGE_LABELS = {
    FxStage.QUOTATION: "Quotation: the spot rate, valid for a few days",
    FxStage.BOL: "Bill of lading: the rate locked on the on-board day",
    FxStage.INVOICE: "Invoice: commercial or freight",
    FxStage.PORT_CHARGES: "Port charges: the customs-notified rate",
}
DATE_LABELS = {
    FxStage.QUOTATION: "Quote date",
    FxStage.BOL: "On-board date",
    FxStage.INVOICE: "Invoice date",
    FxStage.PORT_CHARGES: "Date of the port charges",
}
LOCK_WORDS = {LockType.BOL: "Bill of lading", LockType.COMMERCIAL_INVOICE: "Commercial invoice"}
KIND_WORDS = {InvoiceKind.COMMERCIAL: "Commercial", InvoiceKind.FREIGHT: "Freight"}

theme.setup_page(
    "FX Rates",
    "Which exchange rate applies depends on the shipment's stage: a spot rate for a quote, a "
    "rate locked for the bill of lading and invoice, the customs-notified rate for port "
    "charges. Locked and notified rates are never edited, only added to.",
    icon=":material/currency_exchange:",
)


def _rate_input(container: Any, label: str, value: float, key: str) -> float:
    # No min_value on purpose: a zero/negative rate must be rejected by the Pydantic model and
    # the error shown inline, as on the Demurrage page.
    return float(container.number_input(label, value=value, step=0.0001, format="%.6f", key=key))


# --------------------------------------------------------------------------- resolve


def _show_resolved(resolved: ResolvedRate, check_on: date | None) -> None:
    st.success(
        f"{resolved.pair} for {resolved.stage.value.replace('_', ' ')} on "
        f"{fmt_day(resolved.as_of)}: **{resolved.rate.normalize()}**, {resolved.basis}."
    )
    st.caption(
        ("Locked: this rate can never change. " if resolved.locked else "")
        + f"Taken from the {resolved.source.value} rates table."
    )
    if resolved.expires is not None and check_on is not None:
        if svc.is_quote_expired(resolved, check_on):
            st.warning(
                f"The quote expired on {fmt_day(resolved.expires)} (checked on "
                f"{fmt_day(check_on)}). Quote again at a fresh spot rate before pricing with it."
            )
        else:
            st.info(f"The quote is valid until {fmt_day(resolved.expires)} (checked on "
                    f"{fmt_day(check_on)}).")  # fmt: skip


def render_resolver(pairs: list[str]) -> None:
    theme.lead("Pick the stage and the date; the rules choose the rate. A missing rate is "
               "reported, never guessed from a nearby day.")  # fmt: skip
    stage = st.selectbox(
        "Stage",
        FX_STAGES,
        format_func=lambda s: STAGE_LABELS[s],
        key="resolve_stage",
    )
    with st.form("resolve_form"):
        c1, c2, c3 = st.columns(3)
        pair = c1.selectbox("Currency pair", pairs or [DEFAULT_PAIR], key="resolve_pair")
        as_of = c2.date_input(DATE_LABELS[stage], date.today(), key="resolve_date")
        validity: float | None = None
        check_on: date | None = None
        kind: InvoiceKind | None = None
        bol_date: date | None = None
        if stage is FxStage.QUOTATION:
            # No min/max: the service's 7-14 rule is what rejects other values.
            validity = c3.number_input(
                "Quote valid for (7-14 days)", value=14, step=1, key="resolve_validity"
            )
            check_on = c3.date_input("Is it still valid on", date.today(), key="resolve_check_on")
        elif stage is FxStage.INVOICE:
            kind = c3.selectbox("Invoice", INVOICE_KINDS, key="resolve_invoice_kind",
                                format_func=lambda k: KIND_WORDS.get(k, k.value))  # fmt: skip
            bol_date = c3.date_input(
                "On-board date (freight invoice only)", None, key="resolve_bol_date"
            )
        submitted = st.form_submit_button("Find the rate", key="resolve_submit")

    if not submitted:
        return
    try:
        with session_scope() as session:
            resolved = svc.resolve_rate(
                session,
                pair,
                as_of,
                stage,
                quote_validity_days=None if validity is None else int(validity),
                invoice_kind=kind,
                bol_date=bol_date if kind is InvoiceKind.FREIGHT else None,
            )
    except CustomsCopilotError as exc:
        st.error(problem_text(exc))
        return
    _show_resolved(resolved, check_on)


# --------------------------------------------------------------------------- spot


def _spot_row(r: SpotRateRecord) -> dict[str, Any]:
    return {"Pair": r.pair, "Rate": str(r.rate.normalize()), "Observed": fmt_ist(r.as_of),
            "Source": r.source}  # fmt: skip


def render_spot(rates: list[SpotRateRecord], pairs: list[str], actor: str) -> None:
    theme.lead("Market rates observed during the day, used to price a quotation.")
    pair_filter = st.selectbox("Pair", ["all", *pairs], key="spot_pair_filter",
                               format_func=lambda p: "All pairs" if p == "all" else p)  # fmt: skip
    shown = rates if pair_filter == "all" else [r for r in rates if r.pair == pair_filter]
    theme.table([_spot_row(r) for r in shown], empty="No spot rates yet. Add one below.")

    with st.form("spot_form"):
        st.markdown("**Record a spot rate** (the same pair and moment corrects the earlier one)")
        c1, c2, c3 = st.columns(3)
        pair = c1.text_input("Pair (BASE/QUOTE)", DEFAULT_PAIR, key="spot_pair")
        rate = _rate_input(c2, "Rate (rupees per 1 unit)", 0.0, "spot_rate")
        source = c3.text_input("Source", "fx-desk", key="spot_source")
        day = c1.date_input("Observed on (IST)", date.today(), key="spot_date")
        at = c2.time_input("Observed at (IST)", time(13, 30), key="spot_time")
        submitted = st.form_submit_button("Save spot rate", key="spot_submit")

    if submitted:
        data = {
            "pair": pair,
            "rate": to_decimal(rate),
            "as_of": ist_to_utc(day, at),
            "source": source,
        }
        run_action(
            lambda session: svc.upsert_spot_rate(
                session, SpotRateUpsert.model_validate(data), actor
            ),
            f"Saved the {pair} spot rate observed {fmt_day(day)} at {at:%H:%M} IST.",
        )


# --------------------------------------------------------------------------- locked


def _locked_row(r: LockedRateRecord) -> dict[str, Any]:
    return {
        "Pair": r.pair,
        "Locked for": LOCK_WORDS.get(r.lock_type, r.lock_type.value),
        "On": fmt_day(r.lock_date),
        "Rate": str(r.rate.normalize()),
    }


def render_locked(rates: list[LockedRateRecord], actor: str) -> None:
    theme.lead("Rates fixed on a bill of lading or commercial invoice date. A locked rate can "
               "never be edited or removed, and the same lock cannot be made twice.")  # fmt: skip
    theme.table([_locked_row(r) for r in rates], empty="No locked rates yet.")
    with st.form("locked_form"):
        st.markdown("**Lock a rate**")
        c1, c2, c3, c4 = st.columns(4)
        pair = c1.text_input("Pair (BASE/QUOTE)", DEFAULT_PAIR, key="locked_pair")
        lock_type = c2.selectbox("Locked for", LOCK_TYPES, key="locked_type",
                                 format_func=lambda t: LOCK_WORDS.get(t, t.value))  # fmt: skip
        lock_date = c3.date_input("On", date.today(), key="locked_date")
        rate = _rate_input(c4, "Rate", 0.0, "locked_rate")
        submitted = st.form_submit_button("Lock rate", key="locked_submit")

    if submitted:
        data = {
            "pair": pair,
            "rate": to_decimal(rate),
            "lock_date": lock_date,
            "lock_type": lock_type,
        }
        run_action(
            lambda session: svc.lock_rate(session, LockedRateCreate.model_validate(data), actor),
            f"Locked the {pair} {LOCK_WORDS.get(lock_type, lock_type.value).lower()} rate on "
            f"{fmt_day(lock_date)}.",
        )


# --------------------------------------------------------------------------- gazetted


def _gazetted_label(r: GazettedRateRecord) -> str:
    kind = "revision" if r.revision_of else "notification"
    return f"{r.pair} {r.period_start} to {r.period_end} at {r.rate.normalize()} ({kind})"


def _gazetted_rows(rates: list[GazettedRateRecord]) -> list[dict[str, Any]]:
    by_id = {r.id: r for r in rates}
    revised = {r.revision_of for r in rates if r.revision_of}
    return [
        {
            "Pair": r.pair,
            "From": fmt_day(r.period_start),
            "Until": fmt_day(r.period_end),
            "Rate": str(r.rate.normalize()),
            "Notified by": r.publishing_authority,
            "Revises": _gazetted_label(by_id[r.revision_of]) if r.revision_of else "-",
            "Revised later": "Yes" if r.id in revised else "",
        }
        for r in rates
    ]


def render_gazetted(rates: list[GazettedRateRecord], actor: str) -> None:
    theme.lead("Rates the customs authority notifies for each fortnight, used for port "
               "charges and assessment. To correct one, publish a revision of it; the revision "
               "wins on the days it covers.")  # fmt: skip
    theme.table(_gazetted_rows(rates), empty="No notified rates yet.")
    by_label = {_gazetted_label(r): r for r in rates}
    choice = st.selectbox("Revises", [NEW_NOTIFICATION, *by_label], key="gazetted_revises")
    parent = by_label.get(choice)
    suffix = "new" if parent is None else str(parent.id)

    with st.form(f"gazetted_form_{suffix}"):
        st.markdown("**Publish a revision**" if parent else "**Publish a notification**")
        c1, c2, c3 = st.columns(3)
        pair = c1.text_input(
            "Pair (BASE/QUOTE)",
            parent.pair if parent else DEFAULT_PAIR,
            key=f"gazetted_pair_{suffix}",
        )
        rate = _rate_input(
            c2, "Rate", float(parent.rate) if parent else 0.0, f"gazetted_rate_{suffix}"
        )
        authority = c3.text_input(
            "Notified by",
            parent.publishing_authority if parent else "CBIC (simulated)",
            key=f"gazetted_authority_{suffix}",
        )
        start = c1.date_input(
            "From",
            parent.period_start if parent else date.today(),
            key=f"gazetted_start_{suffix}",
        )
        end = c2.date_input(
            "Until (inclusive)",
            parent.period_end if parent else date.today(),
            key=f"gazetted_end_{suffix}",
        )
        submitted = st.form_submit_button(
            "Publish revision" if parent else "Publish notification",
            key=f"gazetted_submit_{suffix}",
        )

    if submitted:
        data = {
            "pair": pair,
            "rate": to_decimal(rate),
            "period_start": start,
            "period_end": end,
            "publishing_authority": authority,
            "revision_of": parent.id if parent else None,
        }
        run_action(
            lambda session: svc.publish_gazetted_rate(
                session, GazettedRateCreate.model_validate(data), actor
            ),
            f"Published the {pair} rate for {fmt_day(start)} to {fmt_day(end)}"
            + (" (revision)." if parent else "."),
        )


# --------------------------------------------------------------------------- page

operator = st.sidebar.text_input("Your name (for the audit trail)", "ops-dashboard", key="actor")
actor_name = operator.strip() or "ops-dashboard"
with session_scope() as db:
    known_pairs = svc.list_pairs(db)
    spot_rates = svc.list_spot_rates(db)
    locked_rates = svc.list_locked_rates(db)
    gazetted_rates = svc.list_gazetted_rates(db)

resolve_tab, gazetted_tab, locked_tab, spot_tab = theme.tabs(
    ["Find the rate", "Customs-notified", "Locked", "Spot"], key="fx_tabs"
)
with resolve_tab:
    render_resolver(known_pairs)
with gazetted_tab:
    render_gazetted(gazetted_rates, actor_name)
with locked_tab:
    render_locked(locked_rates, actor_name)
with spot_tab:
    render_spot(spot_rates, known_pairs, actor_name)
