"""Customs Bulletin — edit the simulated external bulletin and inspect what scans have seen.

Editing here plays the part of the customs authority publishing a new bulletin. It goes through
``bulletin_service.publish`` and deliberately does **not** capture a snapshot: the Regulatory
Watch agent only learns about an edit at its next scheduled scan (edge cases, Phase 6,
Option A), so an edit reverted before that scan is never seen.
"""

import hashlib
from typing import Any

import streamlit as st

from dashboards.ui import fmt_ist, run_action, show_flash
from models.schemas import BulletinSnapshotRecord, ChangeLogEntryRecord
from services import bulletin_service as svc
from services.db import session_scope
from services.errors import BulletinNotPublishedError

st.set_page_config(page_title="Customs Bulletin", layout="wide")

FORMAT_HELP = """\
Free text first, then one section per rule:

```
## HSN-8708-duty
Effective: 2026-04-01
HS codes: 8708, 870830      (or: all)
Rule text, one or more lines.
```
"""


def render_published(content: str | None, updated: str) -> None:
    st.subheader("As published")
    if content is None:
        st.info("Nothing published yet — publish below, or run `python -m scripts.seed`.")
        return
    st.caption(updated)
    sections = svc.parse_sections(content)  # stored text was validated on publish
    st.dataframe(
        [
            {
                "Rule": s.rule_key,
                "Effective": s.effective_date.isoformat(),
                "HS codes": ", ".join(s.hs_codes) or "all",
                "Text": s.text,
            }
            for s in sections
        ],
        hide_index=True,
        width="stretch",
    )
    with st.expander("Full text"):
        st.markdown(content)


def render_editor(content: str | None, actor: str) -> None:
    st.subheader("Edit and publish")
    st.caption(
        "Publishing replaces the bulletin text. No scan is triggered: the watch agent sees the "
        "change at its next scheduled scan."
    )
    with st.expander("Section format"):
        st.markdown(FORMAT_HELP)
    # Key the editor to the published text so a fresh publish (here or elsewhere) reloads it.
    version = hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:12]
    with st.form(f"bulletin_form_{version}"):
        text = st.text_area(
            "Bulletin text", content or "", height=480, key=f"bulletin_text_{version}"
        )
        submitted = st.form_submit_button("Publish", key="bulletin_publish")
    if submitted:
        run_action(
            lambda session: svc.publish(session, text, actor),
            "Bulletin published. It will be picked up at the next scheduled scan.",
        )


def _snapshot_row(s: BulletinSnapshotRecord, published: str | None) -> dict[str, Any]:
    return {
        "Scan #": s.seq,
        "Captured": fmt_ist(s.captured_at),
        "Characters": len(s.content),
        "Same as published now": "yes" if s.content == published else "no",
    }


def _change_row(c: ChangeLogEntryRecord) -> dict[str, Any]:
    return {
        "Detected": fmt_ist(c.detected_at),
        "Substantive": "yes" if c.is_substantive else "no",
        "Summary": c.diff_summary,
        "Emailed": fmt_ist(c.emailed_at) if c.emailed_at else "",
    }


def render_scans(
    snapshots: list[BulletinSnapshotRecord],
    changes: list[ChangeLogEntryRecord],
    published: str | None,
) -> None:
    st.subheader("What the scheduled scans have seen")
    if not snapshots:
        st.info("No scan has run yet (the Regulatory Watch scheduler arrives in Phase 6).")
        return
    st.dataframe([_snapshot_row(s, published) for s in snapshots], hide_index=True, width="stretch")
    st.subheader("Detected changes")
    if not changes:
        st.info("No change detected between consecutive scans.")
        return
    st.dataframe([_change_row(c) for c in changes], hide_index=True, width="stretch")
    labels = {f"{fmt_ist(c.detected_at)} - {c.diff_summary.splitlines()[0]}": c for c in changes}
    chosen = labels[st.selectbox("Show diff for", list(labels), key="change_select")]
    with session_scope() as session:
        old, new = svc.change_texts(session, chosen.id)
    st.code(svc.unified_diff(old, new), language="diff")


st.title("Customs Bulletin (simulated external source)")
show_flash()
editor = st.sidebar.text_input(
    "Editor (recorded in the audit trail)", "bulletin-editor", key="actor"
)
with session_scope() as db:
    try:
        publication = svc.get_publication(db)
        published_text: str | None = publication.content
        updated_note = (
            f"Last published {fmt_ist(publication.updated_at)} by {publication.updated_by}"
        )
    except BulletinNotPublishedError:
        published_text, updated_note = None, ""
    recent_snapshots = svc.list_snapshots(db, limit=20)
    detected_changes = svc.list_changes(db)

published_tab, edit_tab, scans_tab = st.tabs(["Published", "Edit", "Scans & changes"])
with published_tab:
    render_published(published_text, updated_note)
with edit_tab:
    render_editor(published_text, editor.strip() or "bulletin-editor")
with scans_tab:
    render_scans(recent_snapshots, detected_changes, published_text)
