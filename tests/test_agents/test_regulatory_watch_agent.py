"""Phase 6 — the regulatory watch pipeline end to end: scheduler capture → agent over MCP
(in-memory servers, real protocol) → pending proposals, reviewer emails, audit trail.

Exit criteria and the Phase 6 edge cases (Option A, locked). Email goes to a recording sender;
the LLM is either absent or a fake — no test reaches OpenAI or Gmail.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agents import regulatory_watch_agent as agent
from agents.mcp_clients import memory_connector
from agents.state import WatchState
from integrations import email
from models.db_models import AuditEvent, BulletinSnapshot, ComplianceRule
from models.enums import RuleDecision, RuleStatus, SectionChangeKind
from models.schemas import BulletinSection, RuleProposal, SectionChange
from scheduler import watch_scheduler
from scripts import seed_compliance
from services import bulletin_service, compliance_service
from services.errors import EmailDeliveryError
from tests.fixtures.compliance import BASELINE_BULLETIN, edit_section

REVIEWER = "4x2kms@gmail.com"
KEY = "HSN-8708-duty"
DUTY_RAISED = edit_section(BASELINE_BULLETIN, KEY, "15% of the assessable", "20% of the assessable")


class RecordingSender:
    backend = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> str:
        self.sent.append((to, subject, body))
        return f"<msg-{len(self.sent)}@test>"


class FailingSender:
    backend = "gmail"

    def send(self, to: str, subject: str, body: str) -> str:
        raise EmailDeliveryError("Gmail API unreachable: simulated outage")


class ExplodingModel(FakeListChatModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("insufficient_quota")


@pytest.fixture
def sender(monkeypatch: pytest.MonkeyPatch) -> RecordingSender:
    recording = RecordingSender()
    monkeypatch.setattr(email, "get_email_sender", lambda: recording)
    return recording


@pytest.fixture
def watched(session_factory: sessionmaker[Session], sender: RecordingSender) -> Iterator[None]:
    """Seeded rulebook + published baseline bulletin, and one baseline scan already done."""
    seed_compliance.seed()
    with session_factory() as session, session.begin():
        bulletin_service.capture_snapshot(session, "baseline-scan")
    yield


async def scan(model: Any = None, reviewer: str | None = REVIEWER) -> WatchState | None:
    return await watch_scheduler.run_scan(
        connector=memory_connector, model_factory=lambda: model, reviewer_email=reviewer
    )


def publish(session_factory: sessionmaker[Session], text: str) -> None:
    with session_factory() as session, session.begin():
        bulletin_service.publish(session, text, "bulletin-editor")


def actions(session_factory: sessionmaker[Session]) -> list[str]:
    with session_factory() as session:
        return list(session.scalars(select(AuditEvent.action).order_by(AuditEvent.created_at)))


def rule_count(session_factory: sessionmaker[Session]) -> int | None:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(ComplianceRule))


# --------------------------------------------------------------------------- exit criteria


async def test_bulletin_edit_then_scan_proposes_emails_and_audits(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    """Exit criterion: edit → fire the job → a pending rule, an email to the reviewer, and
    audit rows for both the detection and the send."""
    publish(session_factory, DUTY_RAISED)

    state = await scan()

    assert state is not None and state.outcome == "proposed"
    [proposal] = state.proposals
    assert (proposal.rule_key, proposal.version, proposal.status) == (KEY, 2, RuleStatus.PENDING)
    assert state.change is not None and proposal.source_diff_id == state.change.id
    assert "20%" in proposal.content
    [(to, subject, body)] = sender.sent
    assert (to, subject) == (REVIEWER, f"[Customs Copilot] Review needed: {KEY} v2")
    assert proposal.change_summary and proposal.change_summary in body
    assert "Approve or reject it on the Compliance dashboard" in body

    trail = actions(session_factory)
    for action in ("bulletin_snapshot.captured", "bulletin_change.detected",
                   "compliance_rule.proposed", "notification.review_email_sent",
                   "bulletin_change.emailed", "regulatory_watch.completed"):  # fmt: skip
        assert action in trail, action
    with session_factory() as session:
        entry = bulletin_service.get_change(session, state.change.id)
        proposed_by = session.scalars(
            select(AuditEvent.actor).where(
                AuditEvent.action == "compliance_rule.proposed",
                AuditEvent.entity_id == proposal.id,
            )
        ).one()
    assert entry.emailed_at is not None
    assert proposed_by == agent.AGENT  # the agent's identity reached the audit trail via MCP


async def test_rescan_without_a_bulletin_change_creates_nothing(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    """Exit criterion: running the job again with no change → zero rows, zero emails."""
    publish(session_factory, DUTY_RAISED)
    await scan()
    rules_before, emails_before = rule_count(session_factory), len(sender.sent)

    state = await scan()

    assert state is not None and state.outcome == "no_change"
    assert (rule_count(session_factory), len(sender.sent)) == (rules_before, emails_before)


async def test_gmail_failure_still_commits_the_pending_rule_and_logs_it(
    watched: None, monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]
) -> None:
    """Exit criterion: the send fails → the pending row is committed anyway and the failure is
    an audit event (architecture §7)."""
    monkeypatch.setattr(email, "get_email_sender", FailingSender)
    publish(session_factory, DUTY_RAISED)

    state = await scan()

    assert state is not None and state.outcome == "proposed" and state.emails == []
    assert "simulated outage" in state.email_failures[0]
    with session_factory() as session:
        [pending] = compliance_service.list_pending(session)
        failure = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "regulatory_watch.email_failed")
        ).one()
        assert state.change is not None
        assert bulletin_service.get_change(session, state.change.id).emailed_at is None
    assert pending.rule_key == KEY
    assert failure.payload["rule_id"] == str(pending.id)
    assert "EmailDeliveryError" in failure.payload["error"]
    assert "notification.review_email_sent" not in actions(session_factory)


# --------------------------------------------------------------------------- edge cases


async def test_no_bulletin_change_at_all_means_no_rows_and_no_emails(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    rules_before = rule_count(session_factory)

    state = await scan()

    assert state is not None and state.outcome == "no_change"
    assert rule_count(session_factory) == rules_before and sender.sent == []


async def test_change_reverted_before_the_scan_is_never_seen(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    """Edge case (Option A, by design): changed then reverted between scans → nothing."""
    publish(session_factory, DUTY_RAISED)
    publish(session_factory, BASELINE_BULLETIN)
    rules_before = rule_count(session_factory)

    state = await scan()

    assert state is not None and state.outcome == "no_change"
    assert rule_count(session_factory) == rules_before and sender.sent == []


async def test_formatting_only_change_is_neither_proposed_nor_emailed(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    publish(
        session_factory,
        BASELINE_BULLETIN.replace("Parts and accessories", "PARTS  and accessories"),
    )
    rules_before = rule_count(session_factory)

    state = await scan()

    assert state is not None and state.outcome == "formatting_only"
    assert state.change is not None and not state.change.is_substantive
    assert rule_count(session_factory) == rules_before and sender.sent == []


async def test_pending_rule_is_never_auto_transitioned(
    watched: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: nobody decides → the rule stays pending through any number of scans."""
    publish(session_factory, DUTY_RAISED)
    first = await scan()
    for _ in range(3):
        await scan()

    assert first is not None
    with session_factory() as session:
        rule = compliance_service.get_rule(session, first.proposals[0].id)
    assert (rule.status, rule.decided_by, rule.decided_at) == (RuleStatus.PENDING, None, None)
    decisions = [a for a in actions(session_factory) if a in ("compliance_rule.approved",
                 "compliance_rule.rejected")]  # fmt: skip
    assert len(decisions) == 6  # only the six seed approvals, ever


