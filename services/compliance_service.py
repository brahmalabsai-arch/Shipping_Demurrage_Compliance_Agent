"""Compliance rulebook: versioned rules, proposals and the human approval decision.

The single code path to rule data for the Compliance dashboard, ``compliance_rulebook_mcp``
and the agents. Governance guarantees (problem statement FR6/FR7):

* ``propose_change`` always stores a new version as ``pending`` — it has no status parameter.
* ``decide`` is the **only** function that changes ``status`` (a test scans the codebase for
  any other assignment), and it requires a named human reviewer. A decision is final.
* Approving version N supersedes the currently approved version of that ``rule_key``; pending
  proposals of the same key are never touched by another proposal's decision, and an older
  proposal can no longer be approved once a newer version is (``StaleProposalError``).

A trigger (migration ``0004``) enforces the same lifecycle in Postgres. Rule versions are never
edited or deleted: a change is a new version, and history is the chain of ``superseded_by``.
"""

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.db_models import ChangeLogEntry, ComplianceRule, utcnow
from models.enums import RuleDecision, RuleStatus
from models.schemas import ComplianceRuleRecord, RuleMatch, RuleProposal
from services import audit_service
from services.errors import (
    ChangeLogEntryNotFoundError,
    DuplicateProposalError,
    InvalidDecisionError,
    RuleAlreadyDecidedError,
    RuleNotFoundError,
    StaleProposalError,
)

# --------------------------------------------------------------------------- reads


def get_rule(session: Session, rule_id: UUID) -> ComplianceRuleRecord:
    """One rule version, whatever its status."""
    row = session.get(ComplianceRule, rule_id)
    if row is None:
        raise RuleNotFoundError(f"no compliance rule with id {rule_id}")
    return ComplianceRuleRecord.model_validate(row)


def list_approved(session: Session, hs_code: str | None = None) -> list[ComplianceRuleRecord]:
    """The rulebook in force: the current approved version of each rule, by rule_key.

    With ``hs_code``, only rules that cover it (see ``ComplianceRuleRecord.applies_to``).
    Pending and rejected versions are never returned — agents must only ever see this.
    """
    rows = session.scalars(
        select(ComplianceRule)
        .where(
            ComplianceRule.status == RuleStatus.APPROVED,
            ComplianceRule.superseded_by.is_(None),
        )
        .order_by(ComplianceRule.rule_key)
    )
    records = [ComplianceRuleRecord.model_validate(row) for row in rows]
    return records if hs_code is None else [r for r in records if r.applies_to(hs_code)]


def list_pending(session: Session) -> list[ComplianceRuleRecord]:
    """Proposals awaiting a human decision, oldest first."""
    rows = session.scalars(
        select(ComplianceRule)
        .where(ComplianceRule.status == RuleStatus.PENDING)
        .order_by(ComplianceRule.created_at, ComplianceRule.rule_key, ComplianceRule.version)
    )
    return [ComplianceRuleRecord.model_validate(row) for row in rows]


def list_rules(
    session: Session, rule_key: str | None = None, status: RuleStatus | None = None
) -> list[ComplianceRuleRecord]:
    """Every version (optionally of one rule and/or in one status), by rule_key and version."""
    query = select(ComplianceRule).order_by(ComplianceRule.rule_key, ComplianceRule.version)
    if rule_key is not None:
        query = query.where(ComplianceRule.rule_key == rule_key)
    if status is not None:
        query = query.where(ComplianceRule.status == status)
    return [ComplianceRuleRecord.model_validate(row) for row in session.scalars(query)]


_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "which",
        "with",
    ]
)


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in _STOPWORDS}


def search_rules(
    session: Session, query: str, hs_code: str | None = None, limit: int = 5
) -> list[RuleMatch]:
    """Approved rules relevant to ``query`` (optionally only those covering ``hs_code``).

    Interim keyword retriever: score = share of the query's terms found in the rule key and
    text; rules with no overlap are dropped. Phase 9 replaces the scoring with the hybrid
    BM25 + dense + rerank retriever behind this same function. Only ever searches
    ``list_approved`` (``status='approved'`` in SQL) — pending or rejected text is never a
    candidate.
    """
    wanted = _terms(query)
    if not wanted:
        return []
    matches = []
    for rule in list_approved(session, hs_code):
        found = wanted & _terms(f"{rule.rule_key.replace('-', ' ')} {rule.content}")
        if found:
            matches.append(RuleMatch(rule=rule, score=len(found) / len(wanted), method="keyword"))
    matches.sort(key=lambda m: (-m.score, m.rule.rule_key))
    return matches[:limit]


def get_current_rule(session: Session, rule_key: str) -> ComplianceRuleRecord:
    """The approved version of ``rule_key`` currently in force."""
    row = _current_approved(session, rule_key)
    if row is None:
        raise RuleNotFoundError(f"no approved version of rule {rule_key}")
    return ComplianceRuleRecord.model_validate(row)


