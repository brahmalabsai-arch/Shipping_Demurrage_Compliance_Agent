"""Regulatory Watch agent — turns customs-bulletin changes into pending rule proposals and
reviewer emails (problem statement FR6; architecture §6-7; edge cases, Phase 6).

Runs after the scheduler has captured a snapshot (the only capture site — Option A). Through
MCP only (``customs_bulletin_mcp``, ``compliance_rulebook_mcp``, ``notifications_mcp``):

1. ``diff_since_last_scan`` — records whether the bulletin changed since the previous scan (the
   detection event, once per snapshot pair). A substantive change is broken down per rule with
   ``get_section_changes``: it links the resulting proposals to the change (``source_diff_id``)
   and flags rules *removed* from the bulletin (audited, not proposed — no repeal workflow yet).
2. ``list_bulletin_drift`` — what matters to a reviewer: every rule where the latest scanned
   bulletin disagrees with the **approved rule in force**. This also catches a change made
   before the first scan. For each drift, unless that exact text is already pending or was
   rejected before (never re-raised): a plain-language summary *against the rule in force*,
   then ``propose_change`` (always ``pending``) — one proposal per ``rule_key``.
3. ``send_review_email`` per proposal. The email (built server-side) compares with the rule in
   force and lists the history in between (other proposals: pending, or rejected and why). The
   proposal is already committed, so a failed email never loses it: the failure is recorded as
   a ``regulatory_watch.email_failed`` audit event and the run carries on.

The control flow is deterministic on purpose — idempotency, one proposal per rule and "never
approve" are guarantees, not judgement calls. The LLM (summary tier) only writes the summary,
and only if it adds no number absent from the two texts; otherwise, or without an API key, a
deterministic template summary is used. Nothing here can approve a rule.

State slice: reads ``WatchState.run_id``; writes every other ``WatchState`` field.
"""

import difflib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import TypeAdapter

from agents.audit import record_run_event
from agents.mcp_clients import McpToolbox
from agents.state import SectionSummary, WatchState
from models.enums import SectionChangeKind
from models.schemas import (
    BulletinSection,
    ChangeLogEntryRecord,
    ComplianceRuleRecord,
    EmailReceipt,
    RuleDrift,
    RuleProposal,
    SectionChange,
)
from services.errors import McpToolError

AGENT = "regulatory_watch_agent"
BULLETIN, RULEBOOK, NOTIFY = "customs_bulletin_mcp", "compliance_rulebook_mcp", "notifications_mcp"
SERVERS = (BULLETIN, RULEBOOK, NOTIFY)

logger = structlog.get_logger("agents")

