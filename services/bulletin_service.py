"""The simulated external customs bulletin: publication, scan-time snapshots, change detection.

Two write paths, deliberately kept apart (edge cases, Phase 6, "Option A"):

* ``publish`` — the Customs Bulletin dashboard edits the published text. It never captures a
  snapshot, so the system only learns about an edit when the next scheduled scan looks.
* ``capture_snapshot`` + ``diff_since_last`` — called only by the scheduled scan. A change made
  and reverted between two scans is invisible, by design.

``diff_since_last`` compares the two most recent snapshots and logs at most one
``bulletin.change_log`` entry per snapshot pair (unique constraint), so calling it again without
a new capture returns ``None``.

Bulletin format — free text, then one section per rule::

    ## HSN-8708-duty
    Effective: 2026-04-01
    HS codes: 8708, 870830        (or "all")
    The rule text, one or more lines.

Sections let a change be attributed to the ``rule_key`` it affects, so the watch agent can
raise one proposal per changed rule. A change is *substantive* unless it only alters
whitespace or letter case.
"""

import difflib
import hashlib
import re
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.db_models import BulletinPublication, BulletinSnapshot, ChangeLogEntry
from models.enums import SectionChangeKind
from models.schemas import (
    BulletinPublicationRecord,
    BulletinSection,
    BulletinSnapshotRecord,
    ChangeLogEntryRecord,
    SectionChange,
)
from services import audit_service
from services.errors import (
    BulletinNotPublishedError,
    BulletinSnapshotNotFoundError,
    ChangeLogEntryNotFoundError,
    InvalidBulletinError,
)

_HEADER = re.compile(r"^##\s+(?P<key>\S+)\s*$")
_META = re.compile(r"^(?P<name>effective|hs codes)\s*:\s*(?P<value>.*?)\s*$", re.IGNORECASE)
_RULE_KEY = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_HS_CODE = re.compile(r"^\d{4}(\d{2}){0,3}$")


# --------------------------------------------------------------------------- parsing (pure)


def normalize_text(text: str) -> str:
    """Text with whitespace runs collapsed and case folded — equal iff not substantive."""
    return " ".join(text.split()).casefold()


def _split(content: str) -> tuple[str, list[tuple[int, str, list[str]]]]:
    """Preamble, then (line number, rule key, body lines) per ``## <key>`` section."""
    preamble: list[str] = []
    sections: list[tuple[int, str, list[str]]] = []
    for number, line in enumerate(content.splitlines(), start=1):
        header = _HEADER.match(line)
        if header:
            sections.append((number, header["key"], []))
        elif sections:
            sections[-1][2].append(line)
        else:
            preamble.append(line)
    return "\n".join(preamble), sections


def _parse_section(number: int, key: str, lines: list[str]) -> BulletinSection:
    where = f"section '{key}' (line {number})"
    if not _RULE_KEY.match(key):
        raise InvalidBulletinError(f"{where}: rule key must be letters/digits joined by '-'")
    meta: dict[str, str] = {}
    body_start = len(lines)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = _META.match(line.strip())
        if match is None or len(meta) == 2:
            body_start = index
            break
        meta[match["name"].lower()] = match["value"]
    missing = [name for name in ("effective", "hs codes") if name not in meta]
    if missing:
        raise InvalidBulletinError(
            f"{where}: needs 'Effective: YYYY-MM-DD' and 'HS codes: ...' lines "
            f"(missing {', '.join(missing)})"
        )
    try:
        effective = date.fromisoformat(meta["effective"])
    except ValueError as exc:
        raise InvalidBulletinError(
            f"{where}: invalid Effective date {meta['effective']!r}"
        ) from exc
    raw_codes = meta["hs codes"]
    codes = (
        ()
        if raw_codes.lower() == "all"
        else tuple(sorted({c.strip() for c in raw_codes.split(",") if c.strip()}))
    )
    bad = [c for c in codes if not _HS_CODE.match(c)]
    if bad or (not codes and raw_codes.lower() != "all"):
        raise InvalidBulletinError(
            f"{where}: HS codes must be 'all' or comma-separated 4/6/8/10-digit codes"
        )
    text = "\n".join(lines[body_start:]).strip()
    if not text:
        raise InvalidBulletinError(f"{where}: rule text is empty")
    return BulletinSection(rule_key=key, effective_date=effective, hs_codes=codes, text=text)


