"""Customs Bulletin — edit the simulated external bulletin and inspect what scans have seen.

Editing here plays the part of the customs authority publishing a new bulletin. It goes through
``bulletin_service.publish`` and deliberately does **not** capture a snapshot: the Regulatory
Watch agent only learns about an edit at its next scheduled scan (edge cases, Phase 6,
Option A), so an edit reverted before that scan is never seen. "Run scheduled scan now" fires
that same scheduler job on demand (capture + agent): it is the scan path, not the save path.
"""

import asyncio
import hashlib
from typing import Any

import streamlit as st

from agents.state import WatchState
from dashboards import theme
from dashboards.ui import flash, fmt_day, fmt_ist, problem_text, run_action
from models.schemas import BulletinSnapshotRecord, ChangeLogEntryRecord, RuleDrift
from scheduler import watch_scheduler
from services import bulletin_service as svc
from services import compliance_service
from services.db import session_scope
from services.errors import BulletinNotPublishedError

FORMAT_HELP = """\
Free text first, then one section per rule:

```
## HSN-8708-duty
Effective: 2026-04-01
HS codes: 8708, 870830      (or: all)
Rule text, one or more lines.
```
"""

theme.setup_page(
    "Customs Bulletin",
    "A stand-in for the customs authority's public notice. Editing it here plays the "
    "authority; the regulatory watch only notices at its next scan, then raises any change "
    "for a reviewer.",
    icon=":material/campaign:",
)


def render_published(content: str | None, updated: str) -> None:
    if content is None:
        st.info("Nothing published yet. Publish on the Edit tab, or load the demo data with "
                "python -m scripts.seed.")  # fmt: skip
        return
    st.caption(updated)
    sections = svc.parse_sections(content)  # stored text was validated on publish
    theme.table(
        [
            {
                "Rule": s.rule_key,
                "Takes effect": fmt_day(s.effective_date),
                "Covers": ", ".join(s.hs_codes) or "all goods",
                "Text": s.text,
            }
            for s in sections
        ]
    )
    with theme.expander("The full text as published", key="published_text_open"):
        st.markdown(content)


def render_editor(content: str | None, actor: str) -> None:
    theme.lead("Publishing replaces the bulletin text. No scan runs now: the regulatory watch "
               "sees the change at its next scheduled scan.")  # fmt: skip
    with theme.expander("How sections are written", key="format_help_open"):
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
            "Bulletin published. The next scheduled scan will pick it up.",
        )


def _snapshot_row(s: BulletinSnapshotRecord, published: str | None) -> dict[str, Any]:
    return {
        "Scan": s.seq,
        "When": fmt_ist(s.captured_at),
        "Length": f"{len(s.content):,} characters",
        "Same as the bulletin now": "Yes" if s.content == published else "No",
    }


def _change_row(c: ChangeLogEntryRecord) -> dict[str, Any]:
    return {
        "Noticed": fmt_ist(c.detected_at),
        "Real change": "Yes" if c.is_substantive else "Formatting only",
        "Summary": c.diff_summary,
        "Reviewer emailed": fmt_ist(c.emailed_at) if c.emailed_at else "",
    }


def _scan_report(state: WatchState | None) -> str:
    if state is None:
        return "Scan skipped: nothing has been published yet."
    if state.proposals:
        report = (
            f"Scan complete: {len(state.proposals)} proposal(s) pending review on the Compliance "
            f"page, {len(state.emails)} review email(s) sent."
        )
    elif state.outcome in ("no_change", "formatting_only"):
        kind = "no change" if state.outcome == "no_change" else "a formatting-only change"
        report = f"Scan complete: {kind} since the previous scan. Nothing new to review."
    else:
        report = "Scan complete: the bulletin changed, but there is nothing new to review."
    if state.email_failures:
        report += f" Emails that failed (the proposals are kept): {'; '.join(state.email_failures)}"
    not_proposed = [
        *state.skipped,
        *(f"{k}: removed from the bulletin" for k in state.removed_sections),
    ]
    if not_proposed:
        report += f" Not proposed: {'; '.join(not_proposed)}."
    return report


