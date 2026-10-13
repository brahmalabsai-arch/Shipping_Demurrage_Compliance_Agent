"""Phase 13 — ``docs/DEMO_SCRIPT.md`` steps 5-7 as a test, on freshly seeded data: the
bulletin change reaches a reviewer, the clearance run waits for the decision and then for the
filing approval, and the ROI is the documented INR 82,212.00. (Step 2's table is pinned in
``test_packaging.py``.) Same functions the dashboards call; in-memory MCP servers."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from agents import supervisor
from agents.mcp_clients import memory_connector
from config.settings import get_settings
from models.enums import RuleStatus, RunState, ShipmentStatus
from models.schemas import RoiAssumptions
from roi.calculator import compare
from scheduler import watch_scheduler
from scripts import seed
from services import bulletin_service, compliance_service, roi_service, shipping_service

AS_OF = date(2026, 9, 28)
REVIEWER = "Asha Rao"  # registered in tests/conftest.py


async def test_the_demo_script_story(
    session_factory: sessionmaker[Session], checkpoints: None
) -> None:
    seed.main()

    # 5. The bulletin raises the duty to 20%; the scheduled scan stages v2 and emails the reviewer
    with session_factory() as session, session.begin():
        bulletin = bulletin_service.get_publication(session).content
        assert "15% of the assessable value" in bulletin
        bulletin_service.publish(
            session, bulletin.replace("15% of the assessable value", "20% of the assessable value"),
            "editor",
        )  # fmt: skip
    scan = await watch_scheduler.run_scan(connector=memory_connector, model_factory=lambda: None,
                                          reviewer_email="reviewer@example.com")  # fmt: skip
    assert scan is not None
    [v2] = scan.proposals
    assert (v2.rule_key, v2.version, v2.status) == ("HSN-8708-duty", 2, RuleStatus.PENDING)
    assert list(Path(get_settings().outbox_path).glob("*.eml"))  # the review email

    # 6. The clearance run waits for the decision, then for the filing approval
    with session_factory() as session:
        msku = shipping_service.get_shipment_by_container_id(session, "MSKU7001001")
    run = await supervisor.start_clearance_run(msku.id, connector=memory_connector,
                                               model_factory=lambda: None, as_of=AS_OF)  # fmt: skip
    assert run.run.state is RunState.AWAITING_COMPLIANCE and run.run.awaiting_rule_ids == [v2.id]

    with session_factory() as session, session.begin():
        compliance_service.decide(session, v2.id, "approve", REVIEWER)
    resumed = await supervisor.resume_after_compliance_decision(
        run.run.run_id, v2.id, connector=memory_connector, model_factory=lambda: None
    )
    assert resumed.run.state is RunState.AWAITING_FILING
    duty = next(f for f in resumed.case.compliance_findings
                if f.question.startswith("What basic customs"))  # fmt: skip
    assert [(c.rule_key, c.version) for c in duty.citations] == [("HSN-8708-duty", 2)]

    filed = await supervisor.resume_after_filing_approval(
        run.run.run_id, "approve", REVIEWER, "papers in order", connector=memory_connector
    )
    assert filed.run.state is RunState.FILED
    with session_factory() as session:
        assert shipping_service.get_shipment(session, msku.id).status is ShipmentStatus.CLEARED

    # 7. The ROI of the agents: the documented figures, unaffected by the steps above
    with session_factory() as session, session.begin():
        snapshot = roi_service.capture_snapshot(session, "demo", AS_OF, RoiAssumptions(), "demo")
    result = compare(snapshot, snapshot)
    lines = {line.container_id: line.avoided_inr for line in result.lines}
    assert lines == {"MSKU7001001": Decimal("0.00"), "MSCU7002002": Decimal("23868.00"),
                     "CMAU7003003": Decimal("58344.00"), "HLXU7004004": Decimal("0.00")}  # fmt: skip
    assert result.total_avoided_inr == Decimal("82212.00")
