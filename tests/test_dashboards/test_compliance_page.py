"""Phase 4 — Compliance dashboard: proposals land as pending, and only an Approve/Reject click
by a registered reviewer (``compliance_service.decide``; Phase 11 registry) changes a rule's
status; an approval in force can be revoked (Phase 11)."""

from datetime import date
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker
from streamlit.testing.v1 import AppTest

from agents import compliance_rag_agent
from agents.mcp_clients import memory_connector
from config.settings import PROJECT_ROOT
from models.enums import RuleStatus, RunState
from models.schemas import ComplianceRuleRecord, RuleProposal
from scripts import seed_compliance
from services import compliance_service as svc
from services import governance_service, shipping_service
from tests.fixtures.shipments import make_shipment

PAGE = str(PROJECT_ROOT / "dashboards" / "views" / "4_Compliance.py")
KEY = "HSN-8708-duty"


REVIEWER = "Asha Rao"  # registered in tests/conftest.py


def table_with(at: AppTest, column: str) -> Any:
    """The (last) table on the page that has this column."""
    return next(t.value for t in reversed(at.table) if column in t.value.columns)


def _open_page(reviewer: str = "") -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception, at.exception
    if reviewer:
        at.selectbox(key="reviewer").set_value(reviewer)
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
    at = _open_page(reviewer=REVIEWER)
    assert any("BCD raised to 20%" in c.value for c in at.caption)
    assert any("+20% of the assessable" in c.value for c in at.code)  # diff vs v1 shown

    at.button(key=f"approve_{proposal.id}").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert at.toast[0].value.startswith(f"Approved {KEY} v2")
    with session_factory() as session:
        current = svc.get_current_rule(session, KEY)
        assert (current.id, current.decided_by) == (proposal.id, REVIEWER)
        v1 = svc.list_rules(session, rule_key=KEY)[0]
        assert v1.superseded_by == proposal.id


def test_only_registered_reviewers_can_decide(
    session_factory: sessionmaker[Session],
) -> None:
    proposal = _pending_v2(session_factory)
    at = _open_page()  # no reviewer chosen
    assert at.warning[0].value.startswith("Choose yourself")
    assert at.selectbox(key="reviewer").options == ["Asha Rao", "Vikram Shah"]  # no free text
    assert at.button(key=f"approve_{proposal.id}").disabled
    assert at.button(key=f"reject_{proposal.id}").disabled
    with session_factory() as session:
        assert svc.get_rule(session, proposal.id).status is RuleStatus.PENDING


def test_reject_click_is_final(session_factory: sessionmaker[Session]) -> None:
    proposal = _pending_v2(session_factory)
    at = _open_page(reviewer=REVIEWER)

    at.text_input(key=f"note_{proposal.id}").set_value("awaiting the official notification")
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

    assert "rule key" in at.error[0].value.lower()
    with session_factory() as session:
        assert svc.list_rules(session) == []


def test_rulebook_filters_by_hs_code(session_factory: sessionmaker[Session]) -> None:
    seed_compliance.seed()
    at = _open_page()
    assert len(at.table[0].value) == 6

    at.text_input(key="rulebook_hs").set_value("87084000")  # gearboxes: not brakes
    at.run()

    rules = set(at.table[0].value["Rule"])
    assert "HSN-870830-brake-certification" not in rules
    assert {"HSN-8708-duty", "ORG-KR-CEPA", "DOC-quantity-consistency"} <= rules


def test_reject_without_a_reason_is_refused(session_factory: sessionmaker[Session]) -> None:
    proposal = _pending_v2(session_factory)
    at = _open_page(reviewer=REVIEWER)

    at.button(key=f"reject_{proposal.id}").click()
    at.run()

    assert at.error[0].value == "A rejection must give a reason"
    with session_factory() as session:
        assert svc.get_rule(session, proposal.id).status is RuleStatus.PENDING


