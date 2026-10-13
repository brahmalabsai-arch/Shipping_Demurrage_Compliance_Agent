"""Compliance — review pending rule changes, browse the rulebook in force, propose changes, and
ask the Compliance agent (RAG over the approved rules in force, with citations).

Reads and writes go through ``services.compliance_service`` — the same functions
``compliance_rulebook_mcp`` and the agents use. A proposal (from here or from the Regulatory
Watch agent) is always ``pending``; only the Approve / Reject buttons, which call
``compliance_service.decide`` with the reviewer chosen in the sidebar (a registered reviewer,
``REVIEWERS``), change a rule's status. An approval in force can later be revoked (a separate,
audited act: the version it replaced comes back in force). Clearance runs waiting on a decided
rule are resumed from Shipping Ops.
A proposal is always shown against the **rule in force** (``compliance_service.review_context``)
with the history since it; a rejection must give a reason, stored with the decision.
"""

import asyncio
from datetime import date
from typing import Any
from uuid import UUID

import streamlit as st
from sqlalchemy.orm import Session

from agents import compliance_rag_agent
from agents.state import ComplianceFinding
from dashboards import theme
from dashboards.ui import agent_name, fmt_day, fmt_ist, problem_text, run_action
from models.enums import RuleDecision, RuleStatus
from models.schemas import ComplianceRuleRecord, ReviewContext, RuleProposal
from services import audit_service, bulletin_service, governance_service
from services import compliance_service as svc
from services.db import session_scope

NEW_RULE = "(a new rule)"
REVIEWERS: list[str] = [r.name for r in governance_service.reviewers()]
STATUS_TONES: dict[RuleStatus, theme.Tone] = {
    RuleStatus.APPROVED: "ok",
    RuleStatus.PENDING: "warn",
    RuleStatus.REJECTED: "stop",
    RuleStatus.REVOKED: "quiet",
}

theme.setup_page(
    "Compliance",
    "The approved customs rulebook the agents answer from. Proposed changes wait here until a "
    "named reviewer approves or rejects them; nothing reaches the rulebook any other way.",
    icon=":material/gavel:",
)


def _codes(rule: ComplianceRuleRecord) -> str:
    return ", ".join(rule.hs_codes) or "all goods"


def _parse_codes(raw: str) -> list[str]:
    cleaned = raw.strip()
    if cleaned.lower() in {"", "all", "all goods"}:
        return []
    return [c.strip() for c in cleaned.split(",") if c.strip()]


# --------------------------------------------------------------------------- pending review


def _decide(rule: ComplianceRuleRecord, decision: RuleDecision, reviewer: str, note: str) -> None:
    verb = "Approved" if decision is RuleDecision.APPROVE else "Rejected"
    run_action(
        lambda session: svc.decide(session, rule.id, decision, reviewer, note),
        f"{verb} {rule.rule_key} v{rule.version} (decided by {reviewer.strip()}).",
    )


def _render_against_in_force(rule: ComplianceRuleRecord, in_force: ComplianceRuleRecord) -> None:
    st.markdown(f"**What would change** compared with the rule in force (version "
                f"{in_force.version}):")  # fmt: skip
    meta = []
    if in_force.effective_date != rule.effective_date:
        meta.append(f"takes effect {fmt_day(rule.effective_date)} instead of "
                    f"{fmt_day(in_force.effective_date)}")  # fmt: skip
    if in_force.hs_codes != rule.hs_codes:
        meta.append(f"covers {_codes(rule)} instead of {_codes(in_force)}")
    if meta:
        st.markdown("It " + "; ".join(meta) + ".")
    diff = bulletin_service.unified_diff(
        in_force.content,
        rule.content,
        f"v{in_force.version} (in force)",
        f"v{rule.version} (proposed)",
    )
    st.code(diff or "(the text is unchanged)", language="diff")


def _render_waiting_runs(rule: ComplianceRuleRecord) -> None:
    with session_scope() as session:
        runs = governance_service.runs_awaiting_rule(session, rule.id)
    if runs:
        st.info(f"{len(runs)} clearance run(s) are waiting on this decision. Resume them on "
                "Shipping Ops, in the shipment's Clearance tab, once it is decided.")  # fmt: skip


WATCH = "regulatory_watch_agent"


