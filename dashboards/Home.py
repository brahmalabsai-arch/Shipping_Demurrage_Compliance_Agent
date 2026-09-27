"""Port & Customs Clearance Copilot — dashboard home. Run: ``streamlit run dashboards/Home.py``."""

import streamlit as st

st.set_page_config(page_title="Customs Copilot", layout="wide")

st.title("Port & Customs Clearance Copilot")
st.markdown("""
Each page edits one data domain through the same service layer the MCP servers use, so what
you change here is exactly what the agents see.

| Page | Domain | Phase |
|---|---|---|
| **Shipping Ops** | Shipments, documents, lifecycle status | 1 |
| Demurrage | Port demurrage/detention rates | 2 |
| FX Rates | Spot, locked and gazetted exchange rates | 3 |
| Compliance | Rulebook and pending rule changes | 4 |
| Customs Bulletin | Simulated external bulletin | 4 |
| Mission Control | Agent runs, audit trail, ROI | 12 |

All times are shown in IST (stored in UTC).
""")