def _current_approved(
    session: Session, rule_key: str, *, lock: bool = False
) -> ComplianceRule | None:
    query = select(ComplianceRule).where(
        ComplianceRule.rule_key == rule_key,
        ComplianceRule.status == RuleStatus.APPROVED,
        ComplianceRule.superseded_by.is_(None),
    )
    return session.scalars(query.with_for_update() if lock else query).one_or_none()


# --------------------------------------------------------------------------- writes


def propose_change(session: Session, proposal: RuleProposal, actor: str) -> ComplianceRuleRecord:
    """Stage a new version of a rule for human review. The new row is always ``pending``.

    The version number is one above the highest existing version of the rule. A proposal
    identical (text, HS codes, effective date) to the approved version or to a pending one
    raises ``DuplicateProposalError``; an unknown ``source_diff_id`` raises
    ``ChangeLogEntryNotFoundError``.
    """
    versions = list(
        session.scalars(
            select(ComplianceRule)
            .where(ComplianceRule.rule_key == proposal.rule_key)
            .order_by(ComplianceRule.version)
            .with_for_update()  # serialise version numbering per rule
        )
    )
    same = (proposal.content, proposal.hs_codes, proposal.effective_date)
    for existing in versions:
        live = existing.status is RuleStatus.PENDING or (
            existing.status is RuleStatus.APPROVED and existing.superseded_by is None
        )
        if live and (existing.content, sorted(existing.hs_codes), existing.effective_date) == same:
            raise DuplicateProposalError(
                f"{proposal.rule_key} v{existing.version} ({existing.status.value}) already "
                "says exactly this"
            )
    if (
        proposal.source_diff_id is not None
        and session.get(ChangeLogEntry, proposal.source_diff_id) is None
    ):
        raise ChangeLogEntryNotFoundError(f"no bulletin change with id {proposal.source_diff_id}")

    row = ComplianceRule(
        rule_key=proposal.rule_key,
        version=versions[-1].version + 1 if versions else 1,
        status=RuleStatus.PENDING,
        content=proposal.content,
        hs_codes=proposal.hs_codes,
        effective_date=proposal.effective_date,
        change_summary=proposal.change_summary,
        source_diff_id=proposal.source_diff_id,
    )
    session.add(row)
    session.flush()
    record = ComplianceRuleRecord.model_validate(row)
    audit_service.record(
        session,
        actor=actor,
        action="compliance_rule.proposed",
        entity_type="compliance_rule",
        entity_id=row.id,
        payload={"rule": record.model_dump()},
    )
    session.flush()
    return record


def decide(
    session: Session, rule_id: UUID, decision: RuleDecision | str, reviewer: str
) -> ComplianceRuleRecord:
    """Record a human reviewer's final decision on a pending rule — the only way to approve.

    Approve: the rule becomes the version in force and the previously approved version (if
    any) is marked ``superseded_by`` it; approving an older proposal after a newer version was
    approved raises ``StaleProposalError`` (reject it instead). Reject: permanent. Deciding a
    rule that is not pending raises ``RuleAlreadyDecidedError``; a blank reviewer or unknown
    decision raises ``InvalidDecisionError``.
    """
    reviewer = reviewer.strip()
    if not reviewer:
        raise InvalidDecisionError("a decision must name the human reviewer")
    try:
        verdict = RuleDecision(decision)
    except ValueError as exc:
        raise InvalidDecisionError(
            f"decision must be 'approve' or 'reject', not {decision!r}"
        ) from exc

    row = session.get(ComplianceRule, rule_id, with_for_update=True)
    if row is None:
        raise RuleNotFoundError(f"no compliance rule with id {rule_id}")
    if row.status is not RuleStatus.PENDING:
        raise RuleAlreadyDecidedError(
            f"{row.rule_key} v{row.version} is already {row.status.value}; decisions are final"
        )

    superseded: ComplianceRule | None = None
    if verdict is RuleDecision.APPROVE:
        superseded = _current_approved(session, row.rule_key, lock=True)
        if superseded is not None and superseded.version > row.version:
            raise StaleProposalError(
                f"{row.rule_key} v{superseded.version} is already approved; v{row.version} is "
                "older — reject it instead"
            )
        if superseded is not None:
            superseded.superseded_by = row.id
            session.flush()  # retire the old version before the new one is approved (unique idx)

    row.status = RuleStatus.APPROVED if verdict is RuleDecision.APPROVE else RuleStatus.REJECTED
    row.decided_by = reviewer
    row.decided_at = utcnow()
    session.flush()
    record = ComplianceRuleRecord.model_validate(row)

    if superseded is not None:
        audit_service.record(
            session,
            actor=reviewer,
            action="compliance_rule.superseded",
            entity_type="compliance_rule",
            entity_id=superseded.id,
            payload={"rule_key": row.rule_key, "version": superseded.version, "by": row.id},
        )
    audit_service.record(
        session,
        actor=reviewer,
        action=f"compliance_rule.{record.status.value}",
        entity_type="compliance_rule",
        entity_id=row.id,
        payload={
            "rule_key": row.rule_key,
            "version": row.version,
            "decision": verdict.value,
            "supersedes": None if superseded is None else superseded.id,
        },
    )
    session.flush()
    # Phase 9 hooks in here: an approved rule is embedded into Chroma (never a pending one).
    return record
