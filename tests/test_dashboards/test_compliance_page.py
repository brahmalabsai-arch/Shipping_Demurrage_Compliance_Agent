"""Phase 4 — Compliance dashboard: proposals land as pending, and only an Approve/Reject click
with a named reviewer (``compliance_service.decide``) changes a rule's status."""

from datetime import date

from sqlalchemy.orm import Session, sessionmaker
from streamlit.testing.v1 import AppTest

from config.settings import PROJECT_ROOT
from models.enums import RuleStatus
from models.schemas import ComplianceRuleRecord, RuleProposal
from scripts import seed_compliance
from services import compliance_service as svc

PAGE = str(PROJECT_ROOT / "dashboards" / "pages" / "4_Compliance.py")
KEY = "HSN-8708-duty"


def _open_page(reviewer: str = "") -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception, at.exception
    if reviewer:
        at.text_input(key="reviewer").set_value(reviewer)
        at.run()
    return at


def _pending_v2(session_factory: sessionmaker[Session]) -> ComplianceRuleRecord:
    seed_compliance.seed()
    with session_factory() as session, session.begin():
        current = svc.get_current_rule(session, KEY)
        return svc.propose_change(
            session,
            RuleProposal(
                rule_key=KEY,
                content=current.content.replace("15%", "20%"),
                hs_codes=current.hs_codes,
                effective_date=date(2026, 10, 1),
                change_summary="BCD raised to 20%",
            ),
            "regulatory_watch_agent",
        )


def test_approve_click_with_reviewer_puts_the_new_version_in_force(
    session_factory: sessionmaker[Session],
) -> None:
    proposal = _pending_v2(session_factory)
    at = _open_page(reviewer="a.sharma")
    assert "BCD raised to 20%" in [c.value for c in at.caption]
    assert any("+20% of the assessable" in c.value for c in at.code)  # diff vs v1 shown

    at.button(key=f"approve_{proposal.id}").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert at.success[0].value.startswith(f"Approved {KEY} v2")
    with session_factory() as session:
        current = svc.get_current_rule(session, KEY)
        assert (current.id, current.decided_by) == (proposal.id, "a.sharma")
        v1 = svc.list_rules(session, rule_key=KEY)[0]
        assert v1.superseded_by == proposal.id


def test_decision_without_a_reviewer_name_is_refused(
    session_factory: sessionmaker[Session],
) -> None:
    proposal = _pending_v2(session_factory)
    at = _open_page()  # reviewer left blank
    assert at.warning[0].value.startswith("Enter your name")

    at.button(key=f"approve_{proposal.id}").click()
    at.run()

    assert at.error[0].value.startswith("InvalidDecisionError")
    with session_factory() as session:
        assert svc.get_rule(session, proposal.id).status is RuleStatus.PENDING


def test_reject_click_is_final(session_factory: sessionmaker[Session]) -> None:
    proposal = _pending_v2(session_factory)
    at = _open_page(reviewer="a.sharma")

    at.button(key=f"reject_{proposal.id}").click()
    at.run()

    assert not at.error, [e.value for e in at.error]
    with session_factory() as session:
        assert svc.get_rule(session, proposal.id).status is RuleStatus.REJECTED
        assert svc.list_pending(session) == []
        assert svc.get_current_rule(session, KEY).version == 1
    assert f"approve_{proposal.id}" not in [b.key for b in at.button]


def test_proposal_from_the_form_is_pending_until_decided(
    session_factory: sessionmaker[Session],
) -> None:
    seed_compliance.seed()
    at = _open_page()
    at.selectbox(key="propose_template").set_value("ORG-KR-CEPA")
    at.run()
    at.text_area(key="propose_content_ORG-KR-CEPA").set_value("New origin wording.")
    at.text_input(key="propose_summary_ORG-KR-CEPA").set_value("Clarified issuing authority")
    at.button(key="propose_submit_ORG-KR-CEPA").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    with session_factory() as session:
        [pending] = svc.list_pending(session)
        assert (pending.rule_key, pending.version, pending.status) == (
            "ORG-KR-CEPA",
            2,
            RuleStatus.PENDING,
        )
        assert pending.hs_codes == ["8708"]
        assert svc.get_current_rule(session, "ORG-KR-CEPA").version == 1


def test_invalid_proposal_shows_validation_error(session_factory: sessionmaker[Session]) -> None:
    at = _open_page()
    at.text_input(key="propose_key_new").set_value("bad key!")
    at.text_area(key="propose_content_new").set_value("x")
    at.button(key="propose_submit_new").click()
    at.run()

    assert "rule_key" in at.error[0].value
    with session_factory() as session:
        assert svc.list_rules(session) == []


def test_rulebook_filters_by_hs_code(session_factory: sessionmaker[Session]) -> None:
    seed_compliance.seed()
    at = _open_page()
    assert len(at.dataframe[0].value) == 6

    at.text_input(key="rulebook_hs").set_value("87084000")  # gearboxes: not brakes
    at.run()

    rules = set(at.dataframe[0].value["Rule"])
    assert "HSN-870830-brake-certification" not in rules
    assert {"HSN-8708-duty", "ORG-KR-CEPA", "DOC-quantity-consistency"} <= rules
