"""The walkthrough (``docs/WALKTHROUGH.md``): every case, from what stops it to ``cleared``.

Each test plays one case the way the guide does on the dashboards — a clearance run, the fix
through the same service call the page makes, a new run, a reviewer's approval — so a guide step
that stops working fails here first. Runs use in-memory MCP servers, no LLM and no email.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from agents import supervisor
from agents.mcp_clients import memory_connector
from models.enums import DocType, RuleDecision, RunState, ShipmentStatus
from models.schemas import (
    DocumentAmend,
    DocumentCreate,
    PortRateUpsert,
    RuleProposal,
    ShipmentUpdate,
)
from scripts import (
    seed_compliance,
    seed_fx_rates,
    seed_port_rates,
    seed_shipments,
    seed_walkthrough,
)
from services import bulletin_service, compliance_service, demurrage_service, fx_service
from services import shipping_service as ship
from services.db import session_scope
from tests.fixtures.compliance import BASELINE_BULLETIN
from tests.fixtures.walkthrough import (
    AS_OF,
    LAMP_RULE_KEY,
    LAMP_RULE_SECTION,
    REEFER_TARIFF,
    WALKTHROUGH,
    WT14_LOCKS,
)

REVIEWER = "Asha Rao"  # registered in tests/conftest.py
S = ShipmentStatus


@pytest.fixture(autouse=True)
def seeded(session_factory: sessionmaker[Session], checkpoints: None) -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()
    seed_walkthrough.seed()


# --------------------------------------------------------------------------- helpers


def sid(container_id: str) -> UUID:
    with session_scope() as session:
        return ship.get_shipment_by_container_id(session, container_id).id


def status(container_id: str) -> ShipmentStatus:
    with session_scope() as session:
        return ship.get_shipment_by_container_id(session, container_id).status


def document(container_id: str, doc_type: DocType) -> UUID:
    with session_scope() as session:
        docs = ship.get_documents(session, sid(container_id))
    return next(d.id for d in docs if d.doc_type is doc_type)


async def start(container_id: str) -> supervisor.ClearanceRun:
    return await supervisor.start_clearance_run(
        sid(container_id), started_by="walkthrough", connector=memory_connector,
        model_factory=lambda: None, as_of=AS_OF,
    )  # fmt: skip


async def decide_filing(run: supervisor.ClearanceRun, decision: str,
                        note: str = "") -> supervisor.ClearanceRun:  # fmt: skip
    return await supervisor.resume_after_filing_approval(
        run.run.run_id, decision, REVIEWER, note, connector=memory_connector
    )


def items(run: supervisor.ClearanceRun, severity: str) -> list[str]:
    checklist = run.case.checklist
    assert checklist is not None
    return [i.title for i in checklist.items if i.severity == severity]


async def file_it(container_id: str) -> None:
    """A fresh run reaches the filing gate, the reviewer approves, the shipment is cleared."""
    run = await start(container_id)
    assert run.run.state is RunState.AWAITING_FILING, run.run.detail
    done = await decide_filing(run, "approve", "walkthrough")
    assert done.run.state is RunState.FILED, done.run.detail
    assert status(container_id) is S.CLEARED


# --------------------------------------------------------------------------- the seed


def test_the_walkthrough_seed_is_complete_and_idempotent() -> None:
    with session_scope() as session:
        present = {s.container_id for s in ship.list_shipments(session, None)}
    assert {f.shipment.container_id for f in WALKTHROUGH} <= present
    assert all(line.startswith("skipped") for line in seed_walkthrough.seed())


# --------------------------------------------------------------------------- the cases


async def test_wt01_the_straight_path() -> None:
    run = await start("OOLU8101010")
    assert run.run.state is RunState.AWAITING_FILING
    assert run.case.risk == "low" and items(run, "blocker") == []
    done = await decide_filing(run, "approve", "walkthrough")
    assert done.run.state is RunState.FILED and status("OOLU8101010") is S.CLEARED


async def test_wt02_packing_list_short() -> None:
    run = await start("EGHU8102020")
    assert run.run.state is RunState.BLOCKED
    assert items(run, "blocker") == ["quantity mismatch"]
    assert run.case.risk == "high"  # one chargeable day already

    with session_scope() as session:  # Papers -> Amend a document: packing list, qty 500
        ship.amend_document(session, document("EGHU8102020", DocType.PACKING_LIST),
                            DocumentAmend(qty=Decimal(500)), "walkthrough")  # fmt: skip
    await file_it("EGHU8102020")


async def test_wt03_invoice_value_differs_from_the_booking() -> None:
    run = await start("TGHU8103030")
    assert items(run, "blocker") == ["value mismatch"]

    with session_scope() as session:  # Details -> Edit the booking: declared value 64,800
        ship.update_shipment(session, sid("TGHU8103030"),
                             ShipmentUpdate(declared_value=Decimal("64800")), "walkthrough")  # fmt: skip
    await file_it("TGHU8103030")


async def test_wt04_invoice_in_the_wrong_currency() -> None:
    run = await start("CMAU8104040")
    assert items(run, "blocker") == ["currency mismatch"]  # values are not compared yet
    assert run.case.fx_context is not None and run.case.fx_context.rates == []

    with session_scope() as session:  # Papers -> Amend the invoice: currency EUR
        ship.amend_document(session, document("CMAU8104040", DocType.INVOICE),
                            DocumentAmend(currency="EUR"), "walkthrough")  # fmt: skip
    await file_it("CMAU8104040")


async def test_wt05_currency_code_typo() -> None:
    run = await start("HLXU8105050")
    assert items(run, "blocker") == ["invalid currency"]

    with session_scope() as session:  # Papers -> Amend the invoice: currency USD
        ship.amend_document(session, document("HLXU8105050", DocType.INVOICE),
                            DocumentAmend(currency="USD"), "walkthrough")  # fmt: skip
    await file_it("HLXU8105050")


async def test_wt06_bill_of_lading_missing() -> None:
    run = await start("MSCU8106060")
    assert items(run, "blocker") == ["missing document"]

    with session_scope() as session:  # Papers -> Attach a document: bill of lading
        ship.add_document(session, sid("MSCU8106060"), DocumentCreate(
            doc_type=DocType.BILL_OF_LADING, qty=Decimal(1100), issued_date=date(2026, 9, 5)),
            "walkthrough")  # fmt: skip
    await file_it("MSCU8106060")


async def test_wt07_still_in_transit() -> None:
    run = await start("ONEU8107070")
    assert run.run.state is RunState.NOT_FILEABLE
    assert run.case.risk == "medium"  # the ETA has passed without docking

    with session_scope() as session:  # Details -> Move to the next stage: Docked
        ship.update_status(session, sid("ONEU8107070"), S.DOCKED, "walkthrough")
    await file_it("ONEU8107070")


async def test_wt08_held_by_customs() -> None:
    run = await start("YMLU8108080")
    assert run.run.state is RunState.NOT_FILEABLE and "held" in (run.run.detail or "")
    assert run.case.risk == "high"

    with session_scope() as session:  # Details -> Move to the next stage: Under review
        ship.update_status(session, sid("YMLU8108080"), S.UNDER_REVIEW, "walkthrough")
    await file_it("YMLU8108080")


async def test_wt09_reefer_without_a_tariff() -> None:
    run = await start("SEGU8109090")
    assert run.run.state is RunState.BLOCKED
    assert items(run, "blocker") == ["data gap: tariff"]
    assert items(run, "action") == []  # the papers and rates are in order
    assert run.case.risk == "indeterminate"

    with session_scope() as session:  # Demurrage -> Add or revise a tariff
        demurrage_service.upsert_rate(session, PortRateUpsert.model_validate(REEFER_TARIFF),
                                      "walkthrough")  # fmt: skip
    await file_it("SEGU8109090")


async def test_wt09_a_tariff_starting_after_arrival_does_not_close_the_gap() -> None:
    with session_scope() as session:  # the date picker's default (today) is too late
        demurrage_service.upsert_rate(session, PortRateUpsert.model_validate(
            {**REEFER_TARIFF, "effective_from": AS_OF}), "walkthrough")  # fmt: skip
    run = await start("SEGU8109090")
    assert items(run, "blocker") == ["data gap: tariff"]


async def test_wt10_no_arrival_date() -> None:
    run = await start("ZIMU8110100")
    assert items(run, "blocker") == ["data gap: eta"]

    with session_scope() as session:  # Details -> Edit the booking: arrival 1 Oct, 09:00 IST
        ship.update_shipment(session, sid("ZIMU8110100"), ShipmentUpdate.model_validate(
            {"eta": "2026-10-01T03:30:00+00:00"}), "walkthrough")  # fmt: skip
    await file_it("ZIMU8110100")


async def test_wt11_a_new_rule_for_the_goods_waits_for_review() -> None:
    # Customs Bulletin -> Edit: the new section; the scan proposes it as a pending rule
    [section] = [s for s in bulletin_service.parse_sections(
        BASELINE_BULLETIN + "\n" + LAMP_RULE_SECTION) if s.rule_key == LAMP_RULE_KEY]  # fmt: skip
    assert list(section.hs_codes) == ["8512"]
    with session_scope() as session:
        rule = compliance_service.propose_change(session, RuleProposal(
            rule_key=section.rule_key, content=section.text, hs_codes=list(section.hs_codes),
            effective_date=section.effective_date), "regulatory_watch_agent")  # fmt: skip

    run = await start("BMOU8111110")
    assert run.run.state is RunState.AWAITING_COMPLIANCE
    assert run.run.awaiting_rule_ids == [rule.id]
    # the rule covers heading 8512 only: the 8708 cases do not wait on it
    assert (await start("OOLU8101010")).run.state is RunState.AWAITING_FILING

    with session_scope() as session:  # Compliance -> Waiting for review -> Approve
        compliance_service.decide(session, rule.id, RuleDecision.APPROVE, REVIEWER, "notified")
    resumed = await supervisor.resume_after_compliance_decision(
        run.run.run_id, rule.id, connector=memory_connector, model_factory=lambda: None
    )  # Shipping Ops -> Clearance -> Resume run
    assert resumed.run.state is RunState.AWAITING_FILING
    done = await decide_filing(resumed, "approve", "walkthrough")
    assert done.run.state is RunState.FILED and status("BMOU8111110") is S.CLEARED


async def test_wt12_the_first_filing_is_rejected() -> None:
    run = await start("TCNU8112120")
    assert run.run.state is RunState.AWAITING_FILING
    rejected = await decide_filing(run, "reject", "No Korean certificate of origin on file")
    assert rejected.run.state is RunState.FILING_REJECTED
    assert status("TCNU8112120") is S.DOCKED  # a rejection changes nothing on the shipment

    with session_scope() as session:  # Papers -> Attach a document: certificate
        ship.add_document(session, sid("TCNU8112120"), DocumentCreate(
            doc_type=DocType.CERTIFICATE, issued_date=date(2026, 9, 1)), "walkthrough")  # fmt: skip
    await file_it("TCNU8112120")  # a new run: the rejected one is final


async def test_wt13_papers_change_while_the_filing_waits() -> None:
    run = await start("FCIU8113130")
    assert run.run.state is RunState.AWAITING_FILING
    with session_scope() as session:  # Papers -> Attach the certificate of conformity
        ship.add_document(session, sid("FCIU8113130"), DocumentCreate(
            doc_type=DocType.CERTIFICATE, issued_date=date(2026, 9, 25)), "walkthrough")  # fmt: skip

    done = await decide_filing(run, "approve", "walkthrough")
    assert done.run.state is RunState.BLOCKED
    assert "documents changed" in (done.run.detail or "")
    assert status("FCIU8113130") is S.DOCKED
    await file_it("FCIU8113130")  # a new run analyses the papers as they are now


async def test_wt14_from_quotation_to_clearance() -> None:
    run = await start("GESU8114140")
    assert run.run.state is RunState.NOT_FILEABLE  # only quoted
    assert "quote expired" in items(run, "action")

    cid, shipment = "GESU8114140", sid("GESU8114140")
    with session_scope() as session:
        # Details -> Edit the booking: invoice date and on-board date
        ship.update_shipment(session, shipment, ShipmentUpdate(
            invoice_date=date(2026, 9, 18), bol_date=date(2026, 9, 20)), "walkthrough")  # fmt: skip
        # Papers -> Attach the three documents
        for doc in (
            DocumentCreate(doc_type=DocType.INVOICE, qty=Decimal(1200), value=Decimal(66000),
                           currency="USD", issued_date=date(2026, 9, 18)),
            DocumentCreate(doc_type=DocType.PACKING_LIST, qty=Decimal(1200),
                           issued_date=date(2026, 9, 18)),
            DocumentCreate(doc_type=DocType.BILL_OF_LADING, qty=Decimal(1200),
                           issued_date=date(2026, 9, 20)),
        ):  # fmt: skip
            ship.add_document(session, shipment, doc, "walkthrough")
        # FX Rates -> Locked: the invoice and bill of lading dates
        for lock in WT14_LOCKS:
            fx_service.lock_rate(session, lock, "walkthrough")
        # Details -> Move to the next stage, three times
        for step in (S.BOOKED, S.IN_TRANSIT, S.DOCKED):
            ship.update_status(session, shipment, step, "walkthrough")

    run = await start(cid)
    assert items(run, "blocker") == [] and items(run, "action") == []
    await decide_filing(run, "approve", "walkthrough")
    assert status(cid) is S.CLEARED


async def test_wt15_to_do_items_do_not_stop_a_filing() -> None:
    run = await start("TRHU8115150")
    assert run.run.state is RunState.AWAITING_FILING
    assert items(run, "blocker") == []
    assert sorted(items(run, "action")) == [  # both missing rates are listed
        "date mismatch", "demurrage risk medium", "fx rate unavailable", "fx rate unavailable"
    ]  # fmt: skip
    done = await decide_filing(run, "approve", "filed with the loose ends noted")
    assert done.run.state is RunState.FILED and status("TRHU8115150") is S.CLEARED
