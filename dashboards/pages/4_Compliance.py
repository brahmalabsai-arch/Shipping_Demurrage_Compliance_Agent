"""Compliance — review pending rule changes, browse the rulebook in force, propose changes, and
ask the Compliance agent (RAG over the approved rules in force, with citations).

Reads and writes go through ``services.compliance_service`` — the same functions
``compliance_rulebook_mcp`` and the agents use. A proposal (from here or from the Regulatory
Watch agent) is always ``pending``; only the Approve / Reject buttons, which call
``compliance_service.decide`` with the reviewer chosen in the sidebar (a registered reviewer,
``REVIEWERS``), change a rule's status. An approval in force can later be revoked (a separate,
audited act — the version it replaced comes back in force). Clearance runs waiting on a decided
rule are resumed from Shipping Ops.
A proposal is always shown against the **rule in force** (``compliance_service.review_context``)
with the history since it; a rejection must give a reason, stored with the decision.
"""

import asyncio
from datetime import date
from typing import Any

import streamlit as st

from agents import compliance_rag_agent
from agents.state import ComplianceFinding
from dashboards.ui import fmt_ist, run_action, show_flash
from models.enums import RuleDecision
from models.schemas import ComplianceRuleRecord, ReviewContext, RuleProposal
from services import bulletin_service, governance_service
from services import compliance_service as svc
from services.db import session_scope

st.set_page_config(page_title="Compliance", layout="wide")

NEW_RULE = "(new rule)"
REVIEWERS: list[str] = [r.name for r in governance_service.reviewers()]


def _codes(rule: ComplianceRuleRecord) -> str:
    return ", ".join(rule.hs_codes) or "all"


def _parse_codes(raw: str) -> list[str]:
    cleaned = raw.strip()
    if cleaned.lower() in {"", "all"}:
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
    st.markdown(f"**What would change** — compared with the rule in force, v{in_force.version}:")
    meta = []
    if in_force.effective_date != rule.effective_date:
        meta.append(f"effective date {in_force.effective_date} -> {rule.effective_date}")
    if in_force.hs_codes != rule.hs_codes:
        meta.append(f"HS codes {_codes(in_force)} -> {_codes(rule)}")
    if meta:
        st.write("; ".join(meta))
    diff = bulletin_service.unified_diff(
        in_force.content,
        rule.content,
        f"v{in_force.version} (in force)",
        f"v{rule.version} (proposed)",
    )
    st.code(diff or "(text unchanged)", language="diff")


def _render_waiting_runs(rule: ComplianceRuleRecord) -> None:
    with session_scope() as session:
        runs = governance_service.runs_awaiting_rule(session, rule.id)
    if runs:
        st.info(f"{len(runs)} clearance run(s) are waiting on this decision — resume them on "
                "Shipping Ops → Clearance run once it is decided.")  # fmt: skip


def _render_proposal(context: ReviewContext, reviewer: str | None) -> None:
    rule, in_force = context.proposal, context.in_force
    with st.container(border=True):
        st.markdown(f"**{rule.rule_key} v{rule.version}** — proposed {fmt_ist(rule.created_at)}")
        c1, c2, c3 = st.columns(3)
        c1.write(f"Effective: {rule.effective_date}")
        c2.write(f"HS codes: {_codes(rule)}")
        c3.write(
            "Source: bulletin scan" if rule.source_diff_id else "Source: rulebook check / manual"
        )
        if rule.change_summary:
            st.caption(f"Summary when proposed: {rule.change_summary}")
        _render_waiting_runs(rule)
        for line in context.history:
            st.warning(line)
        if in_force is None:
            st.info("New rule — nothing is approved for it yet.")
            st.write(rule.content)
        else:
            _render_against_in_force(rule, in_force)
        note = st.text_input(
            "Reason (required to reject; optional to approve)", "", key=f"note_{rule.id}"
        )
        b1, b2, _ = st.columns([1, 1, 4])
        undecidable = reviewer is None
        if b1.button("Approve", key=f"approve_{rule.id}", type="primary", disabled=undecidable):
            _decide(rule, RuleDecision.APPROVE, reviewer or "", note)
        if b2.button("Reject", key=f"reject_{rule.id}", disabled=undecidable):
            _decide(rule, RuleDecision.REJECT, reviewer or "", note)