def test_card_compares_with_the_rule_in_force_and_shows_history(
    session_factory: sessionmaker[Session],
) -> None:
    """v2 (20%) rejected with a reason, v3 (25%) pending: the v3 card diffs against v1 (15%)
    and says why v2 was rejected; the reason shows in All versions too."""
    v2 = _pending_v2(session_factory)
    with session_factory() as session, session.begin():
        svc.decide(session, v2.id, "reject", "a.sharma", "awaiting official notification")
        current = svc.get_current_rule(session, KEY)
        v3 = svc.propose_change(
            session,
            RuleProposal(rule_key=KEY, content=current.content.replace("15%", "25%"),
                         hs_codes=current.hs_codes, effective_date=date(2026, 10, 1)),
            "regulatory_watch_agent",
        )  # fmt: skip

    at = _open_page(reviewer=REVIEWER)

    assert v3.version == 3
    assert any("v2 (proposed " in w.value and "awaiting official notification" in w.value
               for w in at.warning)  # fmt: skip
    [diff] = [c.value for c in at.code if "(in force)" in c.value]
    assert "--- v1 (in force)" in diff and "+++ v3 (proposed)" in diff
    assert "-15% of the assessable" in diff and "+25% of the assessable" in diff
    history = table_with(at, "Revocation reason")  # All versions
    assert "awaiting official notification" in set(history["Reason"])


# --------------------------------------------------------------------------- Phase 9


def test_ask_the_rulebook_answers_with_a_citation(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compliance_rag_agent, "stdio_connector", memory_connector)
    seed_compliance.seed()
    at = _open_page()

    at.text_input(key="ask_question").set_value(
        "Can customs assess the bill of entry before the commercial invoice is filed?"
    )
    at.button(key="ask_submit").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    [answer] = at.success
    assert answer.value.startswith("Per [DOC-commercial-invoice-required v1]")
    citations = table_with(at, "Relevance")
    assert citations.loc[0, "Rule"] == "DOC-commercial-invoice-required"


def test_ask_the_rulebook_says_not_found(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compliance_rag_agent, "stdio_connector", memory_connector)
    seed_compliance.seed()
    at = _open_page()

    at.text_input(key="ask_question").set_value("Which licence is needed to import live poultry?")
    at.button(key="ask_submit").click()
    at.run()

    assert not at.exception, at.exception
    assert any(i.value == "Not found in current rulebook." for i in at.info)


# --------------------------------------------------------------------------- Phase 11


def test_revoke_puts_the_previous_version_back_in_force(
    session_factory: sessionmaker[Session],
) -> None:
    proposal = _pending_v2(session_factory)
    with session_factory() as session, session.begin():
        svc.decide(session, proposal.id, "approve", REVIEWER)
    at = _open_page(reviewer="Vikram Shah")

    at.selectbox(key="revoke_pick").set_value(f"{KEY} v2")
    at.button(key="revoke_submit").click()
    at.run()
    assert at.error[0].value == "A revocation must give a reason"

    at.text_input(key="revoke_reason").set_value("notification withdrawn")
    at.button(key="revoke_submit").click()
    at.run()

    assert not at.error, [e.value for e in at.error]
    assert at.toast[0].value == f"Revoked {KEY} v2 (by Vikram Shah)."
    with session_factory() as session:
        revoked = svc.get_rule(session, proposal.id)
        assert (revoked.status, revoked.decided_by, revoked.revoked_by) == (
            RuleStatus.REVOKED, REVIEWER, "Vikram Shah"
        )  # fmt: skip
        assert svc.get_current_rule(session, KEY).version == 1
    history = table_with(at, "Revocation reason")
    assert "notification withdrawn" in set(history["Revocation reason"])


def test_a_pending_card_says_which_clearance_runs_wait_on_it(
    session_factory: sessionmaker[Session],
) -> None:
    proposal = _pending_v2(session_factory)
    with session_factory() as session, session.begin():
        sid = shipping_service.create_shipment(session, make_shipment(), "test").id
        governance_service.start_run(session, "11111111-1111-1111-1111-111111111111", sid,
                                     date(2026, 9, 28), "test")  # fmt: skip
        governance_service.update_run(session, "11111111-1111-1111-1111-111111111111",
                                      RunState.AWAITING_COMPLIANCE, actor="supervisor",
                                      awaiting_rule_ids=[proposal.id])  # fmt: skip

    at = _open_page(reviewer=REVIEWER)

    assert any(i.value.startswith("1 clearance run(s) are waiting") for i in at.info)
