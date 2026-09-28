"""FX Rates — resolve a rate for a lifecycle stage, and edit the spot / locked / gazetted tables.

Reads and writes go through ``services.fx_service`` — the same functions ``fx_rates_mcp`` and the
agents use — with no caching, so an edit here is what the next ``resolve_rate`` call sees.
Locked and gazetted rates are append-only: a lock can never change, and a gazetted rate is
corrected by publishing a revision of it.
"""

from datetime import date, time
from typing import Any

import streamlit as st

from dashboards.ui import fmt_ist, ist_to_utc, run_action, show_flash, to_decimal
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

st.set_page_config(page_title="FX Rates", layout="wide")

# Explicitly typed option lists: inline literals make mypy pick Streamlit's `-> None` overload.
FX_STAGES: list[FxStage] = list(FxStage)
LOCK_TYPES: list[LockType] = list(LockType)
INVOICE_KINDS: list[InvoiceKind] = list(InvoiceKind)
NEW_NOTIFICATION = "(new notification)"
DEFAULT_PAIR = "USD/INR"

STAGE_LABELS = {
    FxStage.QUOTATION: "Quotation (spot + validity window)",
    FxStage.BOL: "Bill of lading (locked on-board rate)",
    FxStage.INVOICE: "Invoice (commercial or freight)",
    FxStage.PORT_CHARGES: "Port charges (gazetted period)",
}
DATE_LABELS = {
    FxStage.QUOTATION: "Quote date",
    FxStage.BOL: "BoL on-board date",
    FxStage.INVOICE: "Invoice date",
    FxStage.PORT_CHARGES: "Date of port charges",
}


def _rate_input(container: Any, label: str, value: float, key: str) -> float:
    # No min_value on purpose: a zero/negative rate must be rejected by the Pydantic model and
    # the error shown inline, as on the Demurrage page.
    return float(container.number_input(label, value=value, step=0.0001, format="%.6f", key=key))


# --------------------------------------------------------------------------- resolve


def _show_resolved(resolved: ResolvedRate, check_on: date | None) -> None:
    st.success(
        f"{resolved.pair} at stage '{resolved.stage.value}' on {resolved.as_of}: "
        f"**{resolved.rate}** — {resolved.basis}."
    )
    st.caption(
        f"Source: fx.{resolved.source.value} row {resolved.source_id} · "
        f"{'locked (can never change)' if resolved.locked else 'not locked'}"
    )
    if resolved.expires is not None and check_on is not None:
        if svc.is_quote_expired(resolved, check_on):
            st.warning(
                f"Quote expired on {resolved.expires} (checked {check_on}) — re-quote at a "
                "fresh spot rate before pricing with it."
            )
        else:
            st.info(f"Quote valid through {resolved.expires} (checked {check_on}).")


def render_resolver(pairs: list[str]) -> None:
    st.subheader("Resolve the rate for a lifecycle stage")
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
                "Quote validity (days, 7-14)", value=14, step=1, key="resolve_validity"
            )
            check_on = c3.date_input("Check validity on", date.today(), key="resolve_check_on")
        elif stage is FxStage.INVOICE:
            kind = c3.selectbox("Invoice kind", INVOICE_KINDS, key="resolve_invoice_kind")
            bol_date = c3.date_input(
                "BoL date (freight invoice only)", None, key="resolve_bol_date"
            )
        submitted = st.form_submit_button("Resolve", key="resolve_submit")

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
        st.error(f"{type(exc).__name__}: {exc}")
        return
    _show_resolved(resolved, check_on)


# --------------------------------------------------------------------------- spot


def _spot_row(r: SpotRateRecord) -> dict[str, Any]:
    return {"Pair": r.pair, "Rate": str(r.rate), "Observed": fmt_ist(r.as_of), "Source": r.source}


def render_spot(rates: list[SpotRateRecord], pairs: list[str], actor: str) -> None:
    st.subheader("Spot rates (quotation stage)")
    pair_filter = st.selectbox("Pair", ["all", *pairs], key="spot_pair_filter")
    shown = rates if pair_filter == "all" else [r for r in rates if r.pair == pair_filter]
    if shown:
        st.dataframe([_spot_row(r) for r in shown], hide_index=True, width="stretch")
    else:
        st.info("No spot rates — add one below, or run `python -m scripts.seed`.")

    st.caption("An observation for the same pair and instant corrects the existing one (audited).")
    with st.form("spot_form"):
        c1, c2, c3 = st.columns(3)
        pair = c1.text_input("Pair (BASE/QUOTE)", DEFAULT_PAIR, key="spot_pair")
        rate = _rate_input(c2, "Rate (quote units per 1 base)", 0.0, "spot_rate")
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
            f"Saved {pair} spot rate observed {day} {at:%H:%M} IST.",
        )


