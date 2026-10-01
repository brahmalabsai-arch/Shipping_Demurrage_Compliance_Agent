"""Compliance rulebook: versioned rules, proposals and the human approval decision.

The single code path to rule data for the Compliance dashboard, ``compliance_rulebook_mcp``
and the agents. Governance guarantees (problem statement FR6/FR7):

* ``propose_change`` always stores a new version as ``pending`` — it has no status parameter.
* ``decide`` is the **only** function that decides a proposal (a test scans the codebase for
  any other status assignment), and it requires a named human reviewer — and, to reject, a
  reason (``decision_note``). A decision is final.
* ``revoke_decision`` (Phase 11) withdraws the approval of the version in force as a separate,
  audited act: ``status`` becomes ``revoked`` with its own who/when/why columns, the original
  decision fields are never rewritten, and the version it had superseded is back in force.
* Approving version N supersedes the currently approved version of that ``rule_key``; pending
  proposals of the same key are never touched by another proposal's decision, and an older
  proposal can no longer be approved once a newer version is (``StaleProposalError``).

An approval (or a revocation) refreshes the RAG index only after its transaction commits — the
index always holds exactly the versions in force (never a pending, rejected or revoked one);
``search_rules`` retrieves from that index (Phase 9).

A trigger (migrations ``0004``/``0006``) enforces the same lifecycle in Postgres. Rule versions
are never edited or deleted: a change is a new version, and history is the chain of
``superseded_by``.

Reviewers always judge a proposal against the **rule in force** (the approved version), not
against whatever the bulletin said at the previous scan: ``review_context`` gives that version
plus the history in between (other proposals — still pending, or rejected and why), and
``bulletin_drift`` lists every rule where the latest scanned bulletin disagrees with the rule in
force.
"""

from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import event, select
from sqlalchemy.orm import Session, SessionTransaction

from models.db_models import ChangeLogEntry, ComplianceRule, utcnow
from models.enums import RuleDecision, RuleStatus
from models.schemas import (
    BulletinSection,
    ComplianceRuleRecord,
    ReviewContext,
    RuleDrift,
    RuleMatch,
    RuleProposal,
)
from rag import ingest, retriever
from services import audit_service, bulletin_service
from services.db import session_scope
from services.errors import (
    ChangeLogEntryNotFoundError,
    DuplicateProposalError,
    InvalidDecisionError,
    RuleAlreadyDecidedError,
    RuleNotFoundError,
    RuleNotInForceError,
    StaleProposalError,
)

BUSINESS_TZ = ZoneInfo("Asia/Kolkata")  # dates in review history are the reviewer's (IST) days

logger = structlog.get_logger("services")

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


def search_rules(
    session: Session, query: str, hs_code: str | None = None, limit: int = 5
) -> list[RuleMatch]:
    """Approved rules in force that answer ``query`` (optionally only those covering
    ``hs_code``), most relevant first — possibly none.

    Hybrid retrieval (``rag.retriever``: BM25 + BGE dense → RRF → cross-encoder rerank) over
    the RAG index, which only ever holds approved versions in force. Every hit is re-read from
    Postgres and dropped unless it is still approved and in force, so a stale index entry can
    never surface a superseded, pending or rejected text. ``score`` is the reranker's.
    """
    matches = []
    for hit in retriever.search(query, hs_code, limit):
        try:
            rule = get_rule(session, hit.rule_id)
        except RuleNotFoundError:
            rule = None
        if rule is None or rule.status is not RuleStatus.APPROVED or rule.superseded_by:
            logger.warning("rag.stale_hit", rule_key=hit.rule_key, version=hit.version)
            continue
        matches.append(RuleMatch(rule=rule, score=hit.score, method="hybrid"))
    return matches


def sync_rag_index(session: Session) -> ingest.SyncReport:
    """Rebuild the RAG index as exactly the rulebook in force (backfill / repair)."""
    return ingest.sync_index(list_approved(session))


# The index is not transactional with Postgres: after an approval or a revocation commits, the
# entry of each touched rule_key is refreshed from a fresh read — the version now in force is
# (re-)embedded, or the entry is dropped if the rule has none.
_TO_INDEX = "compliance_rule_keys_to_index"
_HOOKED = "compliance_rag_hooks"


def _index_after_commit(session: Session, rule_key: str) -> None:
    session.info.setdefault(_TO_INDEX, []).append(rule_key)
    if session.info.get(_HOOKED):
        return
    session.info[_HOOKED] = True
    event.listen(session, "after_commit", _index_committed)
    event.listen(session, "after_soft_rollback", _forget_on_rollback)