def render_scan_button() -> None:
    with st.container(key=theme.field_key("scan")):
        st.markdown("**Run the regulatory watch now.** The same scan the scheduler runs every "
                    "15 minutes: read the bulletin, compare it with the approved rulebook, "
                    "propose any change for review and email the reviewer.")  # fmt: skip
        if not st.button("Run scheduled scan now", key="run_scan", type="primary"):
            return
        try:
            with st.spinner("Scanning the bulletin..."):
                state = asyncio.run(watch_scheduler.run_scan())
        except Exception as exc:  # show any failure; the scheduled job would log and audit it
            st.error(f"The scan could not finish: {problem_text(exc)}")
            return
        flash(_scan_report(state))
        st.rerun()


def _drift_status(drift: RuleDrift) -> str:
    if drift.pending_same is not None:
        return f"waiting for review as v{drift.pending_same.version}"
    if (rejected := drift.rejected_same) is not None:
        return f"turned down as v{rejected.version}: {rejected.decision_note or '-'}"
    return "not proposed yet (the next scan will)"


def render_drift(drifts: list[RuleDrift]) -> None:
    theme.section("Where the bulletin and the rulebook differ",
                  "Rules whose text in the latest scan is not the rule in force (spacing and "
                  "capitals aside). This is what a reviewer judges.")  # fmt: skip
    if not drifts:
        st.success("The latest scan matches the approved rulebook.")
        return
    theme.table(
        [
            {
                "Rule": d.rule_key,
                "In force": f"v{d.in_force.version}" if d.in_force else "(a new rule)",
                "Status": _drift_status(d),
                "Bulletin text (latest scan)": d.section.text,
            }
            for d in drifts
        ]
    )


def render_scans(
    snapshots: list[BulletinSnapshotRecord],
    changes: list[ChangeLogEntryRecord],
    drifts: list[RuleDrift],
    published: str | None,
) -> None:
    render_scan_button()
    if not snapshots:
        st.info("No scan has run yet.")
        return
    render_drift(drifts)
    theme.section("Scans")
    theme.table([_snapshot_row(s, published) for s in snapshots])
    theme.section("What changed between consecutive scans",
                  "Changes in the bulletin itself from one scan to the next. What would change "
                  "in the rulebook is the table above, and the Compliance page.")  # fmt: skip
    if not changes:
        st.info("No change between consecutive scans.")
        return
    theme.table([_change_row(c) for c in changes])
    labels = {f"{fmt_ist(c.detected_at)}: {c.diff_summary.splitlines()[0]}": c for c in changes}
    chosen = labels[st.selectbox("Show the difference for", list(labels), key="change_select")]
    with session_scope() as session:
        old, new = svc.change_texts(session, chosen.id)
    st.code(svc.unified_diff(old, new), language="diff")


editor = st.sidebar.text_input(
    "Your name, as the publisher (audit trail)", "bulletin-editor", key="actor"
)
with session_scope() as db:
    try:
        publication = svc.get_publication(db)
        published_text: str | None = publication.content
        updated_note = (
            f"Last published {fmt_ist(publication.updated_at)} by {publication.updated_by}."
        )
    except BulletinNotPublishedError:
        published_text, updated_note = None, ""
    recent_snapshots = svc.list_snapshots(db, limit=20)
    detected_changes = svc.list_changes(db)
    rule_drifts = compliance_service.bulletin_drift(db) if recent_snapshots else []

scans_tab, published_tab, edit_tab = theme.tabs(
    ["Scans and changes", "Published", "Edit"], key="bulletin_tabs"
)
with scans_tab:
    render_scans(recent_snapshots, detected_changes, rule_drifts, published_text)
with published_tab:
    render_published(published_text, updated_note)
with edit_tab:
    render_editor(published_text, editor.strip() or "bulletin-editor")
