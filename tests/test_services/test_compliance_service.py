"""Phase 4 — compliance_service: proposals are always pending, ``decide`` is the only path to a
status change, version chaining, and the Phase 4 edge cases."""

import ast
import re
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from config.settings import PROJECT_ROOT
from models.db_models import AuditEvent, ComplianceRule
from models.enums import RuleDecision, RuleStatus
from models.schemas import ComplianceRuleRecord, RuleProposal
from services import bulletin_service
from services import compliance_service as svc
from services.errors import (
    ChangeLogEntryNotFoundError,
    DuplicateProposalError,
    InvalidDecisionError,
    RuleAlreadyDecidedError,
    RuleNotFoundError,
    RuleNotInForceError,
    StaleProposalError,
)
from tests.fixtures.compliance import BASELINE_BULLETIN, edit_section

AGENT = "regulatory_watch_agent"
REVIEWER = "a.sharma (compliance)"
KEY = "HSN-8708-duty"
APPROVE, REJECT = RuleDecision.APPROVE, RuleDecision.REJECT


def _propose(session: Session, content: str, **overrides: Any) -> ComplianceRuleRecord:
    fields: dict[str, Any] = {
        "rule_key": KEY,
        "content": content,
        "hs_codes": ["8708"],
        "effective_date": date(2026, 4, 1),
    }
    fields.update(overrides)
    return svc.propose_change(session, RuleProposal.model_validate(fields), AGENT)


def _approved_v1(session: Session) -> ComplianceRuleRecord:
    rule = _propose(session, "BCD 15%.")
    return svc.decide(session, rule.id, APPROVE, REVIEWER)


def _actions(session: Session) -> list[str]:
    query = select(AuditEvent.action).where(AuditEvent.entity_type == "compliance_rule")
    return list(session.scalars(query.order_by(AuditEvent.created_at)))


# --------------------------------------------------------------------------- proposals


def test_proposal_is_always_pending_and_audited(db_session: Session) -> None:
    rule = _propose(db_session, "BCD 15%.")

    assert (rule.status, rule.version, rule.decided_by, rule.decided_at) == (
        RuleStatus.PENDING,
        1,
        None,
        None,
    )
    assert svc.list_pending(db_session) == [rule]
    assert svc.list_approved(db_session) == []
    assert _actions(db_session) == ["compliance_rule.proposed"]


def test_a_proposal_cannot_even_ask_for_a_status() -> None:
    """propose_change has no status input at all: the model forbids the field."""
    with pytest.raises(ValidationError, match="status"):
        RuleProposal.model_validate(
            {
                "rule_key": KEY,
                "content": "x",
                "effective_date": date(2026, 4, 1),
                "status": "approved",
            }
        )


def test_versions_increment_per_rule_key(db_session: Session) -> None:
    _approved_v1(db_session)

    v2 = _propose(db_session, "BCD 20%.")
    other = _propose(db_session, "Certificate needed.", rule_key="HSN-870830-brake-certification")

    assert (v2.version, other.version) == (2, 1)


def test_duplicate_of_approved_or_pending_version_raises(db_session: Session) -> None:
    _approved_v1(db_session)
    with pytest.raises(DuplicateProposalError, match="approved"):
        _propose(db_session, "BCD 15%.")

    _propose(db_session, "BCD 20%.")
    with pytest.raises(DuplicateProposalError, match="pending"):
        _propose(db_session, "BCD 20%.")

    # a different effective date is a genuine new proposal
    _propose(db_session, "BCD 20%.", effective_date=date(2026, 7, 1))


def test_unknown_source_diff_raises(db_session: Session) -> None:
    with pytest.raises(ChangeLogEntryNotFoundError):
        _propose(db_session, "BCD 15%.", source_diff_id=uuid4())


# --------------------------------------------------------------------------- decisions