SUMMARY_PROMPT = (
    "You explain customs rule changes to the import operations reviewer of an Indian auto-parts "
    "importer. You get the rule currently in force (if any) and the proposed new text. In 2-3 "
    "plain-English sentences say how the proposed rule differs from the rule in force and what "
    "that means for an importer. Use only facts stated in the texts you are given: never add a "
    "rate, date, code or any other number that is not in them. Quote every figure exactly as "
    "written and do not calculate anything — no differences, totals, net or effective rates "
    "(duties and surcharges compound, so such arithmetic is easily wrong). No preamble, no "
    "markdown."
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


@dataclass(frozen=True)
class WatchDeps:
    """What the node needs besides state: MCP access, the summary model, the reviewer."""

    toolbox: McpToolbox
    model: BaseChatModel | None
    reviewer_email: str | None


# --------------------------------------------------------------------------- summaries


def _numbers(text: str) -> set[Decimal]:
    values = set()
    for token in _NUMBER.findall(text):
        try:
            values.add(Decimal(token.replace(",", "")))
        except InvalidOperation:  # pragma: no cover - the regex only yields digit groups
            continue
    return values


def _source_text(change: SectionChange) -> str:
    parts = [change.rule_key]
    for section in (change.before, change.after):
        if section is not None:
            parts += [section.effective_date.isoformat(), *section.hs_codes, section.text]
    return " ".join(parts)


def _phrase_changes(before: str, after: str, limit: int = 4) -> list[str]:
    old, new = before.split(), after.split()
    phrases = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        was = " ".join(old[max(i1 - 2, 0) : i2 + 2]) or "(nothing)"
        now = " ".join(new[max(j1 - 2, 0) : j2 + 2]) or "(nothing)"
        phrases.append(f'"{was}" -> "{now}"')
    if len(phrases) > limit:
        phrases = [*phrases[:limit], f"and {len(phrases) - limit} more edit(s)"]
    return phrases


def template_summary(change: SectionChange) -> str:
    """Deterministic plain-language summary of one change (the LLM fallback). ``before`` is the
    rule in force, ``after`` the proposed text."""
    before, after = change.before, change.after
    if after is None:
        return f"Rule {change.rule_key} was removed from the customs bulletin."
    codes = ", ".join(after.hs_codes) or "all goods"
    if before is None:
        return (
            f"New rule {change.rule_key} (effective {after.effective_date}, HS codes {codes}): "
            f"{after.text[:400]}"
        )
    details = []
    if before.effective_date != after.effective_date:
        details.append(f"effective date {before.effective_date} -> {after.effective_date}")
    if before.hs_codes != after.hs_codes:
        details.append(f"HS codes {', '.join(before.hs_codes) or 'all'} -> {codes}")
    details += _phrase_changes(before.text, after.text)
    return f"Compared with the rule in force, {change.rule_key}: " + "; ".join(details) + "."


async def summarize(change: SectionChange, model: BaseChatModel | None) -> SectionSummary:
    """LLM summary when a model is configured and it invents no numbers; template otherwise.

    A summary quoting a number that is in neither text (typically a computed difference) gets
    one corrective retry naming the offending numbers; if the rewrite still has one, or the
    call fails or returns nothing, the deterministic template is used instead.
    """

    def fallback(reason: str) -> SectionSummary:
        return SectionSummary(rule_key=change.rule_key, text=template_summary(change),
                              method="template", fallback_reason=reason)  # fmt: skip

    if model is None:
        return fallback("no LLM configured")
    before, after = change.before, change.after
    prompt = "\n\n".join(
        [f"Rule: {change.rule_key}\nChange: {change.kind.value}"]
        + [
            f"{label} (effective {s.effective_date}, HS codes {', '.join(s.hs_codes) or 'all'}):"
            f"\n{s.text}"
            for label, s in (("RULE IN FORCE", before), ("PROPOSED", after))
            if s is not None
        ]
    )
    messages: list[BaseMessage] = [SystemMessage(SUMMARY_PROMPT), HumanMessage(prompt)]
    allowed = _numbers(_source_text(change))
    for attempt in (1, 2):
        try:
            reply = await model.ainvoke(messages)
        except Exception as exc:  # any provider failure (quota, network, timeout) -> template
            return fallback(f"LLM call failed: {type(exc).__name__}")
        text = " ".join((reply.content if isinstance(reply.content, str) else "").split())
        if not text:
            return fallback("LLM returned no text")
        invented = sorted(_numbers(text) - allowed)
        if not invented:
            return SectionSummary(rule_key=change.rule_key, text=text, method="llm")
        if attempt == 1:
            listed = ", ".join(str(n) for n in invented)
            messages += [
                reply,
                HumanMessage(
                    f"Your summary contains {listed}, which appear in neither text — probably a "
                    "calculation. Rewrite it quoting only figures exactly as written, with no "
                    "arithmetic."
                ),
            ]
    return fallback(f"LLM summary contained numbers not in the texts (after one retry): {invented}")


# --------------------------------------------------------------------------- the node


def _as_section(rule: ComplianceRuleRecord) -> BulletinSection:
    return BulletinSection(rule_key=rule.rule_key, effective_date=rule.effective_date,
                           hs_codes=tuple(sorted(rule.hs_codes)), text=rule.content)  # fmt: skip


def _against_rule_in_force(drift: RuleDrift) -> SectionChange:
    """The drift as a before/after pair: the rule in force (if any) → the bulletin's text."""
    return SectionChange(
        rule_key=drift.rule_key,
        kind=SectionChangeKind.ADDED if drift.in_force is None else SectionChangeKind.MODIFIED,
        before=None if drift.in_force is None else _as_section(drift.in_force),
        after=drift.section,
        is_substantive=True,
    )


def _skip_reason(drift: RuleDrift) -> str | None:
    if drift.pending_same is not None:
        return f"{drift.rule_key}: already pending as v{drift.pending_same.version}"
    if (rejected := drift.rejected_same) is not None:
        return (
            f"{drift.rule_key}: rejected before as v{rejected.version} by {rejected.decided_by}"
            f" ({rejected.decision_note or 'no reason recorded'})"
        )
    return None


def _email(
    proposal: ComplianceRuleRecord, summary: str, change: ChangeLogEntryRecord | None
) -> dict[str, Any]:
    if change is not None:
        origin = f"Detected in the latest bulletin scan:\n{change.diff_summary}"
    else:
        origin = (
            "Found by comparing the latest scanned bulletin with the approved rulebook (the "
            "bulletin already differed when it was scanned)."
        )
    return {
        "subject": f"[Customs Copilot] Review needed: {proposal.rule_key} v{proposal.version}",
        "diff_summary": f"{summary}\n\n{origin}",
        "rule_id": proposal.id,
    }


async def regulatory_watch_agent(state: WatchState, config: RunnableConfig) -> dict[str, Any]:
    """LangGraph node: detect → compare with the rulebook → propose (pending) → notify."""
    deps: WatchDeps = config["configurable"]["deps"]
    tools = deps.toolbox

    raw = await tools.call(BULLETIN, "diff_since_last_scan")
    change = None if raw is None else ChangeLogEntryRecord.model_validate(raw)
    changed: set[str] = set()
    removed: list[str] = []
    if change is not None and change.is_substantive:
        sections = TypeAdapter(list[SectionChange]).validate_python(
            await tools.call(BULLETIN, "get_section_changes", {"change_id": change.id})
        )
        changed = {s.rule_key for s in sections if s.is_substantive}
        removed = [s.rule_key for s in sections
                   if s.is_substantive and s.kind is SectionChangeKind.REMOVED]  # fmt: skip
        if removed:
            record_run_event(AGENT, state.run_id, "regulatory_watch.removed_sections_flagged",
                             {"change_id": change.id, "rule_keys": removed})  # fmt: skip

    drifts = TypeAdapter(list[RuleDrift]).validate_python(
        await tools.call(RULEBOOK, "list_bulletin_drift")
    )
    summaries: list[SectionSummary] = []
    proposals: list[ComplianceRuleRecord] = []
    linked: dict[str, ChangeLogEntryRecord | None] = {}
    by_rule: dict[str, SectionSummary] = {}
    skipped: list[str] = []
    for drift in drifts:
        if (reason := _skip_reason(drift)) is not None:
            skipped.append(reason)
            continue
        summary = await summarize(_against_rule_in_force(drift), deps.model)
        summaries.append(summary)
        source = change if change is not None and drift.rule_key in changed else None
        draft = RuleProposal(
            rule_key=drift.rule_key,
            content=drift.section.text,
            hs_codes=list(drift.section.hs_codes),
            effective_date=drift.section.effective_date,
            change_summary=summary.text,
            source_diff_id=None if source is None else source.id,
        )
        try:
            created = await tools.call(RULEBOOK, "propose_change", {"proposal": draft})
        except McpToolError as exc:
            if exc.error_type != "DuplicateProposalError":
                raise
            skipped.append(f"{drift.rule_key}: {exc.message}")
            continue
        record = ComplianceRuleRecord.model_validate(created)
        proposals.append(record)
        by_rule[record.rule_key] = summary
        linked[record.rule_key] = source

    emails: list[EmailReceipt] = []
    failures: list[str] = []
    for proposal in proposals:
        if not deps.reviewer_email:
            failure = "no reviewer email configured (REVIEWER_EMAIL)"
        else:
            message = _email(proposal, by_rule[proposal.rule_key].text, linked[proposal.rule_key])
            try:
                receipt = await tools.call(
                    NOTIFY, "send_review_email", {"to": deps.reviewer_email, **message}
                )
                emails.append(EmailReceipt.model_validate(receipt))
                continue
            except McpToolError as exc:
                failure = f"{exc.error_type}: {exc.message}"
        failures.append(f"{proposal.rule_key} v{proposal.version}: {failure}")
        record_run_event(AGENT, state.run_id, "regulatory_watch.email_failed",
                         {"rule_id": proposal.id, "rule_key": proposal.rule_key,
                          "error": failure})  # fmt: skip

    if proposals:
        outcome = "proposed"
    elif change is None:
        outcome = "no_change"
    elif not change.is_substantive:
        outcome = "formatting_only"
    else:
        outcome = "nothing_to_propose"
    return _finish(state, {
        "change": change, "summaries": summaries, "proposals": proposals, "skipped": skipped,
        "removed_sections": removed, "emails": emails, "email_failures": failures,
        "outcome": outcome,
    })  # fmt: skip


def _finish(state: WatchState, updates: dict[str, Any]) -> dict[str, Any]:
    final = state.model_copy(update=updates)
    record_run_event(AGENT, state.run_id, "regulatory_watch.completed", {
        "outcome": final.outcome,
        "change_id": final.change.id if final.change else None,
        "proposals": [f"{p.rule_key} v{p.version}" for p in final.proposals],
        "summary_methods": {s.rule_key: s.method for s in final.summaries},
        "summary_fallbacks": {
            s.rule_key: s.fallback_reason for s in final.summaries if s.method == "template"
        },
        "skipped": final.skipped,
        "removed_sections": final.removed_sections,
        "emails_sent": len(final.emails),
        "email_failures": final.email_failures,
    })  # fmt: skip
    logger.info("agent.run_completed", agent=AGENT, run_id=state.run_id, outcome=final.outcome)
    return updates


def build_graph() -> CompiledStateGraph[WatchState, Any, WatchState, WatchState]:
    """The regulatory watch pipeline: this agent alone (architecture §6)."""
    graph = StateGraph(WatchState)
    graph.add_node("regulatory_watch_agent", regulatory_watch_agent)
    graph.add_edge(START, "regulatory_watch_agent")
    graph.add_edge("regulatory_watch_agent", END)
    return graph.compile()


async def run(deps: WatchDeps, run_id: str) -> WatchState:
    """Run the pipeline once and return the final state."""
    result = await build_graph().ainvoke(
        WatchState(run_id=run_id), config={"configurable": {"deps": deps}}
    )
    return WatchState.model_validate(result)
