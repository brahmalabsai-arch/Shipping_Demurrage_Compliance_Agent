"""Seed the simulated customs bulletin and the baseline compliance rulebook.

* The bulletin is published only if nothing has been published yet — a dashboard edit is
  never overwritten. No snapshot is captured: only the scheduled scan does that (Phase 6).
* Each section of the baseline bulletin becomes version 1 of its rule, proposed and then
  approved through ``compliance_service.decide`` (the only path to ``approved``), with
  ``decided_by='seed_script'`` in the audit trail. A rule_key that already has any version is
  skipped.

    .venv\\Scripts\\python.exe -m scripts.seed_compliance
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from models.enums import RuleDecision
from models.schemas import BulletinSection, RuleProposal
from services import bulletin_service, compliance_service
from services.db import session_scope
from services.errors import BulletinNotPublishedError
from tests.fixtures.compliance import BASELINE_BULLETIN

ACTOR = "seed_script"


@dataclass(frozen=True)
class SeedResult:
    label: str
    outcome: Literal["created", "skipped"]


def seed_bulletin(session: Session) -> SeedResult:
    """Publish the baseline bulletin unless a bulletin is already published."""
    try:
        bulletin_service.get_publication(session)
        return SeedResult("customs bulletin", "skipped")
    except BulletinNotPublishedError:
        pass
    bulletin_service.publish(session, BASELINE_BULLETIN, ACTOR)
    return SeedResult("customs bulletin", "created")


def seed_rule(session: Session, section: BulletinSection) -> SeedResult:
    """Import one bulletin section as an approved v1 rule unless the rule already exists."""
    label = f"rule {section.rule_key} v1 (approved)"
    if compliance_service.list_rules(session, rule_key=section.rule_key):
        return SeedResult(label, "skipped")
    proposal = RuleProposal(
        rule_key=section.rule_key,
        content=section.text,
        hs_codes=list(section.hs_codes),
        effective_date=section.effective_date,
        change_summary="Baseline rulebook import",
    )
    rule = compliance_service.propose_change(session, proposal, ACTOR)
    compliance_service.decide(session, rule.id, RuleDecision.APPROVE, ACTOR)
    return SeedResult(label, "created")


def seed() -> list[SeedResult]:
    """Seed the bulletin and every baseline rule, each in its own transaction."""
    with session_scope() as session:
        results = [seed_bulletin(session)]
    for section in bulletin_service.parse_sections(BASELINE_BULLETIN):
        with session_scope() as session:
            results.append(seed_rule(session, section))
    return results


def main() -> None:
    for result in seed():
        print(f"{result.outcome:8} {result.label}")


if __name__ == "__main__":
    main()