def _proposed_by(session: Session, rule: ComplianceRuleRecord) -> str:
    """Who proposed the rule, from the audit trail (the rule row does not keep it). The very
    first scan has no earlier scan to diff against, so the proposer, not the change link, tells
    a scan's proposal from one made by hand."""
    events = audit_service.query_events(session, action="compliance_rule.proposed",
                                        entity_type="compliance_rule", entity_id=rule.id, limit=1)  # fmt: skip
    if not events:
        return "the bulletin scan" if rule.source_diff_id else "not recorded"
    actor = events[0].actor
    return "Regulatory watch (bulletin scan)" if actor == WATCH else f"{agent_name(actor)}, by hand"


def _render_proposal(context: ReviewContext, reviewer: str | None, proposed_by: str) -> None:
    rule, in_force = context.proposal, context.in_force
    with st.container(key=theme.focus_key(f"proposal_{rule.id}")):
        st.markdown(f"#### {rule.rule_key}, version {rule.version}")
        theme.facts([
            ("Proposed", fmt_ist(rule.created_at)),
            ("Takes effect", fmt_day(rule.effective_date)),
            ("Covers", _codes(rule)),
            ("Proposed by", proposed_by),
        ])  # fmt: skip
        if rule.change_summary:
            st.caption(f"Summary when proposed: {rule.change_summary}")
        _render_waiting_runs(rule)
        for line in context.history:
            st.warning(line)
        if in_force is None:
            st.info("A new rule: nothing is approved for it yet.")
            st.markdown(rule.content)
        else:
            _render_against_in_force(rule, in_force)
        note = st.text_input(
            "Reason (required to reject, optional to approve)", "", key=f"note_{rule.id}"
        )
        b1, b2, _ = st.columns([1, 1, 3])
        undecidable = reviewer is None
        if b1.button("Approve", key=f"approve_{rule.id}", type="primary", disabled=undecidable):
            _decide(rule, RuleDecision.APPROVE, reviewer or "", note)
        if b2.button("Reject", key=f"reject_{rule.id}", disabled=undecidable):
            _decide(rule, RuleDecision.REJECT, reviewer or "", note)


def render_pending(
    contexts: list[ReviewContext], reviewer: str | None, proposers: dict[UUID, str]
) -> None:
    if not contexts:
        st.info("Nothing is waiting for review.")
        return
    theme.lead(f"{len(contexts)} proposed change(s), oldest first. Each is shown against the "
               "rule in force, with what happened to earlier proposals.")  # fmt: skip
    if reviewer is None:
        st.warning("Choose yourself as Reviewer in the sidebar before deciding.")
    for context in contexts:
        _render_proposal(context, reviewer, proposers[context.proposal.id])


# --------------------------------------------------------------------------- rulebook


def render_revoke(approved: list[ComplianceRuleRecord], reviewer: str | None) -> None:
    """Withdraw an approval in force (a new, audited act; the approval itself is kept)."""
    with theme.expander("Revoke an approval", key="revoke_open"):
        st.caption("The version it replaced, if any, comes back in force; with none, the rule "
                   "has no version in force until a new proposal is approved. This is final.")  # fmt: skip
        by_label = {f"{r.rule_key} v{r.version}": r for r in approved}
        picked = st.selectbox("Rule in force", list(by_label), key="revoke_pick")
        reason = st.text_input("Reason (required)", "", key="revoke_reason")
        if st.button("Revoke approval", key="revoke_submit", disabled=reviewer is None):
            rule = by_label[picked]
            run_action(
                lambda session: svc.revoke_decision(session, rule.id, reviewer or "", reason),
                f"Revoked {picked} (by {reviewer}).",
            )


def render_rulebook(approved: list[ComplianceRuleRecord]) -> None:
    hs_filter = st.text_input("Only rules that cover this HS code", "", key="rulebook_hs").strip()
    shown = [r for r in approved if r.applies_to(hs_filter)] if hs_filter else approved
    theme.table(
        [
            {
                "Rule": r.rule_key,
                "Version": r.version,
                "Covers": _codes(r),
                "Takes effect": fmt_day(r.effective_date),
                "Rule text": r.content,
                "Approved by": r.decided_by,
                "Approved on": fmt_ist(r.decided_at),
            }
            for r in shown
        ],
        empty="No approved rule covers that.",
    )


def render_history(rules: list[ComplianceRuleRecord]) -> None:
    version_of = {r.id: f"v{r.version}" for r in rules}
    theme.table(
        [
            {
                "Rule": r.rule_key,
                "Version": r.version,
                "Status": r.status.value.capitalize(),
                "Replaced by": version_of.get(r.superseded_by, "") if r.superseded_by else "",
                "Takes effect": fmt_day(r.effective_date),
                "Proposed": fmt_ist(r.created_at),
                "Decided by": r.decided_by or "",
                "Decided on": fmt_ist(r.decided_at) if r.decided_at else "",
                "Reason": r.decision_note or "",
                "Revoked by": r.revoked_by or "",
                "Revoked on": fmt_ist(r.revoked_at) if r.revoked_at else "",
                "Revocation reason": r.revoke_note or "",
                "Summary": r.change_summary or "",
            }
            for r in rules
        ]
    )