def test_approval_supersedes_the_previous_version(db_session: Session) -> None:
    v1 = _approved_v1(db_session)
    v2 = _propose(db_session, "BCD 20%.")

    approved = svc.decide(db_session, v2.id, APPROVE, REVIEWER)

    assert (approved.status, approved.decided_by) == (RuleStatus.APPROVED, REVIEWER)
    assert approved.decided_at is not None
    assert svc.get_rule(db_session, v1.id).superseded_by == v2.id
    assert svc.list_approved(db_session) == [approved]
    assert svc.get_current_rule(db_session, KEY) == approved
    assert _actions(db_session)[-2:] == ["compliance_rule.superseded", "compliance_rule.approved"]


def test_rejection_is_permanent(db_session: Session) -> None:
    v1 = _approved_v1(db_session)
    v2 = _propose(db_session, "BCD 20%.")

    rejected = svc.decide(db_session, v2.id, REJECT, REVIEWER, "not yet notified")

    assert rejected.status is RuleStatus.REJECTED
    assert svc.get_current_rule(db_session, KEY).id == v1.id
    for decision in (APPROVE, REJECT):
        with pytest.raises(RuleAlreadyDecidedError):
            svc.decide(db_session, v2.id, decision, "another reviewer", "second opinion")
    assert svc.get_rule(db_session, v2.id).status is RuleStatus.REJECTED


def test_approved_rule_cannot_be_decided_again(db_session: Session) -> None:
    v1 = _approved_v1(db_session)

    with pytest.raises(RuleAlreadyDecidedError):
        svc.decide(db_session, v1.id, REJECT, REVIEWER, "not yet notified")


@pytest.mark.parametrize(("decision", "reviewer"), [("approve", "   "), ("publish", REVIEWER)])
def test_invalid_decisions_raise(db_session: Session, decision: str, reviewer: str) -> None:
    rule = _propose(db_session, "BCD 15%.")

    with pytest.raises(InvalidDecisionError):
        svc.decide(db_session, rule.id, decision, reviewer)
    assert svc.get_rule(db_session, rule.id).status is RuleStatus.PENDING


def test_decision_accepts_the_plain_string_form(db_session: Session) -> None:
    rule = _propose(db_session, "BCD 15%.")

    assert svc.decide(db_session, rule.id, "approve", REVIEWER).status is RuleStatus.APPROVED


def test_unknown_rule_raises(db_session: Session) -> None:
    with pytest.raises(RuleNotFoundError):
        svc.decide(db_session, uuid4(), APPROVE, REVIEWER)
    with pytest.raises(RuleNotFoundError):
        svc.get_rule(db_session, uuid4())
    with pytest.raises(RuleNotFoundError):
        svc.get_current_rule(db_session, KEY)


def test_older_proposal_approved_while_a_newer_one_is_pending(db_session: Session) -> None:
    """Edge case: approving the older proposal leaves the newer pending one untouched; both
    stay independently queryable, and the superseded_by chain is right once both are decided."""
    v1 = _approved_v1(db_session)
    v2 = _propose(db_session, "BCD 20%.")
    v3 = _propose(db_session, "BCD 25%.")

    svc.decide(db_session, v2.id, APPROVE, REVIEWER)

    assert svc.get_rule(db_session, v3.id).status is RuleStatus.PENDING
    assert svc.get_rule(db_session, v3.id).superseded_by is None
    assert svc.get_current_rule(db_session, KEY).id == v2.id

    svc.decide(db_session, v3.id, APPROVE, REVIEWER)

    chain = {r.version: r.superseded_by for r in svc.list_rules(db_session, rule_key=KEY)}
    assert chain == {1: v2.id, 2: v3.id, 3: None}
    assert [r.id for r in svc.list_rules(db_session, rule_key=KEY)] == [v1.id, v2.id, v3.id]