def render_pending(contexts: list[ReviewContext], reviewer: str | None) -> None:
    st.subheader("Pending review")
    if not contexts:
        st.info("Nothing awaiting review.")
        return
    if reviewer is None:
        st.warning("Choose yourself as *Reviewer* in the sidebar before deciding.")
    for context in contexts:
        _render_proposal(context, reviewer)


# --------------------------------------------------------------------------- rulebook


def render_revoke(approved: list[ComplianceRuleRecord], reviewer: str | None) -> None:
    """Withdraw an approval in force (a new, audited act; the approval itself is kept)."""
    with st.expander("Revoke an approval"):
        st.caption("The version it replaced (if any) comes back in force; with none, the rule "
                   "has no version in force until a new proposal is approved. Final.")  # fmt: skip
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
                "Reason": r.decision_note or "",
                "Revoked by": r.revoked_by or "",
                "Revoked at": fmt_ist(r.revoked_at) if r.revoked_at else "",
                "Revocation reason": r.revoke_note or "",
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


def render_ask() -> None:
    """Ask the Compliance agent (RAG over the approved rulebook in force, with citations)."""
    st.caption(
        "Answers come only from the approved rules in force (pending and rejected versions are "
        "never indexed): hybrid retrieval (BM25 + BGE embeddings, fused, then a cross-encoder "
        "rerank), then an LLM answer that must cite the rules it uses — or the rule quoted "
        'verbatim. If nothing relevant is found, the answer is "Not found in current '
        'rulebook."'
    )
    with st.form("ask_rulebook"):
        question = st.text_input("Question", key="ask_question")
        hs_code = st.text_input("HS code (optional — only rules covering it)", key="ask_hs")
        asked = st.form_submit_button("Ask the rulebook", key="ask_submit")
    if asked and question.strip():
        try:
            with st.spinner("Searching the approved rulebook..."):
                st.session_state["ask_result"] = asyncio.run(
                    compliance_rag_agent.ask(question.strip(), hs_code.strip() or None)
                )
        except Exception as exc:  # show any failure to the operator
            st.error(f"Question failed: {type(exc).__name__}: {exc}")
    finding: ComplianceFinding | None = st.session_state.get("ask_result")
    if finding is None:
        return
    st.markdown(f"**Q:** {finding.question}")
    if finding.found:
        st.success(finding.answer)
    else:
        st.info(finding.answer)
    if finding.method == "template" and finding.fallback_reason:
        st.caption(f"Quoted the rule instead of an LLM answer: {finding.fallback_reason}")
    if finding.citations:
        st.dataframe(
            [
                {"Rule": c.rule_key, "Version": c.version,
                 "Effective": c.effective_date.isoformat(), "Relevance": round(c.score, 3)}
                for c in finding.citations
            ],
            hide_index=True,
            width="stretch",
        )  # fmt: skip


# --------------------------------------------------------------------------- page

st.title("Compliance Rulebook")
show_flash()
reviewer_name: str | None = st.sidebar.selectbox(
    "Reviewer (recorded as decided_by)", REVIEWERS, index=None, key="reviewer",
    placeholder="registered reviewers (REVIEWERS)",
)  # fmt: skip
if not REVIEWERS:
    st.sidebar.warning("No reviewers configured — set REVIEWERS in .env.")
proposer = st.sidebar.text_input(
    "Proposer (recorded in the audit trail)", "compliance-dashboard", key="actor"
)
with session_scope() as db:
    pending_rules = svc.list_pending(db)
    review_contexts = [svc.review_context(db, rule.id) for rule in pending_rules]
    approved_rules = svc.list_approved(db)
    all_rules = svc.list_rules(db)

pending_tab, rulebook_tab, history_tab, propose_tab, ask_tab = st.tabs(
    [f"Pending review ({len(pending_rules)})", "Rulebook in force", "All versions", "Propose",
     "Ask the rulebook"]
)  # fmt: skip
with ask_tab:
    render_ask()
with pending_tab:
    render_pending(review_contexts, reviewer_name)
with rulebook_tab:
    render_rulebook(approved_rules)
    render_revoke(approved_rules, reviewer_name)
with history_tab:
    render_history(all_rules)
with propose_tab:
    render_propose(approved_rules, proposer.strip() or "compliance-dashboard")