# --------------------------------------------------------------------------- propose


def render_propose(approved: list[ComplianceRuleRecord], actor: str) -> None:
    theme.lead("A proposal is saved as a new pending version. It changes nothing until a "
               "reviewer approves it under Waiting for review.")  # fmt: skip
    by_key = {r.rule_key: r for r in approved}
    choice = st.selectbox("Start from", [NEW_RULE, *by_key], key="propose_template")
    base = by_key.get(choice)
    suffix = "new" if base is None else base.rule_key

    with st.form(f"propose_{suffix}"):
        c1, c2, c3 = st.columns(3)
        rule_key = c1.text_input(
            "Rule name", base.rule_key if base else "", key=f"propose_key_{suffix}"
        )
        effective = c2.date_input(
            "Takes effect",
            base.effective_date if base else date.today(),
            key=f"propose_effective_{suffix}",
        )
        codes = c3.text_input(
            "Covers HS codes (comma-separated, or all)",
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
            f"Proposed a new version of {rule_key.strip()}. It is waiting for review.",
        )


def render_ask() -> None:
    """Ask the Compliance agent (RAG over the approved rulebook in force, with citations)."""
    theme.lead("Answers come only from the approved rules in force, and cite the rule they use. "
               "If no rule covers the question, the answer says so rather than guessing.")  # fmt: skip
    with st.form("ask_rulebook"):
        question = st.text_input("Question", key="ask_question",
                                 placeholder="What basic customs duty is charged on motor "
                                 "vehicle parts?")  # fmt: skip
        hs_code = st.text_input("HS code (optional: only rules that cover it)", key="ask_hs")
        asked = st.form_submit_button("Ask the rulebook", key="ask_submit")
    if asked and question.strip():
        try:
            with st.spinner("Searching the approved rulebook..."):
                st.session_state["ask_result"] = asyncio.run(
                    compliance_rag_agent.ask(question.strip(), hs_code.strip() or None)
                )
        except Exception as exc:  # show any failure to the operator
            st.error(f"The question could not be answered: {problem_text(exc)}")
    finding: ComplianceFinding | None = st.session_state.get("ask_result")
    if finding is None:
        return
    st.markdown(f"**You asked:** {finding.question}")
    if finding.found:
        st.success(finding.answer)
    else:
        st.info(finding.answer)
    if finding.method == "template" and finding.fallback_reason:
        st.caption(f"The rule is quoted as written: {finding.fallback_reason}")
    if finding.citations:
        theme.table(
            [
                {"Rule": c.rule_key, "Version": c.version,
                 "Takes effect": fmt_day(c.effective_date), "Relevance": round(c.score, 3)}
                for c in finding.citations
            ]
        )  # fmt: skip


# --------------------------------------------------------------------------- page

reviewer_name: str | None = st.sidebar.selectbox(
    "Reviewer (recorded with each decision)", REVIEWERS, index=None, key="reviewer",
    placeholder="Choose a registered reviewer",
)  # fmt: skip
if not REVIEWERS:
    st.sidebar.warning("No reviewers are set up. Add REVIEWERS to .env.")
proposer = st.sidebar.text_input(
    "Your name, for proposals (audit trail)", "compliance-dashboard", key="actor"
)
with session_scope() as db:
    pending_rules = svc.list_pending(db)
    review_contexts = [svc.review_context(db, rule.id) for rule in pending_rules]
    proposers = {rule.id: _proposed_by(db, rule) for rule in pending_rules}
    approved_rules = svc.list_approved(db)
    all_rules = svc.list_rules(db)

pending_tab, rulebook_tab, ask_tab, propose_tab, history_tab = theme.tabs(
    [f"Waiting for review ({len(pending_rules)})", "Rulebook in force", "Ask the rulebook",
     "Propose a change", "All versions"],
    key="compliance_tabs",
)  # fmt: skip
with pending_tab:
    render_pending(review_contexts, reviewer_name, proposers)
with rulebook_tab:
    render_rulebook(approved_rules)
    render_revoke(approved_rules, reviewer_name)
with ask_tab:
    render_ask()
with propose_tab:
    render_propose(approved_rules, proposer.strip() or "compliance-dashboard")
with history_tab:
    render_history(all_rules)
