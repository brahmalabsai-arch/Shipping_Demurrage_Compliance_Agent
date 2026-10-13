"""Port & Customs Clearance Copilot — the dashboards. Run: ``streamlit run dashboards/Home.py``.

This entry point only routes: a top navigation bar over the pages in ``dashboards/views/``
(not ``pages/``: Streamlit would auto-list that folder and, right after a restart, run its
files directly, bypassing this router).
Each page sets up the theme, sidebar and header itself (``dashboards.theme.setup_page``), so a
page also runs on its own (the test harness opens pages directly). URL paths are unchanged
(``/Shipping_Ops``, ``/Mission_Control``, ...).
"""

import streamlit as st

PAGES = [
    st.Page("views/0_Overview.py", title="Overview", icon=":material/space_dashboard:",
            default=True),
    st.Page("views/6_Mission_Control.py", title="Mission Control", icon=":material/monitoring:",
            url_path="Mission_Control"),
    st.Page("views/1_Shipping_Ops.py", title="Shipping Ops", icon=":material/directions_boat:",
            url_path="Shipping_Ops"),
    st.Page("views/4_Compliance.py", title="Compliance", icon=":material/gavel:",
            url_path="Compliance"),
    st.Page("views/2_Demurrage.py", title="Demurrage", icon=":material/hourglass_top:",
            url_path="Demurrage"),
    st.Page("views/3_FX_Rates.py", title="FX Rates", icon=":material/currency_exchange:",
            url_path="FX_Rates"),
    st.Page("views/5_Customs_Bulletin.py", title="Customs Bulletin", icon=":material/campaign:",
            url_path="Customs_Bulletin"),
]  # fmt: skip

st.navigation(PAGES, position="top").run()