# --------------------------------------------------------------------------- locked


def _locked_row(r: LockedRateRecord) -> dict[str, Any]:
    return {
        "Pair": r.pair,
        "Lock type": r.lock_type.value,
        "Lock date": r.lock_date.isoformat(),
        "Rate": str(r.rate),
    }


def render_locked(rates: list[LockedRateRecord], actor: str) -> None:
    st.subheader("Locked rates (BoL / commercial invoice)")
    if rates:
        st.dataframe([_locked_row(r) for r in rates], hide_index=True, width="stretch")
    else:
        st.info("No locked rates yet.")

    st.caption(
        "A locked rate is **immutable**: it cannot be edited or removed, and locking the same "
        "pair, type and date again is rejected."
    )
    with st.form("locked_form"):
        c1, c2, c3, c4 = st.columns(4)
        pair = c1.text_input("Pair (BASE/QUOTE)", DEFAULT_PAIR, key="locked_pair")
        lock_type = c2.selectbox("Lock type", LOCK_TYPES, key="locked_type")
        lock_date = c3.date_input("Lock date", date.today(), key="locked_date")
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
            f"Locked {pair} {lock_type} rate on {lock_date}.",
        )


# --------------------------------------------------------------------------- gazetted


def _gazetted_label(r: GazettedRateRecord) -> str:
    kind = "revision" if r.revision_of else "notification"
    return f"{r.pair} {r.period_start}..{r.period_end} @ {r.rate} ({kind})"


def _gazetted_rows(rates: list[GazettedRateRecord]) -> list[dict[str, Any]]:
    by_id = {r.id: r for r in rates}
    revised = {r.revision_of for r in rates if r.revision_of}
    return [
        {
            "Pair": r.pair,
            "Period start": r.period_start.isoformat(),
            "Period end": r.period_end.isoformat(),
            "Rate": str(r.rate),
            "Authority": r.publishing_authority,
            "Revision of": _gazetted_label(by_id[r.revision_of]) if r.revision_of else "—",
            "Revised later": "yes" if r.id in revised else "",
        }
        for r in rates
    ]


def render_gazetted(rates: list[GazettedRateRecord], actor: str) -> None:
    st.subheader("Gazetted rates (port charges / customs)")
    if rates:
        st.dataframe(_gazetted_rows(rates), hide_index=True, width="stretch")
    else:
        st.info("No gazetted rates yet.")

    st.caption(
        "Notifications for one pair may not overlap. To correct a published rate, choose it "
        "under *Revises* and publish a revision covering all or part of its period — the "
        "revision is preferred on the days it covers."
    )
    by_label = {_gazetted_label(r): r for r in rates}
    choice = st.selectbox("Revises", [NEW_NOTIFICATION, *by_label], key="gazetted_revises")
    parent = by_label.get(choice)
    suffix = "new" if parent is None else str(parent.id)

    with st.form(f"gazetted_form_{suffix}"):
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
            "Publishing authority",
            parent.publishing_authority if parent else "CBIC (simulated)",
            key=f"gazetted_authority_{suffix}",
        )
        start = c1.date_input(
            "Period start",
            parent.period_start if parent else date.today(),
            key=f"gazetted_start_{suffix}",
        )
        end = c2.date_input(
            "Period end (inclusive)",
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
            f"Published gazetted {pair} rate for {start}..{end}"
            + (" (revision)." if parent else "."),
        )


# --------------------------------------------------------------------------- page

st.title("FX Rates")
show_flash()
operator = st.sidebar.text_input(
    "Operator (recorded in the audit trail)", "ops-dashboard", key="actor"
)
actor_name = operator.strip() or "ops-dashboard"
with session_scope() as db:
    known_pairs = svc.list_pairs(db)
    spot_rates = svc.list_spot_rates(db)
    locked_rates = svc.list_locked_rates(db)
    gazetted_rates = svc.list_gazetted_rates(db)

resolve_tab, spot_tab, locked_tab, gazetted_tab = st.tabs(["Resolve", "Spot", "Locked", "Gazetted"])
with resolve_tab:
    render_resolver(known_pairs)
with spot_tab:
    render_spot(spot_rates, known_pairs, actor_name)
with locked_tab:
    render_locked(locked_rates, actor_name)
with gazetted_tab:
    render_gazetted(gazetted_rates, actor_name)