async def test_rejected_change_is_not_re_proposed_on_rescan(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    """Edge case (agent level): reject, then rescan the unchanged bulletin → no duplicate."""
    publish(session_factory, DUTY_RAISED)
    first = await scan()
    assert first is not None
    with session_factory() as session, session.begin():
        compliance_service.decide(
            session, first.proposals[0].id, RuleDecision.REJECT, "a.sharma", "not notified yet"
        )

    again = await scan()

    assert again is not None and again.outcome == "no_change"
    with session_factory() as session:
        versions = compliance_service.list_rules(session, rule_key=KEY)
    assert [v.status for v in versions] == [RuleStatus.APPROVED, RuleStatus.REJECTED]
    assert len(sender.sent) == 1


async def test_two_changed_rules_give_two_proposals_and_two_emails(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    both = edit_section(DUTY_RAISED, "ORG-KR-CEPA", "Korean authority", "Korean customs authority")
    publish(session_factory, both)

    state = await scan()

    assert state is not None
    assert sorted(p.rule_key for p in state.proposals) == [KEY, "ORG-KR-CEPA"]
    assert len({p.source_diff_id for p in state.proposals}) == 1
    assert sorted(to for to, _, _ in sender.sent) == [REVIEWER, REVIEWER]


async def test_added_rule_is_proposed_and_removed_rule_is_flagged(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    renamed = edit_section(BASELINE_BULLETIN, "ORG-KR-CEPA", "## ORG-KR-CEPA", "## ORG-JP-CEPA")
    publish(session_factory, renamed)

    state = await scan()

    assert state is not None
    assert [(p.rule_key, p.version) for p in state.proposals] == [("ORG-JP-CEPA", 1)]
    assert state.removed_sections == ["ORG-KR-CEPA"]
    assert "regulatory_watch.removed_sections_flagged" in actions(session_factory)
    with session_factory() as session:  # the removed rule stays in force until a human acts
        assert compliance_service.get_current_rule(session, "ORG-KR-CEPA").version == 1


async def test_change_already_proposed_by_hand_is_skipped(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session, session.begin():
        section = next(s for s in bulletin_service.parse_sections(DUTY_RAISED) if s.rule_key == KEY)
        compliance_service.propose_change(
            session,
            RuleProposal(rule_key=KEY, content=section.text, hs_codes=list(section.hs_codes),
                         effective_date=section.effective_date),
            "compliance-dashboard",
        )  # fmt: skip
    publish(session_factory, DUTY_RAISED)

    state = await scan()

    assert state is not None and state.outcome == "nothing_to_propose"
    assert state.skipped == [f"{KEY}: already pending as v2"] and sender.sent == []


async def test_missing_reviewer_address_keeps_the_proposal_and_logs_why(
    watched: None, monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]
) -> None:
    monkeypatch.setattr(watch_scheduler, "get_settings", lambda: _no_reviewer_settings())
    publish(session_factory, DUTY_RAISED)

    state = await scan(reviewer=None)

    assert state is not None and state.proposals and state.emails == []
    assert "REVIEWER_EMAIL" in state.email_failures[0]
    assert "regulatory_watch.email_failed" in actions(session_factory)


def _no_reviewer_settings() -> Any:
    from config.settings import get_settings

    return get_settings().model_copy(update={"reviewer_email": None})


async def test_nothing_published_skips_the_scan(session_factory: sessionmaker[Session]) -> None:
    assert await scan() is None
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(BulletinSnapshot)) == 0


# --------------------------------------------------------------------------- summaries


async def test_llm_summary_is_used_when_it_sticks_to_the_facts(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    text = "Basic customs duty on heading 8708 parts rises from 15% to 20% of assessable value."
    publish(session_factory, DUTY_RAISED)

    state = await scan(model=FakeListChatModel(responses=[text]))

    assert state is not None
    assert (state.summaries[0].method, state.summaries[0].text) == ("llm", text)
    assert state.proposals[0].change_summary == text
    assert text in sender.sent[0][2]


@pytest.mark.parametrize(
    ("model", "reason"),
    [
        (
            FakeListChatModel(
                responses=[
                    "Duty rises to 25% from 1 November 2026.",
                    "Duty is now an additional 5% of value.",
                ]
            ),
            "numbers not in the texts (after one retry)",
        ),
        (ExplodingModel(responses=[""]), "LLM call failed"),
        (FakeListChatModel(responses=["   "]), "no text"),
    ],
)
async def test_llm_summary_falls_back_to_the_template(
    watched: None, session_factory: sessionmaker[Session], model: Any, reason: str
) -> None:
    """An invented figure, a provider failure (e.g. no credits) or an empty reply never
    reaches the reviewer: the deterministic summary is used instead, and the run goes on."""
    publish(session_factory, DUTY_RAISED)

    state = await scan(model=model)

    assert state is not None and state.outcome == "proposed"
    summary = state.summaries[0]
    assert summary.method == "template" and reason in (summary.fallback_reason or "")
    with session_factory() as session:  # the reason is on the audit trail, not just in memory
        completed = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "regulatory_watch.completed")
        ).all()[-1]
    assert completed.payload["summary_fallbacks"] == {KEY: summary.fallback_reason}
    assert (
        summary.text
        == 'Compared with the rule in force, HSN-8708-duty: "(BCD) of 15% of the" -> "(BCD) of 20% of the".'
    )


def _section(
    key: str, text: str, date_: str = "2026-04-01", codes: tuple[str, ...] = ("8708",)
) -> BulletinSection:
    return BulletinSection.model_validate(
        {"rule_key": key, "effective_date": date_, "hs_codes": codes, "text": text}
    )


def test_template_summary_describes_each_kind_of_change() -> None:
    before = _section("R-1", "Duty is 15% of value.")
    after = _section("R-1", "Duty is 20% of value.", date_="2026-10-01")

    modified = agent.template_summary(SectionChange(
        rule_key="R-1", kind=SectionChangeKind.MODIFIED, before=before, after=after, is_substantive=True))  # fmt: skip
    added = agent.template_summary(SectionChange(
        rule_key="R-1", kind=SectionChangeKind.ADDED, before=None, after=after, is_substantive=True))  # fmt: skip
    removed = agent.template_summary(SectionChange(
        rule_key="R-1", kind=SectionChangeKind.REMOVED, before=before, after=None, is_substantive=True))  # fmt: skip

    assert modified == ('Compared with the rule in force, R-1: effective date 2026-04-01 -> 2026-10-01; '
                        '"Duty is 15% of value." -> "Duty is 20% of value.".')  # fmt: skip
    assert added.startswith("New rule R-1 (effective 2026-10-01, HS codes 8708): Duty is 20%")
    assert removed == "Rule R-1 was removed from the customs bulletin."


# --------------------------------------------------------------------------- scheduler


async def test_scheduled_job_survives_and_audits_a_failed_scan(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]
) -> None:
    async def broken(**kwargs: Any) -> None:
        raise ConnectionError("MCP server did not start")

    monkeypatch.setattr(watch_scheduler, "run_scan", broken)

    await watch_scheduler.scheduled_scan()  # does not raise

    with session_factory() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "regulatory_watch.failed")
        ).one()
    assert "MCP server did not start" in event.payload["error"]