def _forget_on_rollback(session: Session, previous_transaction: SessionTransaction) -> None:
    if not previous_transaction.nested:  # a failed savepoint inside decide's transaction is not
        session.info.pop(_TO_INDEX, None)  # a rollback of the approval itself


def _index_committed(session: Session) -> None:
    rule_keys = list(dict.fromkeys(session.info.pop(_TO_INDEX, [])))
    if not rule_keys:
        return
    try:
        with session_scope() as fresh:
            current = {key: _current_approved(fresh, key) for key in rule_keys}
            in_force = [ComplianceRuleRecord.model_validate(r) for r in current.values() if r]
        ingest.index_rules(in_force)
        ingest.remove_rules([key for key, row in current.items() if row is None])
    except Exception:  # never undo a committed decision; `scripts.index_rulebook` repairs
        logger.exception("rag.index_failed", rule_keys=rule_keys)


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
    session: Session,
    rule_id: UUID,
    decision: RuleDecision | str,
    reviewer: str,
    note: str | None = None,
) -> ComplianceRuleRecord:
    """Record a human reviewer's final decision on a pending rule — the only way to approve.

    Approve: the rule becomes the version in force and the previously approved version (if
    any) is marked ``superseded_by`` it; approving an older proposal after a newer version was
    approved raises ``StaleProposalError`` (reject it instead). Reject: permanent, and needs a
    ``note`` saying why. The note (optional for an approval) is stored as ``decision_note``.
    Deciding a rule that is not pending raises ``RuleAlreadyDecidedError``; a blank reviewer, an
    unknown decision or a rejection without a reason raises ``InvalidDecisionError``.
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
    note = (note or "").strip() or None
    if verdict is RuleDecision.REJECT and note is None:
        raise InvalidDecisionError("a rejection must give a reason")

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
    row.decision_note = note
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
            "note": note,
            "supersedes": None if superseded is None else superseded.id,
        },
    )
    session.flush()
    if record.status is RuleStatus.APPROVED:  # embedded after commit; a rejection never is
        _index_after_commit(session, record.rule_key)
    return record


def revoke_decision(
    session: Session, rule_id: UUID, reviewer: str, reason: str
) -> ComplianceRuleRecord:
    """Withdraw the approval of the version in force — a new, separate act, never an edit.

    The rule's ``status`` becomes ``revoked`` and ``revoked_by`` / ``revoked_at`` /
    ``revoke_note`` record who, when and why; ``decided_by`` / ``decided_at`` /
    ``decision_note`` keep the original approval. The version it had superseded (if any) is
    back in force; without one the rule has no version in force until a new proposal is
    approved. Audited as ``compliance_rule.revoked`` (+ ``compliance_rule.reinstated``); the RAG
    index follows after commit. A revocation is final.

    Raises ``InvalidDecisionError`` without a reviewer or a reason, ``RuleNotFoundError`` for an
    unknown id and ``RuleNotInForceError`` unless the rule is the approved version in force.
    """
    reviewer = reviewer.strip()
    reason = (reason or "").strip()
    if not reviewer:
        raise InvalidDecisionError("a revocation must name the human reviewer")
    if not reason:
        raise InvalidDecisionError("a revocation must give a reason")
    row = session.get(ComplianceRule, rule_id, with_for_update=True)
    if row is None:
        raise RuleNotFoundError(f"no compliance rule with id {rule_id}")
    if row.status is not RuleStatus.APPROVED or row.superseded_by is not None:
        state = "superseded" if row.superseded_by is not None else row.status.value
        raise RuleNotInForceError(
            f"{row.rule_key} v{row.version} is {state}; only the version in force can be revoked"
        )
    predecessor = session.scalars(
        select(ComplianceRule).where(ComplianceRule.superseded_by == row.id).with_for_update()
    ).one_or_none()

    row.status = RuleStatus.REVOKED
    row.revoked_by = reviewer
    row.revoked_at = utcnow()
    row.revoke_note = reason
    session.flush()  # the successor must be revoked before its predecessor is reinstated
    if predecessor is not None:
        predecessor.superseded_by = None
        session.flush()
    record = ComplianceRuleRecord.model_validate(row)

    audit_service.record(
        session,
        actor=reviewer,
        action="compliance_rule.revoked",
        entity_type="compliance_rule",
        entity_id=row.id,
        payload={
            "rule_key": row.rule_key,
            "version": row.version,
            "note": reason,
            "approved_by": row.decided_by,
            "approved_at": row.decided_at,
            "reinstated": None if predecessor is None else predecessor.id,
        },
    )
    if predecessor is not None:
        audit_service.record(
            session,
            actor=reviewer,
            action="compliance_rule.reinstated",
            entity_type="compliance_rule",
            entity_id=predecessor.id,
            payload={"rule_key": row.rule_key, "version": predecessor.version, "after": row.id},
        )
    session.flush()
    _index_after_commit(session, record.rule_key)
    return record


# --------------------------------------------------------------------------- review context


def _ist_day(rule: ComplianceRuleRecord, when: str) -> str:
    moment = rule.decided_at if when == "decided" else rule.created_at
    return "" if moment is None else moment.astimezone(BUSINESS_TZ).strftime("%d %b %Y")


def _history_line(other: ComplianceRuleRecord, proposal: ComplianceRuleRecord) -> str | None:
    if other.status is RuleStatus.REVOKED:
        revoked_on = (
            "" if other.revoked_at is None
            else other.revoked_at.astimezone(BUSINESS_TZ).strftime("%d %b %Y")
        )  # fmt: skip
        return (
            f"v{other.version} was approved by {other.decided_by} on "
            f"{_ist_day(other, 'decided')}, then revoked by {other.revoked_by} on {revoked_on}: "
            f'"{other.revoke_note}"'
        )
    if other.status is RuleStatus.REJECTED:
        return (
            f"v{other.version} (proposed {_ist_day(other, 'created')}) was rejected by "
            f"{other.decided_by} on {_ist_day(other, 'decided')}: \"{other.decision_note or '-'}\""
        )
    if other.status is RuleStatus.PENDING and other.version < proposal.version:
        return (
            f"v{other.version} (proposed {_ist_day(other, 'created')}) is still pending and is "
            f"superseded by this proposal — reject v{other.version} as superseded."
        )
    if other.status is RuleStatus.PENDING:
        return (
            f"A newer proposal, v{other.version} (proposed {_ist_day(other, 'created')}), is "
            "pending for this rule — this one is probably superseded."
        )
    return None


def review_context(session: Session, rule_id: UUID) -> ReviewContext:
    """What a reviewer needs to judge ``rule_id``: the rule in force it would replace, and every
    other version proposed since that one (pending, or rejected with its reason), as records
    and as plain-language history lines. Shared by the Compliance page and the review email."""
    proposal = get_rule(session, rule_id)
    versions = list_rules(session, rule_key=proposal.rule_key)
    in_force = next(
        (v for v in versions if v.status is RuleStatus.APPROVED and v.superseded_by is None), None
    )
    since = in_force.version if in_force is not None else 0
    others = [
        v
        for v in versions
        if v.version > since and v.id != proposal.id and v.status is not RuleStatus.APPROVED
    ]
    history = [line for v in others if (line := _history_line(v, proposal)) is not None]
    return ReviewContext(proposal=proposal, in_force=in_force, others=others, history=history)


def _same(rule: ComplianceRuleRecord, section: BulletinSection) -> bool:
    """Same rule in substance: text equal ignoring whitespace/case, same date and HS codes."""
    return (
        bulletin_service.normalize_text(rule.content)
        == bulletin_service.normalize_text(section.text)
        and rule.effective_date == section.effective_date
        and tuple(sorted(rule.hs_codes)) == tuple(sorted(section.hs_codes))
    )


def bulletin_drift(session: Session) -> list[RuleDrift]:
    """Rules where the latest *scanned* bulletin disagrees with the approved rule in force.

    Compares the scan's view (not the live publication — Option A) with the rulebook, so a
    change is caught even if it happened before the first scan. Each drift says whether that
    exact text is already pending or was turned down before — rejected, or approved and later
    revoked (``rejected_same``; neither is ever proposed again). Raises
    ``BulletinSnapshotNotFoundError`` before the first scan.
    """
    sections = bulletin_service.get_latest_sections(session)
    versions = list_rules(session)
    drifts = []
    for section in sections:
        mine = [v for v in versions if v.rule_key == section.rule_key]
        in_force = next(
            (v for v in mine if v.status is RuleStatus.APPROVED and v.superseded_by is None), None
        )
        if in_force is not None and _same(in_force, section):
            continue
        latest_first = sorted(mine, key=lambda v: -v.version)
        drifts.append(
            RuleDrift(
                rule_key=section.rule_key,
                section=section,
                in_force=in_force,
                pending_same=next(
                    (v for v in latest_first
                     if v.status is RuleStatus.PENDING and _same(v, section)), None
                ),
                rejected_same=next(
                    (v for v in latest_first
                     if v.status in (RuleStatus.REJECTED, RuleStatus.REVOKED)
                     and _same(v, section)), None
                ),
            )
        )  # fmt: skip
    return drifts