def test_older_proposal_cannot_be_approved_after_a_newer_one(db_session: Session) -> None:
    _approved_v1(db_session)
    v2 = _propose(db_session, "BCD 20%.")
    v3 = _propose(db_session, "BCD 25%.")
    svc.decide(db_session, v3.id, APPROVE, REVIEWER)

    with pytest.raises(StaleProposalError, match="reject it instead"):
        svc.decide(db_session, v2.id, APPROVE, REVIEWER)

    assert (
        svc.decide(db_session, v2.id, REJECT, REVIEWER, "not yet notified").status
        is RuleStatus.REJECTED
    )
    assert svc.get_current_rule(db_session, KEY).id == v3.id


# --------------------------------------------------------------------------- reads


def test_list_approved_filters_by_hs_code(db_session: Session) -> None:
    for key, codes in [
        (KEY, ["8708"]),
        ("BRAKE", ["870830"]),
        ("GENERAL", []),
        ("TYRES", ["4011"]),
    ]:
        rule = _propose(db_session, f"{key} text", rule_key=key, hs_codes=codes)
        svc.decide(db_session, rule.id, APPROVE, REVIEWER)
    _propose(db_session, "pending text", rule_key="PENDING-ONLY", hs_codes=["8708"])

    def keys(hs_code: str | None) -> list[str]:
        return [r.rule_key for r in svc.list_approved(db_session, hs_code)]

    assert keys(None) == ["BRAKE", "GENERAL", KEY, "TYRES"]
    assert keys("87083000") == ["BRAKE", "GENERAL", KEY]  # heading covers the sub-heading
    assert keys("87084000") == ["GENERAL", KEY]
    assert keys("8708") == ["BRAKE", "GENERAL", KEY]  # a sub-heading rule matters to 8708


# --------------------------------------------------------------------------- bulletin edge cases


def _bulletin_change(db_session: Session, new_text: str) -> Any:
    bulletin_service.publish(db_session, BASELINE_BULLETIN, "editor")
    bulletin_service.capture_snapshot(db_session, "scan")
    bulletin_service.diff_since_last(db_session, "scan")
    bulletin_service.publish(db_session, new_text, "editor")
    bulletin_service.capture_snapshot(db_session, "scan")
    return bulletin_service.diff_since_last(db_session, "scan")


def _propose_from(db_session: Session, entry_id: Any) -> list[ComplianceRuleRecord]:
    """What the watch agent does: one proposal per substantively changed rule section."""
    proposals = []
    for change in bulletin_service.section_changes(db_session, entry_id):
        if change.is_substantive and change.after is not None:
            proposals.append(
                svc.propose_change(
                    db_session,
                    RuleProposal(
                        rule_key=change.rule_key,
                        content=change.after.text,
                        hs_codes=list(change.after.hs_codes),
                        effective_date=change.after.effective_date,
                        change_summary=f"{change.rule_key} {change.kind.value} in bulletin",
                        source_diff_id=entry_id,
                    ),
                    AGENT,
                )
            )
    return proposals


def test_two_substantive_changes_in_one_snapshot_give_one_proposal_per_rule_key(
    db_session: Session,
) -> None:
    edited = edit_section(BASELINE_BULLETIN, KEY, "15% of the assessable", "20% of the assessable")
    edited = edit_section(edited, "ORG-KR-CEPA", "Korean authority", "Korean customs authority")
    entry = _bulletin_change(db_session, edited)

    proposals = _propose_from(db_session, entry.id)

    assert sorted(p.rule_key for p in proposals) == ["HSN-8708-duty", "ORG-KR-CEPA"]
    assert all(p.status is RuleStatus.PENDING and p.source_diff_id == entry.id for p in proposals)