def parse_sections(content: str) -> list[BulletinSection]:
    """The rule sections of a bulletin, in order. Raises ``InvalidBulletinError`` on a format
    error, a duplicate rule key, or a bulletin without any section."""
    _, raw = _split(content)
    if not raw:
        raise InvalidBulletinError("the bulletin has no '## <rule_key>' sections")
    sections = [_parse_section(*item) for item in raw]
    keys = [s.rule_key for s in sections]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise InvalidBulletinError(f"duplicate rule sections: {', '.join(duplicates)}")
    return sections


def diff_bulletins(old: str, new: str) -> list[SectionChange]:
    """Per-rule changes from ``old`` to ``new`` (sections in ``new`` order, then removals)."""
    before = {s.rule_key: s for s in parse_sections(old)}
    after = {s.rule_key: s for s in parse_sections(new)}
    changes: list[SectionChange] = []
    for key, section in after.items():
        prior = before.get(key)
        if prior is None:
            changes.append(_change(key, SectionChangeKind.ADDED, None, section, True))
        elif prior != section:
            substantive = (prior.effective_date, prior.hs_codes) != (
                section.effective_date,
                section.hs_codes,
            ) or normalize_text(prior.text) != normalize_text(section.text)
            changes.append(_change(key, SectionChangeKind.MODIFIED, prior, section, substantive))
    changes += [
        _change(key, SectionChangeKind.REMOVED, section, None, True)
        for key, section in before.items()
        if key not in after
    ]
    return changes


def _change(
    key: str,
    kind: SectionChangeKind,
    before: BulletinSection | None,
    after: BulletinSection | None,
    substantive: bool,
) -> SectionChange:
    return SectionChange(
        rule_key=key, kind=kind, before=before, after=after, is_substantive=substantive
    )


def _assess(old: str, new: str) -> tuple[bool, str, list[SectionChange]]:
    """(is_substantive, diff_summary, section changes) for two bulletin texts that differ.

    Substantive iff some rule section changed beyond whitespace/case (its effective date and
    HS codes are compared as parsed values), or the text outside the sections did.
    """
    changes = diff_bulletins(old, new)
    old_preamble, new_preamble = _split(old)[0], _split(new)[0]
    preamble_substantive = normalize_text(old_preamble) != normalize_text(new_preamble)
    substantive = preamble_substantive or any(c.is_substantive for c in changes)

    lines = ["Substantive change." if substantive else "Formatting-only change (whitespace/case)."]
    lines += [
        f"- {c.rule_key}: {c.kind.value}" + ("" if c.is_substantive else " (formatting only)")
        for c in changes
    ]
    if old_preamble != new_preamble:
        suffix = "" if preamble_substantive else " (formatting only)"
        lines.append(f"- text outside rule sections: modified{suffix}")
    if len(lines) == 1:
        lines.append("- layout only (no rule section or surrounding text changed)")
    return substantive, "\n".join(lines), changes


def unified_diff(old: str, new: str) -> str:
    """A line diff for display (dashboard, review email)."""
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), "previous scan", "latest scan", lineterm=""
        )
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- publication


def _publication_row(session: Session) -> BulletinPublication | None:
    return session.scalars(select(BulletinPublication).with_for_update()).one_or_none()


def get_publication(session: Session) -> BulletinPublicationRecord:
    """The bulletin as currently published (what the next scan will capture)."""
    row = session.scalars(select(BulletinPublication)).one_or_none()
    if row is None:
        raise BulletinNotPublishedError("the customs bulletin has not been published yet")
    return BulletinPublicationRecord.model_validate(row)


def publish(session: Session, content: str, actor: str) -> BulletinPublicationRecord:
    """Replace the published bulletin text (the simulated external source's own edit).

    The text must parse (``InvalidBulletinError`` otherwise). Publishing identical text is a
    no-op. Deliberately does **not** capture a snapshot — only the scheduled scan does.
    """
    content = content.replace("\r\n", "\n").strip() + "\n"
    sections = parse_sections(content)
    row = _publication_row(session)
    previous = None if row is None else row.content
    if previous == content:
        assert row is not None
        return BulletinPublicationRecord.model_validate(row)

    if row is None:
        row = BulletinPublication(content=content, updated_by=actor)
        session.add(row)
    else:
        row.content, row.updated_by = content, actor
    session.flush()
    audit_service.record(
        session,
        actor=actor,
        action="bulletin.published",
        entity_type="bulletin_publication",
        entity_id=row.id,
        payload={
            "sections": [s.rule_key for s in sections],
            "sha256": _sha256(content),
            "previous_sha256": None if previous is None else _sha256(previous),
        },
    )
    session.flush()
    return BulletinPublicationRecord.model_validate(row)


