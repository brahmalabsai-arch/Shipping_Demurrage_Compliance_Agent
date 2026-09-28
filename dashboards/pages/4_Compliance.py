"""Compliance — review pending rule changes, browse the rulebook in force, propose changes.

Reads and writes go through ``services.compliance_service`` — the same functions
``compliance_rulebook_mcp`` and the agents use. A proposal (from here or from the Regulatory
Watch agent) is always ``pending``; only the Approve / Reject buttons, which call
``compliance_service.decide`` with the reviewer named in the sidebar, change a rule's status.
"""

from datetime import date
from typing import Any

import streamlit as st

from dashboards.ui import fmt_ist, run_action, show_flash
from models.enums import RuleDecision
from models.schemas import ComplianceRuleRecord, RuleProposal
from services import bulletin_service
from services import compliance_service as svc
from services.db import session_scope

st.set_page_config(page_title="Compliance", layout="wide")

NEW_RULE = "(new rule)"


def _codes(rule: ComplianceRuleRecord) -> str:
    return ", ".join(rule.hs_codes) or "all"


def _parse_codes(raw: str) -> list[str]:
    cleaned = raw.strip()
    if cleaned.lower() in {"", "all"}:
        return []
    return [c.strip() for c in cleaned.split(",") if c.strip()]


# --------------------------------------------------------------------------- pending review


def _decide(rule: ComplianceRuleRecord, decision: RuleDecision, reviewer: str) -> None:
    verb = "Approved" if decision is RuleDecision.APPROVE else "Rejected"
    run_action(
        lambda session: svc.decide(session, rule.id, decision, reviewer),
        f"{verb} {rule.rule_key} v{rule.version} (decided by {reviewer.strip()}).",
    )


def _render_proposal(
    rule: ComplianceRuleRecord, current: ComplianceRuleRecord | None, reviewer: str
) -> None:
    with st.container(border=True):
        st.markdown(f"**{rule.rule_key} v{rule.version}** — proposed {fmt_ist(rule.created_at)}")
        if rule.change_summary:
            st.caption(rule.change_summary)
        c1, c2, c3 = st.columns(3)
        c1.write(f"Effective: {rule.effective_date}")
        c2.write(f"HS codes: {_codes(rule)}")
        c3.write("Source: bulletin change" if rule.source_diff_id else "Source: manual proposal")
        if current is None:
            st.info("New rule — no approved version yet.")
            st.write(rule.content)
        else:
            st.caption(f"Changes against the version in force (v{current.version}):")
            meta = []
            if current.effective_date != rule.effective_date:
                meta.append(f"effective date {current.effective_date} -> {rule.effective_date}")
            if current.hs_codes != rule.hs_codes:
                meta.append(f"HS codes {_codes(current)} -> {_codes(rule)}")
            if meta:
                st.write("; ".join(meta))
            diff = bulletin_service.unified_diff(current.content, rule.content)
            st.code(diff or "(text unchanged)", language="diff")
        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("Approve", key=f"approve_{rule.id}", type="primary"):
            _decide(rule, RuleDecision.APPROVE, reviewer)
        if b2.button("Reject", key=f"reject_{rule.id}"):
            _decide(rule, RuleDecision.REJECT, reviewer)


def render_pending(
    pending: list[ComplianceRuleRecord], approved: list[ComplianceRuleRecord], reviewer: str
) -> None:
    st.subheader("Pending review")
    if not pending:
        st.info("Nothing awaiting review.")
        return
    if not reviewer.strip():
        st.warning("Enter your name as *Reviewer* in the sidebar before deciding.")
    current = {r.rule_key: r for r in approved}
    for rule in pending:
        _render_proposal(rule, current.get(rule.rule_key), reviewer)


# --------------------------------------------------------------------------- rulebook