def test_rejected_change_is_not_re_proposed_when_the_bulletin_is_unchanged(
    db_session: Session,
) -> None:
    """Edge case: after a rejection, re-scanning the same unchanged bulletin finds nothing to
    propose — detection is keyed on bulletin.change_log, not on current rule state."""
    edited = edit_section(BASELINE_BULLETIN, KEY, "15% of the assessable", "20% of the assessable")
    entry = _bulletin_change(db_session, edited)
    [proposal] = _propose_from(db_session, entry.id)
    svc.decide(db_session, proposal.id, REJECT, REVIEWER, "not yet notified")

    assert bulletin_service.diff_since_last(db_session, "scan") is None  # same pair
    bulletin_service.capture_snapshot(db_session, "scan")
    assert bulletin_service.diff_since_last(db_session, "scan") is None  # unchanged content

    assert svc.list_pending(db_session) == []
    assert [r.status for r in svc.list_rules(db_session, rule_key=KEY)] == [RuleStatus.REJECTED]


# --------------------------------------------------------------------------- governance


PROJECT_PACKAGES = (
    "config",
    "models",
    "services",
    "mcp_servers",
    "agents",
    "rag",
    "roi",
    "scheduler",
    "integrations",
    "dashboards",
    "scripts",
)
SERVICE = PROJECT_ROOT / "services" / "compliance_service.py"


def _project_modules() -> list[Path]:
    return sorted(p for pkg in PROJECT_PACKAGES for p in (PROJECT_ROOT / pkg).rglob("*.py"))


class _StatusWrites(ast.NodeVisitor):
    """Collects (function, line) of every status write in a module."""

    def __init__(self) -> None:
        self.stack: list[str] = ["<module>"]
        self.writes: list[tuple[str, int, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _targets(self, node: ast.Assign | ast.AugAssign | ast.AnnAssign) -> list[ast.expr]:
        return node.targets if isinstance(node, ast.Assign) else [node.target]

    def _assign(self, node: ast.Assign | ast.AugAssign | ast.AnnAssign) -> None:
        for target in self._targets(node):
            for sub in ast.walk(target):
                if isinstance(sub, ast.Attribute) and sub.attr == "status":
                    self.writes.append((self.stack[-1], node.lineno, "assignment"))
        self.generic_visit(node)

    visit_Assign = visit_AugAssign = visit_AnnAssign = _assign

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if (
            name == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "status"
        ):
            self.writes.append((self.stack[-1], node.lineno, "setattr"))
        for kw in node.keywords:
            if kw.arg == "status" and name in {"ComplianceRule", "values", "update"}:
                is_pending = ast.unparse(kw.value) == "RuleStatus.PENDING"
                if name != "ComplianceRule" or not is_pending:
                    self.writes.append((self.stack[-1], node.lineno, f"{name}(status=...)"))
        self.generic_visit(node)


def test_only_decide_changes_a_rule_status() -> None:
    """Exit criterion: in compliance_service, ``decide`` is the only function that decides
    (assigns ``status``) — plus, since Phase 11, ``revoke_decision`` (approved -> revoked only,
    trigger-enforced); creation (``ComplianceRule(...)``) may only ever say
    ``RuleStatus.PENDING``."""
    visitor = _StatusWrites()
    visitor.visit(ast.parse(SERVICE.read_text(encoding="utf-8")))

    assert visitor.writes, "the scan found no status write at all - is it still looking?"
    assert {function for function, _, _ in visitor.writes} == {"decide", "revoke_decision"}


def test_no_other_module_can_write_compliance_rules() -> None:
    """Nothing outside compliance_service imports the ORM model or writes the table in SQL, so
    no other code path can touch a rule's status."""
    allowed = {PROJECT_ROOT / "models" / "db_models.py", SERVICE}
    raw_write = re.compile(
        r"\b(update|insert\s+into|delete\s+from)\s+compliance\.rules\b", re.IGNORECASE
    )
    offenders = []
    for path in _project_modules():
        if path in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports_model = any(
            isinstance(node, ast.ImportFrom) and any(a.name == "ComplianceRule" for a in node.names)
            for node in ast.walk(tree)
        )
        if imports_model or raw_write.search(source):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == []


# --------------------------------------------------------------------------- database guards


def test_database_rejects_inserting_an_approved_rule(db_session: Session) -> None:
    with pytest.raises(IntegrityError, match="created pending"), db_session.begin_nested():
        db_session.execute(
            text(
                "INSERT INTO compliance.rules (rule_key, version, status, content, hs_codes,"
                " effective_date) VALUES ('X', 1, 'approved', 'x', '{}', '2026-01-01')"
            )
        )


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE compliance.rules SET content = 'edited' WHERE id = :id", "immutable"),
        ("UPDATE compliance.rules SET status = 'pending' WHERE id = :id", "already rejected"),
        ("UPDATE compliance.rules SET decided_by = 'someone' WHERE id = :id", "immutable"),
        ("DELETE FROM compliance.rules WHERE id = :id", "never deleted"),
    ],
)
def test_database_keeps_decided_rules_final(
    db_session: Session, statement: str, message: str
) -> None:
    rule = _propose(db_session, "BCD 15%.")
    svc.decide(db_session, rule.id, REJECT, REVIEWER, "not yet notified")

    with pytest.raises(IntegrityError, match=message), db_session.begin_nested():
        db_session.execute(text(statement), {"id": rule.id})