# --------------------------------------------------------------------------- scan path


def capture_snapshot(session: Session, actor: str) -> BulletinSnapshotRecord:
    """Record what the bulletin says right now. Called only by the scheduled scan."""
    publication = get_publication(session)
    row = BulletinSnapshot(content=publication.content)
    session.add(row)
    session.flush()
    session.refresh(row)  # load the identity-generated seq
    audit_service.record(
        session,
        actor=actor,
        action="bulletin_snapshot.captured",
        entity_type="bulletin_snapshot",
        entity_id=row.id,
        payload={"seq": row.seq, "sha256": _sha256(row.content)},
    )
    session.flush()
    return BulletinSnapshotRecord.model_validate(row)


def get_latest_snapshot(session: Session) -> BulletinSnapshotRecord:
    """The most recent scan-time snapshot — the system's current view of the bulletin."""
    snapshots = list_snapshots(session, limit=1)
    if not snapshots:
        raise BulletinSnapshotNotFoundError("no bulletin snapshot has been captured yet")
    return snapshots[0]


def list_snapshots(session: Session, limit: int | None = None) -> list[BulletinSnapshotRecord]:
    """Snapshots, newest first."""
    query = select(BulletinSnapshot).order_by(BulletinSnapshot.seq.desc()).limit(limit)
    return [BulletinSnapshotRecord.model_validate(row) for row in session.scalars(query)]


def diff_since_last(session: Session, actor: str) -> ChangeLogEntryRecord | None:
    """Log and return the change between the two most recent snapshots, if not logged yet.

    Returns ``None`` when there are fewer than two snapshots, when they are identical, or when
    this pair was already logged — so repeated calls without a new capture never duplicate a
    detection (nor, therefore, a proposal).
    """
    latest_two = list(
        session.scalars(select(BulletinSnapshot).order_by(BulletinSnapshot.seq.desc()).limit(2))
    )
    if len(latest_two) < 2:
        return None
    new, prior = latest_two
    if new.content == prior.content:
        return None
    already = session.scalars(
        select(ChangeLogEntry).where(
            ChangeLogEntry.prior_snapshot_id == prior.id,
            ChangeLogEntry.new_snapshot_id == new.id,
        )
    ).one_or_none()
    if already is not None:
        return None

    substantive, summary, changes = _assess(prior.content, new.content)
    row = ChangeLogEntry(
        prior_snapshot_id=prior.id,
        new_snapshot_id=new.id,
        diff_summary=summary,
        is_substantive=substantive,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if "uq_change_log_prior_new_snapshot" in str(exc.orig):
            return None  # a concurrent scan logged this pair first
        raise
    audit_service.record(
        session,
        actor=actor,
        action="bulletin_change.detected",
        entity_type="bulletin_change",
        entity_id=row.id,
        payload={
            "prior_seq": prior.seq,
            "new_seq": new.seq,
            "is_substantive": substantive,
            "sections": {c.rule_key: c.kind.value for c in changes},
        },
    )
    session.flush()
    return ChangeLogEntryRecord.model_validate(row)


def get_change(session: Session, change_id: UUID) -> ChangeLogEntryRecord:
    """One change_log entry."""
    row = session.get(ChangeLogEntry, change_id)
    if row is None:
        raise ChangeLogEntryNotFoundError(f"no bulletin change with id {change_id}")
    return ChangeLogEntryRecord.model_validate(row)


def list_changes(session: Session) -> list[ChangeLogEntryRecord]:
    """Every detected change, newest first."""
    query = select(ChangeLogEntry).order_by(ChangeLogEntry.detected_at.desc())
    return [ChangeLogEntryRecord.model_validate(row) for row in session.scalars(query)]


def change_texts(session: Session, change_id: UUID) -> tuple[str, str]:
    """The (prior, new) snapshot texts a change_log entry was computed from."""
    entry = get_change(session, change_id)
    prior = session.get_one(BulletinSnapshot, entry.prior_snapshot_id)
    new = session.get_one(BulletinSnapshot, entry.new_snapshot_id)
    return prior.content, new.content


def section_changes(session: Session, change_id: UUID) -> list[SectionChange]:
    """The per-rule changes behind a change_log entry — one proposal per ``rule_key``."""
    return diff_bulletins(*change_texts(session, change_id))
