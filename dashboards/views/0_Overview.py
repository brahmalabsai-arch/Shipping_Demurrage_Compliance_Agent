"""Overview — the front door for someone seeing the desk for the first time: what is waiting on a
person, where the shipments are on their route, what the agents saved, and how a shipment moves
through the pages. Read-only; every figure is live from the database.
"""

from collections import Counter

import streamlit as st

from dashboards import theme
from dashboards.ui import fmt_day
from models.enums import RunState, ShipmentStatus
from roi.calculator import compare
from services import compliance_service, governance_service, roi_service, shipping_service
from services.db import session_scope

theme.setup_page(
    "Port & Customs Clearance Copilot",
    "Agents check each import shipment's papers, price its time in port and ask the approved "
    "rulebook. People approve rule changes and filings. Everything on this page is live.",
    tab_title="Overview",
    icon=":material/space_dashboard:",
)

ROUTE: list[tuple[ShipmentStatus, str]] = [
    (ShipmentStatus.QUOTED, "Quoted"),
    (ShipmentStatus.BOOKED, "Booked"),
    (ShipmentStatus.IN_TRANSIT, "In transit"),
    (ShipmentStatus.DOCKED, "Docked"),
    (ShipmentStatus.UNDER_REVIEW, "Under review"),
    (ShipmentStatus.CLEARED, "Cleared"),
    (ShipmentStatus.CLOSED, "Closed"),
]

with session_scope() as db:
    shipments = shipping_service.list_shipments(db)
    pending_rules = compliance_service.list_pending(db)
    open_runs = governance_service.list_runs(db, open_only=True)
    snapshots = roi_service.list_snapshots(db)

by_status = Counter(s.status for s in shipments)
at_compliance = [r for r in open_runs if r.state is RunState.AWAITING_COMPLIANCE]
at_filing = [r for r in open_runs if r.state is RunState.AWAITING_FILING]

left, right = st.columns([3, 2], gap="large")
with left:
    theme.section("Where the shipments are",
                  f"{len(shipments)} shipment(s) to Nhava Sheva, by stage of their journey.")  # fmt: skip
    theme.route([(label, str(by_status.get(status, 0))) for status, label in ROUTE])
    asides = []
    if held := by_status.get(ShipmentStatus.HELD, 0):
        asides.append((f"{held} held by customs", "warn"))
    if cancelled := by_status.get(ShipmentStatus.CANCELLED, 0):
        asides.append((f"{cancelled} cancelled", "quiet"))
    if asides:
        theme.chips(asides)  # type: ignore[arg-type]

with right, st.container(key=theme.focus_key("waiting")):
    st.markdown("### Waiting on a person")
    waiting = len(pending_rules) + len(at_compliance) + len(at_filing)
    if not waiting:
        st.markdown("Nothing is waiting on anyone right now.")
    if pending_rules:
        st.markdown(f"**{len(pending_rules)} rule change(s)** proposed from the customs bulletin "
                    "need a reviewer's decision.")  # fmt: skip
        theme.link("views/4_Compliance.py", "Review on Compliance", ":material/gavel:")
    if at_compliance or at_filing:
        parts = []
        if at_filing:
            parts.append(f"**{len(at_filing)}** ready to be approved for filing")
        if at_compliance:
            parts.append(f"**{len(at_compliance)}** waiting for a rule decision")
        st.markdown("Clearance runs: " + "; ".join(parts) + ".")
        theme.link(
            "views/1_Shipping_Ops.py", "Decide on Shipping Ops", ":material/directions_boat:"
        )

theme.section("What the agents saved",
              "Demurrage avoided by catching document problems before a box docks, from the "
              "latest frozen snapshot of the data.")  # fmt: skip
if snapshots:
    latest = snapshots[0]
    result = compare(latest, latest)
    theme.figure(
        f"INR {result.total_avoided_inr:,}",
        f"Snapshot “{latest.name}”, priced as of {fmt_day(latest.as_of)}: "
        f"{len(result.lines)} shipment(s), every figure rebuilt on Mission Control.",
    )
else:
    st.markdown('<p class="cc-note">No snapshot yet. Capture one on Mission Control, Savings tab, '
                "to see the saving.</p>", unsafe_allow_html=True)  # fmt: skip
with st.container(key="cta_mission"):
    theme.link("views/6_Mission_Control.py", "Open Mission Control", ":material/arrow_outward:")

theme.section("How a shipment moves through the desk")
theme.steps([
    ("Papers checked", "The invoice, packing list and bill of lading are compared with each "
     "other and with the booking. Shipping Ops."),
    ("Time in port priced", "Free days, the daily tariff and the customs exchange rate turn "
     "the stay into rupees. Demurrage and FX Rates."),
    ("Rulebook asked", "Duty, documents and special requirements come from the approved rules "
     "only, with the rule cited. Compliance."),
    ("A person files", "A named reviewer approves rule changes and the filing; nothing is "
     "cleared without them. Shipping Ops."),
])  # fmt: skip

theme.section("Data the agents read",
              "Each source is editable here, and the agents see an edit at once.")  # fmt: skip
c1, c2, c3 = st.columns(3)
with c1:
    theme.link("views/2_Demurrage.py", "Port tariffs", ":material/hourglass_top:")
    st.caption("Free days and daily charges per container type and period.")
with c2:
    theme.link("views/3_FX_Rates.py", "Exchange rates", ":material/currency_exchange:")
    st.caption("Spot, locked and customs-notified rates for each stage of a shipment.")
with c3:
    theme.link("views/5_Customs_Bulletin.py", "Customs bulletin", ":material/campaign:")
    st.caption("The simulated public notice the regulatory watch scans for changes.")