def test_database_requires_a_reviewer_for_a_status_change(db_session: Session) -> None:
    rule = _propose(db_session, "BCD 15%.")

    with pytest.raises(IntegrityError, match="who and when"), db_session.begin_nested():
        db_session.execute(
            text("UPDATE compliance.rules SET status = 'approved' WHERE id = :id"),
            {"id": rule.id},
        )


def test_database_allows_one_current_approved_version_per_rule(db_session: Session) -> None:
    _approved_v1(db_session)
    v2 = _propose(db_session, "BCD 20%.")

    with (
        pytest.raises(IntegrityError, match="uq_rules_current_approved"),
        db_session.begin_nested(),
    ):
        row = db_session.get_one(ComplianceRule, v2.id)
        row.status, row.decided_by = RuleStatus.APPROVED, "bypass"
        row.decided_at = row.created_at
        db_session.flush()


# --------------------------------------------------------------------------- search (Phase 9)


def test_search_retrieves_approved_rules_in_force_only(
    session_factory: sessionmaker[Session],
) -> None:
    """Hybrid retrieval (fake models here; tests/test_rag has the real-model golden set).
    Rules are indexed after their approval commits, so the setup commits."""
    with session_factory() as session, session.begin():
        for key, text_, codes in [
            ("ORG-KR-CEPA", "Korean certificate of origin for preferential duty.", ["8708"]),
            ("VAL-exchange-rate", "Convert value at the notified exchange rate.", []),
            ("TYRES", "Certificate of origin for tyres.", ["4011"]),
        ]:
            rule = _propose(session, text_, rule_key=key, hs_codes=codes)
            svc.decide(session, rule.id, APPROVE, REVIEWER)
        _propose(session, "Korean certificate of origin pending text", rule_key="PENDING-KR")

    with session_factory() as session:
        matches = svc.search_rules(session, "Korean certificate of origin", hs_code="87084000")
        everything = svc.search_rules(session, "certificate of origin")
        stopwords_only = svc.search_rules(session, "the of and")
        one = svc.search_rules(session, "certificate origin rate", limit=1)

    assert [m.rule.rule_key for m in matches] == ["ORG-KR-CEPA"]  # tyres filtered by HS code
    assert matches[0].method == "hybrid" and matches[0].rule.status is RuleStatus.APPROVED
    assert "PENDING-KR" not in {m.rule.rule_key for m in everything}  # never indexed
    assert stopwords_only == []
    assert len(one) == 1


# --------------------------------------------------------------------------- decision reasons