def test_scheduler_registers_one_non_overlapping_interval_job() -> None:
    scheduler = watch_scheduler.create_scheduler(interval_minutes=15)

    [job] = scheduler.get_jobs()
    assert job.id == watch_scheduler.JOB_ID
    assert job.trigger.interval.total_seconds() == 15 * 60
    assert (job.max_instances, job.coalesce) == (1, True)


# --------------------------------------------------------------------------- judged against the rule in force

DUTY_25 = edit_section(BASELINE_BULLETIN, KEY, "15% of the assessable", "25% of the assessable")


async def test_change_made_before_the_first_scan_is_still_proposed(
    session_factory: sessionmaker[Session], sender: RecordingSender
) -> None:
    """The bulletin says 20% before anything was ever scanned; the rulebook says 15%. The first
    scan has no previous snapshot to diff, but the rulebook check still raises the proposal."""
    seed_compliance.seed()
    publish(session_factory, DUTY_RAISED)

    state = await scan()

    assert state is not None and state.change is None and state.outcome == "proposed"
    [proposal] = state.proposals
    assert (proposal.rule_key, proposal.version, proposal.source_diff_id) == (KEY, 2, None)
    assert "15% of the" in state.summaries[0].text and "20% of the" in state.summaries[0].text
    [(_, _, body)] = sender.sent
    assert "Compared with the rule in force, v1" in body
    assert "Found by comparing the latest scanned bulletin with the approved rulebook" in body
    assert (await scan()) is not None and len(sender.sent) == 1  # not raised twice