def render_rulebook(approved: list[ComplianceRuleRecord]) -> None:
    st.subheader("Rulebook in force (approved versions)")
    hs_filter = st.text_input("Filter by HS code", "", key="rulebook_hs").strip()
    shown = [r for r in approved if r.applies_to(hs_filter)] if hs_filter else approved
    if not shown:
        st.info("No approved rules match.")
        return
    st.dataframe(
        [
            {
                "Rule": r.rule_key,
                "Version": r.version,
                "HS codes": _codes(r),
                "Effective": r.effective_date.isoformat(),
                "Rule text": r.content,
                "Approved by": r.decided_by,
                "Approved at": fmt_ist(r.decided_at),
            }
            for r in shown
        ],
        hide_index=True,
        width="stretch",
    )


def render_history(rules: list[ComplianceRuleRecord]) -> None:
    st.subheader("All versions")
    version_of = {r.id: f"v{r.version}" for r in rules}
    st.dataframe(
        [
            {
                "Rule": r.rule_key,
                "Version": r.version,
                "Status": r.status.value,
                "Superseded by": version_of.get(r.superseded_by, "") if r.superseded_by else "",
                "Effective": r.effective_date.isoformat(),
                "Proposed": fmt_ist(r.created_at),
                "Decided by": r.decided_by or "",
                "Decided at": fmt_ist(r.decided_at) if r.decided_at else "",
                "Summary": r.change_summary or "",
            }
            for r in rules
        ],
        hide_index=True,
        width="stretch",
    )


# --------------------------------------------------------------------------- propose


def render_propose(approved: list[ComplianceRuleRecord], actor: str) -> None:
    st.subheader("Propose a change")
    st.caption(
        "A proposal is stored as a new **pending** version; it takes effect only once a "
        "reviewer approves it on the *Pending review* tab."
    )
    by_key = {r.rule_key: r for r in approved}
    choice = st.selectbox("Start from", [NEW_RULE, *by_key], key="propose_template")
    base = by_key.get(choice)
    suffix = "new" if base is None else base.rule_key

    with st.form(f"propose_{suffix}"):
        c1, c2, c3 = st.columns(3)
        rule_key = c1.text_input(
            "Rule key", base.rule_key if base else "", key=f"propose_key_{suffix}"
        )
        effective = c2.date_input(
            "Effective date",
            base.effective_date if base else date.today(),
            key=f"propose_effective_{suffix}",
        )
        codes = c3.text_input(
            "HS codes (comma-separated, or 'all')",
            _codes(base) if base else "all",
            key=f"propose_hs_{suffix}",
        )
        content = st.text_area(
            "Rule text", base.content if base else "", height=160, key=f"propose_content_{suffix}"
        )
        summary = st.text_input("What changed and why", "", key=f"propose_summary_{suffix}")
        submitted = st.form_submit_button("Submit for review", key=f"propose_submit_{suffix}")

    if submitted:
        data: dict[str, Any] = {
            "rule_key": rule_key,
            "content": content,
            "hs_codes": _parse_codes(codes),
            "effective_date": effective,
            "change_summary": summary,
        }
        run_action(
            lambda session: svc.propose_change(session, RuleProposal.model_validate(data), actor),
            f"Proposed a new version of {rule_key.strip()} — pending review.",
        )


# --------------------------------------------------------------------------- page

st.title("Compliance Rulebook")
show_flash()
reviewer_name = st.sidebar.text_input(
    "Reviewer (your name, recorded as decided_by)", "", key="reviewer"
)
proposer = st.sidebar.text_input(
    "Proposer (recorded in the audit trail)", "compliance-dashboard", key="actor"
)
with session_scope() as db:
    pending_rules = svc.list_pending(db)
    approved_rules = svc.list_approved(db)
    all_rules = svc.list_rules(db)

pending_tab, rulebook_tab, history_tab, propose_tab = st.tabs(
    [f"Pending review ({len(pending_rules)})", "Rulebook in force", "All versions", "Propose"]
)
with pending_tab:
    render_pending(pending_rules, approved_rules, reviewer_name)
with rulebook_tab:
    render_rulebook(approved_rules)
with history_tab:
    render_history(all_rules)
with propose_tab:
    render_propose(approved_rules, proposer.strip() or "compliance-dashboard")