def test_a_rejection_needs_a_reason_and_keeps_it(db_session: Session) -> None:
    _approved_v1(db_session)
    v2 = _propose(db_session, "BCD 20%.")

    with pytest.raises(InvalidDecisionError, match="reason"):
        svc.decide(db_session, v2.id, REJECT, REVIEWER, "   ")
    rejected = svc.decide(db_session, v2.id, REJECT, REVIEWER, "  awaiting notification  ")

    assert rejected.decision_note == "awaiting notification"
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "compliance_rule.rejected")
    ).one()
    assert event.payload["note"] == "awaiting notification"


def test_an_approval_may_carry_a_note(db_session: Session) -> None:
    rule = _propose(db_session, "BCD 15%.")

    assert svc.decide(db_session, rule.id, APPROVE, REVIEWER).decision_note is None
    other = _propose(db_session, "x", rule_key="OTHER")
    assert svc.decide(db_session, other.id, APPROVE, REVIEWER, "checked").decision_note == "checked"


def test_database_requires_a_reason_to_reject_and_keeps_it_immutable(db_session: Session) -> None:
    rule = _propose(db_session, "BCD 15%.")
    reject_without_reason = (
        "UPDATE compliance.rules SET status = 'rejected', decided_by = 'x', decided_at = now()"
        " WHERE id = :id"
    )
    with pytest.raises(IntegrityError, match="needs a reason"), db_session.begin_nested():
        db_session.execute(text(reject_without_reason), {"id": rule.id})

    svc.decide(db_session, rule.id, REJECT, REVIEWER, "original reason")
    with pytest.raises(IntegrityError, match="immutable"), db_session.begin_nested():
        db_session.execute(
            text("UPDATE compliance.rules SET decision_note = 'rewritten' WHERE id = :id"),
            {"id": rule.id},
        )


# --------------------------------------------------------------------------- review context


def test_review_context_is_against_the_rule_in_force_with_history(db_session: Session) -> None:
    v1 = _approved_v1(db_session)  # 15% in force
    v2 = _propose(db_session, "BCD 20%.")
    svc.decide(db_session, v2.id, REJECT, "a.sharma", "awaiting official notification")
    v3 = _propose(db_session, "BCD 22%.")  # still pending, older than v4
    v4 = _propose(db_session, "BCD 25%.")

    context = svc.review_context(db_session, v4.id)

    assert context.in_force is not None and context.in_force.id == v1.id
    assert [o.version for o in context.others] == [2, 3]
    rejected, pending = context.history
    assert rejected.startswith("v2 (proposed ") and "was rejected by a.sharma on " in rejected
    assert rejected.endswith(': "awaiting official notification"')
    assert pending.startswith("v3 (proposed ") and "superseded by this proposal" in pending
    # seen from the older pending proposal, the newer one is flagged
    older = svc.review_context(db_session, v3.id)
    assert any(line.startswith("A newer proposal, v4") for line in older.history)


def test_review_context_of_a_brand_new_rule(db_session: Session) -> None:
    rule = _propose(db_session, "Brand new rule.", rule_key="NEW-RULE")

    context = svc.review_context(db_session, rule.id)

    assert (context.in_force, context.others, context.history) == (None, [], [])


# --------------------------------------------------------------------------- bulletin drift


def _scan_published(db_session: Session, bulletin: str) -> None:
    bulletin_service.publish(db_session, bulletin, "editor")
    bulletin_service.capture_snapshot(db_session, "scan")