async def test_newer_bulletin_text_is_summarised_against_the_rule_in_force(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    """15% in force, 20% proposed and still pending, then the bulletin says 25%: the new
    proposal is described as 15% -> 25%, and the email says v2 is superseded."""
    publish(session_factory, DUTY_RAISED)
    await scan()
    publish(session_factory, DUTY_25)

    state = await scan()

    assert state is not None
    [v3] = state.proposals
    assert v3.version == 3
    assert state.summaries[0].text == (
        'Compared with the rule in force, HSN-8708-duty: "(BCD) of 15% of the" -> "(BCD) of 25% of the".'
    )
    body = sender.sent[-1][2]
    assert "Compared with the rule in force, v1" in body
    assert "History since the rule in force:" in body
    assert "v2 (proposed " in body and "superseded by this proposal" in body
    assert "Text in force (v1):" in body and "15% of the assessable" in body


async def test_a_rejection_and_its_reason_reach_the_next_review(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    publish(session_factory, DUTY_RAISED)
    first = await scan()
    assert first is not None
    with session_factory() as session, session.begin():
        compliance_service.decide(session, first.proposals[0].id, RuleDecision.REJECT,
                                  "a.sharma", "awaiting official notification")  # fmt: skip
    publish(session_factory, DUTY_25)

    state = await scan()

    assert state is not None and [p.version for p in state.proposals] == [3]
    body = sender.sent[-1][2]
    assert "was rejected by a.sharma on " in body
    assert '"awaiting official notification"' in body
    assert "15% of the" in state.summaries[0].text  # still measured against v1, not v2


async def test_rejected_text_is_never_proposed_again_even_after_other_changes(
    watched: None, sender: RecordingSender, session_factory: sessionmaker[Session]
) -> None:
    publish(session_factory, DUTY_RAISED)
    first = await scan()
    assert first is not None
    with session_factory() as session, session.begin():
        compliance_service.decide(session, first.proposals[0].id, RuleDecision.REJECT,
                                  "a.sharma", "not notified")  # fmt: skip
    publish(session_factory, BASELINE_BULLETIN)  # back to the approved text...
    await scan()
    publish(session_factory, DUTY_RAISED)  # ...and the rejected text again

    state = await scan()

    assert state is not None and state.proposals == [] and state.outcome == "nothing_to_propose"
    assert state.skipped == [f"{KEY}: rejected before as v2 by a.sharma (not notified)"]
    assert len(sender.sent) == 1


def test_summary_prompt_forbids_new_numbers_and_arithmetic() -> None:
    """A model once wrote "an additional 5% of the assessable value" for 25% -> 30%: invented
    (the guard caught it) and wrong (surcharge and IGST compound). The prompt rules it out."""
    prompt = agent.SUMMARY_PROMPT
    assert "never add a rate, date, code or any other number" in prompt
    assert "Quote every figure exactly as written and do not calculate anything" in prompt


async def test_llm_summaries_leave_no_fallback_entry(
    watched: None, session_factory: sessionmaker[Session]
) -> None:
    publish(session_factory, DUTY_RAISED)

    await scan(model=FakeListChatModel(responses=["Basic customs duty rises from 15% to 20%."]))

    with session_factory() as session:
        completed = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "regulatory_watch.completed")
        ).all()[-1]
    assert completed.payload["summary_methods"] == {KEY: "llm"}
    assert completed.payload["summary_fallbacks"] == {}


async def test_one_corrective_retry_rescues_a_summary_with_a_computed_number(
    watched: None, session_factory: sessionmaker[Session]
) -> None:
    """First reply computes "5 percentage points" (in neither text); told so, the model
    rewrites without it and that rewrite is used."""
    publish(session_factory, DUTY_RAISED)
    model = FakeListChatModel(responses=[
        "Duty rises by 5 percentage points to 20%.",
        "Basic customs duty rises from 15% to 20% of the assessable value.",
    ])  # fmt: skip

    state = await scan(model=model)

    assert state is not None
    summary = state.summaries[0]
    assert (summary.method, summary.text) == (
        "llm", "Basic customs duty rises from 15% to 20% of the assessable value.")  # fmt: skip