def test_bulletin_drift_compares_the_latest_scan_with_the_rulebook(db_session: Session) -> None:
    for section in bulletin_service.parse_sections(BASELINE_BULLETIN):
        rule = svc.propose_change(
            db_session,
            RuleProposal(rule_key=section.rule_key, content=section.text,
                         hs_codes=list(section.hs_codes), effective_date=section.effective_date),
            "seed",
        )  # fmt: skip
        svc.decide(db_session, rule.id, APPROVE, "seed")
    _scan_published(db_session, BASELINE_BULLETIN)
    assert svc.bulletin_drift(db_session) == []

    edited = edit_section(BASELINE_BULLETIN, KEY, "15% of the assessable", "20% of the assessable")
    edited = edit_section(edited, "VAL-exchange-rate", "Assessable value", "ASSESSABLE   value")
    edited = edit_section(edited, "ORG-KR-CEPA", "## ORG-KR-CEPA", "## ORG-JP-CEPA")
    _scan_published(db_session, edited)

    drifts = {d.rule_key: d for d in svc.bulletin_drift(db_session)}

    assert set(drifts) == {KEY, "ORG-JP-CEPA"}  # the formatting-only edit is not drift
    in_force = drifts[KEY].in_force
    assert in_force is not None and in_force.version == 1
    assert "20%" in drifts[KEY].section.text
    assert drifts["ORG-JP-CEPA"].in_force is None
    assert drifts[KEY].pending_same is None and drifts[KEY].rejected_same is None


def test_bulletin_drift_knows_pending_and_rejected_versions_of_the_same_text(
    db_session: Session,
) -> None:
    _approved_v1(db_session)
    bulletin = (
        "Preamble.\n\n## HSN-8708-duty\nEffective: 2026-04-01\nHS codes: 8708\nBCD 20%.\n\n"
        "## OTHER\nEffective: 2026-04-01\nHS codes: all\nOther rule.\n"
    )
    _scan_published(db_session, bulletin)
    pending = _propose(db_session, "BCD 20%.")
    other = _propose(db_session, "Other rule.", rule_key="OTHER", hs_codes=[])
    svc.decide(db_session, other.id, REJECT, REVIEWER, "not applicable to us")

    drifts = {d.rule_key: d for d in svc.bulletin_drift(db_session)}

    same_pending, same_rejected = drifts[KEY].pending_same, drifts["OTHER"].rejected_same
    assert same_pending is not None and same_pending.id == pending.id
    assert same_rejected is not None and same_rejected.decision_note == "not applicable to us"


# --------------------------------------------------------------------------- revocation (Phase 11)


def _v2_approved_over_v1(session: Session) -> tuple[ComplianceRuleRecord, ComplianceRuleRecord]:
    v1 = _approved_v1(session)
    v2 = svc.decide(session, _propose(session, "BCD 20%.").id, APPROVE, REVIEWER, "notified")
    return v1, v2


def test_revoking_keeps_the_approval_and_reinstates_the_previous_version(
    db_session: Session,
) -> None:
    v1, v2 = _v2_approved_over_v1(db_session)

    revoked = svc.revoke_decision(db_session, v2.id, "b.rao (compliance)", "notice withdrawn")

    assert revoked.status is RuleStatus.REVOKED
    assert (revoked.decided_by, revoked.decided_at, revoked.decision_note) == (
        v2.decided_by, v2.decided_at, "notified"
    )  # fmt: skip
    assert (revoked.revoked_by, revoked.revoke_note) == ("b.rao (compliance)", "notice withdrawn")
    assert revoked.revoked_at is not None
    assert svc.get_current_rule(db_session, KEY).id == v1.id
    assert svc.get_rule(db_session, v1.id).superseded_by is None
    assert [r.version for r in svc.list_approved(db_session)] == [1]
    assert _actions(db_session)[-2:] == ["compliance_rule.revoked", "compliance_rule.reinstated"]


def test_revoking_the_first_version_leaves_nothing_in_force(db_session: Session) -> None:
    v1 = _approved_v1(db_session)
    svc.revoke_decision(db_session, v1.id, REVIEWER, "rule withdrawn")

    with pytest.raises(RuleNotFoundError):
        svc.get_current_rule(db_session, KEY)
    assert _actions(db_session)[-1] == "compliance_rule.revoked"
    # a new proposal can be approved into the gap
    v2 = _propose(db_session, "BCD 18%.")
    assert svc.decide(db_session, v2.id, APPROVE, REVIEWER).superseded_by is None
    assert svc.get_current_rule(db_session, KEY).id == v2.id


@pytest.mark.parametrize(("reviewer", "reason"), [(" ", "why"), ("a.sharma", " ")])
def test_a_revocation_names_who_and_why(db_session: Session, reviewer: str, reason: str) -> None:
    v1 = _approved_v1(db_session)
    with pytest.raises(InvalidDecisionError):
        svc.revoke_decision(db_session, v1.id, reviewer, reason)


def test_only_the_version_in_force_can_be_revoked(db_session: Session) -> None:
    v1, v2 = _v2_approved_over_v1(db_session)
    pending = _propose(db_session, "BCD 25%.")
    rejected = svc.decide(db_session, _propose(db_session, "BCD 30%.").id, REJECT, REVIEWER, "no")

    for rule in (v1, pending, rejected):  # superseded, pending, rejected
        with pytest.raises(RuleNotInForceError):
            svc.revoke_decision(db_session, rule.id, REVIEWER, "why")
    svc.revoke_decision(db_session, v2.id, REVIEWER, "why")
    with pytest.raises(RuleNotInForceError):  # final
        svc.revoke_decision(db_session, v2.id, REVIEWER, "again")
    with pytest.raises(RuleNotFoundError):
        svc.revoke_decision(db_session, uuid4(), REVIEWER, "why")


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        # revoking behind the service's back must still record who and why
        ("UPDATE compliance.rules SET status = 'revoked' WHERE id = :v2", "who, when and why"),
        # and must not rewrite the approval
        (
            "UPDATE compliance.rules SET status = 'revoked', revoked_by = 'x', revoked_at = now(),"
            " revoke_note = 'y', decided_by = 'someone else' WHERE id = :v2",
            "immutable",
        ),
        # a superseded version cannot be revoked
        (
            "UPDATE compliance.rules SET status = 'revoked', revoked_by = 'x', revoked_at = now(),"
            " revoke_note = 'y' WHERE id = :v1",
            "version in force",
        ),
        # nor reinstated while its successor is still approved
        ("UPDATE compliance.rules SET superseded_by = NULL WHERE id = :v1", "reinstated only"),
        # revocation fields are set only by revoking
        ("UPDATE compliance.rules SET revoke_note = 'x' WHERE id = :v2", "only by revoking"),
    ],
)
def test_database_guards_revocation(db_session: Session, statement: str, message: str) -> None:
    v1, v2 = _v2_approved_over_v1(db_session)
    with pytest.raises(IntegrityError, match=message), db_session.begin_nested():
        db_session.execute(text(statement), {"v1": v1.id, "v2": v2.id})


def test_a_revoked_version_is_final_in_the_database(db_session: Session) -> None:
    _, v2 = _v2_approved_over_v1(db_session)
    svc.revoke_decision(db_session, v2.id, REVIEWER, "why")
    with pytest.raises(IntegrityError, match="already revoked"), db_session.begin_nested():
        db_session.execute(
            text("UPDATE compliance.rules SET status = 'approved' WHERE id = :id"), {"id": v2.id}
        )


def test_review_history_and_drift_remember_a_revoked_text(db_session: Session) -> None:
    _, v2 = _v2_approved_over_v1(db_session)
    svc.revoke_decision(db_session, v2.id, "b.rao", "notice withdrawn")
    v3 = _propose(db_session, "BCD 22%.")
    bulletin = "Preamble.\n\n## HSN-8708-duty\nEffective: 2026-04-01\nHS codes: 8708\nBCD 20%.\n"
    _scan_published(db_session, bulletin)

    history = svc.review_context(db_session, v3.id).history
    [drift] = svc.bulletin_drift(db_session)

    assert any("v2 was approved by" in line and "revoked by b.rao" in line
               and "notice withdrawn" in line for line in history)  # fmt: skip
    assert drift.rejected_same is not None and drift.rejected_same.id == v2.id  # never re-raised
