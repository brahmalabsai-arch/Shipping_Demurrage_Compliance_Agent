"""Phase 7 — the Document Verification agent over MCP (in-memory servers, real protocol) against
the seeded JNPT shipments and FX rates.

Exit criterion: of the four fixtures exactly ``qty_mismatch`` and ``missing_document`` are
flagged, each with its field-level discrepancy. Then the Phase 7 edge cases and the Phase 1 / 3
items that name this agent. The LLM is either absent or a fake — no test reaches a provider.
"""

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import structlog
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agents import document_verification_agent as agent
from agents.mcp_clients import memory_connector
from agents.state import CaseState, Discrepancy
from models.db_models import AuditEvent
from models.enums import DocType, FxStage, ShipmentStatus
from models.schemas import DocumentAmend, DocumentCreate
from scripts import seed_fx_rates, seed_shipments
from services import shipping_service
from services.errors import ShipmentCancelledError
from tests.fixtures.shipments import CLEAN, SEED_SHIPMENTS, make_shipment

AS_OF = date(2026, 9, 28)
S = ShipmentStatus
ON_BOARD_PATH = (S.BOOKED, S.IN_TRANSIT)


class ExplodingModel(FakeListChatModel):
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the model must not be called")


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> None:
    seed_shipments.seed()
    seed_fx_rates.seed()


async def verify(shipment_id: UUID, model: Any = None, as_of: date = AS_OF) -> CaseState:
    return await agent.verify_shipment(
        shipment_id, connector=memory_connector, model_factory=lambda: model, as_of=as_of
    )


def shipment_id(session_factory: sessionmaker[Session], container_id: str) -> UUID:
    with session_factory() as session:
        return shipping_service.get_shipment_by_container_id(session, container_id).id


def create(
    session_factory: sessionmaker[Session],
    documents: Iterable[DocumentCreate] = CLEAN.documents,
    status_path: tuple[ShipmentStatus, ...] = ON_BOARD_PATH,
    **overrides: Any,
) -> UUID:
    """An ad-hoc shipment (the clean fixture unless overridden) with documents and status."""
    with session_factory() as session, session.begin():
        record = shipping_service.create_shipment(session, make_shipment(**overrides), "test")
        for document in documents:
            shipping_service.add_document(session, record.id, document, "test")
        for status in status_path:
            shipping_service.update_status(session, record.id, status, "test")
    return record.id


def doc(doc_type: DocType, **changes: Any) -> DocumentCreate:
    """The clean fixture's document of that type, with changes."""
    base = next(d for d in CLEAN.documents if d.doc_type is doc_type)
    return base.model_copy(update=changes)


def codes(state: CaseState) -> list[str]:
    return [d.code for d in state.document_discrepancies]


def observed(finding: Discrepancy) -> dict[str, str]:
    return {o.source: o.value for o in finding.observed}


def tool_calls(logs: Iterable[Mapping[str, Any]]) -> list[str]:
    return [e["tool"] for e in logs if e["event"] == "agent.tool_call"]


# --------------------------------------------------------------------------- exit criterion


async def test_exactly_the_mismatch_and_missing_document_fixtures_are_flagged(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Exit criterion: all four fixtures — only qty_mismatch and missing_document flagged."""
    states = {
        f.key: await verify(shipment_id(session_factory, f.shipment.container_id))
        for f in SEED_SHIPMENTS
    }

    flagged = {key for key, state in states.items() if state.document_outcome == "flagged"}
    assert flagged == {"qty_mismatch", "missing_document"}
    assert codes(states["qty_mismatch"]) == ["quantity_mismatch"]
    assert codes(states["missing_document"]) == ["missing_document"]
    for key in ("clean", "already_delayed"):
        assert states[key].document_discrepancies == [], key
        assert states[key].document_outcome == "clear"


async def test_quantity_mismatch_names_every_value_and_the_delta(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Field-level, not a boolean: invoice 800 vs packing list 760 (delta 40); no value is
    silently preferred (edge cases, Phase 1)."""
    state = await verify(shipment_id(session_factory, "MSCU7002002"))

    [finding] = state.document_discrepancies
    assert (finding.code, finding.field, finding.delta) == ("quantity_mismatch", "qty", Decimal(40))
    assert observed(finding) == {
        "commercial invoice": "800",
        "packing list": "760",
        "bill of lading": "800",
        "shipment record": "800",
    }
    assert finding.message == (
        "Quantity disagrees: 800 on commercial invoice, bill of lading, shipment record "
        "vs 760 on packing list (delta 40)."
    )
    assert finding.agent == agent.AGENT
    packing_list = next(d for d in state.documents if d.doc_type is DocType.PACKING_LIST)
    assert finding.observed[1].document_id == packing_list.id


async def test_missing_invoice_is_reported_explicitly(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    state = await verify(shipment_id(session_factory, "CMAU7003003"))

    [finding] = state.document_discrepancies
    assert (finding.code, finding.field) == ("missing_document", "doc_type")
    assert finding.message.startswith("Commercial invoice missing")
    assert "attached: packing list, bill of lading" in finding.message
    # no invoice date → no invoice-stage rate asked for; quotation and BoL rates resolved
    assert state.fx_context is not None and state.fx_context.pair == "JPY/INR"
    assert [r.stage for r in state.fx_context.rates] == [FxStage.QUOTATION, FxStage.BOL]


async def test_clean_shipment_carries_its_fx_context(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """The rates the documents convert at, each traceable to its fx row."""
    state = await verify(shipment_id(session_factory, "MSKU7001001"))

    fx = state.fx_context
    assert fx is not None and fx.pair == "USD/INR" and fx.skipped_reason is None
    rates = {r.stage: r.rate for r in fx.rates}
    assert rates == {
        FxStage.QUOTATION: Decimal("87.615"),
        FxStage.BOL: Decimal("87.86"),
        FxStage.INVOICE: Decimal("87.705"),
    }
    # quoted 08-10 for 14 days → valid through 08-24; invoiced 08-18: not expired
    assert (fx.quote_expires, fx.quote_checked_on, fx.quote_expired) == (
        date(2026, 8, 24), date(2026, 8, 18), False,
    )  # fmt: skip
    assert [d.doc_type for d in state.documents] == [
        DocType.INVOICE, DocType.PACKING_LIST, DocType.BILL_OF_LADING,
    ]  # fmt: skip


# --------------------------------------------------------------------------- edge cases, Phase 7


async def test_only_a_packing_list_reports_missing_documents_and_compares_nothing_else(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: only a packing list → missing_document, no quantity comparison against a
    document that does not exist."""
    sid = create(session_factory, documents=[doc(DocType.PACKING_LIST)])

    state = await verify(sid)

    assert codes(state) == ["missing_document", "missing_document"]
    messages = [d.message for d in state.document_discrepancies]
    assert messages[0].startswith("Commercial invoice missing")
    assert messages[1].startswith("Bill of lading missing")


async def test_quantity_check_with_one_document_uses_only_what_exists(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, documents=[doc(DocType.PACKING_LIST, qty=Decimal("1150"))])

    state = await verify(sid)

    [qty] = [d for d in state.document_discrepancies if d.code == "quantity_mismatch"]
    assert observed(qty) == {"packing list": "1150", "shipment record": "1200"}
    assert qty.delta == 50


async def test_amended_document_only_the_latest_version_is_verified(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: the packing list re-issued as v2 with the right quantity → v1 (760) is never
    compared and the shipment is clear."""
    sid = shipment_id(session_factory, "MSCU7002002")
    with session_factory() as session, session.begin():
        [v1] = [d for d in shipping_service.get_documents(session, sid)
                if d.doc_type is DocType.PACKING_LIST]  # fmt: skip
        v2 = shipping_service.amend_document(session, v1.id, DocumentAmend(qty=Decimal("800")),
                                             "ops")  # fmt: skip

    state = await verify(sid)

    assert state.document_outcome == "clear"
    assert v2.id in {d.id for d in state.documents}
    assert v1.id not in {d.id for d in state.documents}


async def test_amended_invoice_v2_is_the_one_compared(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = shipment_id(session_factory, "MSKU7001001")
    with session_factory() as session, session.begin():
        [invoice] = [d for d in shipping_service.get_documents(session, sid)
                     if d.doc_type is DocType.INVOICE]  # fmt: skip
        v2 = shipping_service.amend_document(session, invoice.id,
                                             DocumentAmend(qty=Decimal("1180")), "ops")  # fmt: skip

    state = await verify(sid)

    [qty] = state.document_discrepancies
    [invoice_value] = [o for o in qty.observed if o.source == "commercial invoice"]
    assert (invoice_value.value, invoice_value.version, invoice_value.document_id) == (
        "1180", 2, v2.id,
    )  # fmt: skip


async def test_invalid_currency_is_flagged_and_never_sent_to_fx(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: "XYZ" → invalid_currency, and resolve_rate is never called with it."""
    sid = create(
        session_factory,
        declared_currency="XYZ",
        documents=[doc(DocType.INVOICE, currency="XYZ"), *CLEAN.documents[1:]],
    )

    with structlog.testing.capture_logs() as logs:
        state = await verify(sid)

    [finding] = state.document_discrepancies
    assert (finding.code, finding.field) == ("invalid_currency", "currency")
    assert observed(finding) == {"commercial invoice": "XYZ", "shipment record": "XYZ"}
    assert "not an ISO 4217 currency code" in finding.message
    assert tool_calls(logs) == ["get_active_shipment", "get_documents"]
    assert state.fx_context is not None and state.fx_context.pair is None
    assert state.fx_context.skipped_reason == "no rate resolved: invalid currency XYZ"


async def test_invalid_currency_on_one_document_is_not_also_a_mismatch(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, documents=[doc(DocType.INVOICE, currency="XYZ"),
                                             *CLEAN.documents[1:]])  # fmt: skip

    with structlog.testing.capture_logs() as logs:
        state = await verify(sid)

    assert codes(state) == ["invalid_currency"]
    assert "resolve_rate" not in tool_calls(logs)


# --------------------------------------------------------------------------- edge cases, Phase 1


async def test_cancelled_shipment_stops_the_agent(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: cancelled after the pipeline was triggered → ShipmentCancelledError at read
    time, no analysis, and the stop is on the audit trail."""
    sid = create(session_factory, status_path=(S.BOOKED,))
    with session_factory() as session, session.begin():
        shipping_service.update_status(session, sid, S.CANCELLED, "ops")

    with pytest.raises(ShipmentCancelledError, match="TSTU0000001 is cancelled"):
        await verify(sid)

    with session_factory() as session:
        actions = list(session.scalars(
            select(AuditEvent.action).where(AuditEvent.entity_type == "agent_run")))  # fmt: skip
    assert actions == ["document_verification.stopped"]


async def test_two_current_documents_of_one_type_are_both_reported(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: two documents with conflicting qty → both values and the delta."""
    second_invoice = doc(DocType.INVOICE, qty=Decimal("1190"))
    sid = create(session_factory, documents=[*CLEAN.documents, second_invoice])

    state = await verify(sid)

    [qty] = [d for d in state.document_discrepancies if d.code == "quantity_mismatch"]
    invoices = [o for o in qty.observed if o.source.startswith("commercial invoice")]
    assert sorted(o.value for o in invoices) == ["1190", "1200"]
    assert qty.delta == 10
    assert len({o.source for o in invoices}) == 2  # told apart, not merged


# --------------------------------------------------------------------------- edge cases, Phase 3


async def test_quote_expired_while_still_only_quoted(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: validity 7 days, looked at 10 days later → the caller surfaces "quote
    expired" (not just the resolver's ``expires``)."""
    quoted = AS_OF - timedelta(days=10)
    sid = create(session_factory, documents=[], status_path=(), quote_date=quoted,
                 quote_validity_days=7, bol_date=None, invoice_date=None)  # fmt: skip

    state = await verify(sid)

    [finding] = state.document_discrepancies
    assert finding.code == "quote_expired"
    assert finding.message.startswith("Quote expired: the quotation of 2026-09-18")
    assert "valid through 2026-09-25" in finding.message
    assert finding.delta == 3
    fx = state.fx_context
    assert fx is not None and (fx.quote_expires, fx.quote_checked_on, fx.quote_expired) == (
        date(2026, 9, 25), AS_OF, True,
    )  # fmt: skip


async def test_quote_valid_through_its_last_day(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, documents=[], status_path=(), quote_date=AS_OF - timedelta(7),
                 quote_validity_days=7, bol_date=None, invoice_date=None)  # fmt: skip

    state = await verify(sid)

    assert state.document_outcome == "clear"
    assert state.fx_context is not None and state.fx_context.quote_expired is False


async def test_invoice_issued_after_the_quote_expired(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Quoted 08-10 for 7 days (valid through 08-17), invoiced 08-18."""
    sid = create(session_factory, quote_validity_days=7)

    state = await verify(sid)

    [finding] = state.document_discrepancies
    assert finding.code == "quote_expired"
    assert "the commercial invoice is dated 2026-08-18" in finding.message


async def test_quote_not_judged_after_booking_without_an_invoice(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Booked long after the quote expired, no invoice yet: the booking date is not recorded,
    so the quote window is reported but not judged (no guess)."""
    sid = create(session_factory, documents=[], status_path=(S.BOOKED,),
                 quote_validity_days=7, invoice_date=None)  # fmt: skip

    state = await verify(sid)

    assert state.document_outcome == "clear"
    fx = state.fx_context
    assert fx is not None and fx.quote_expires == date(2026, 8, 17)
    assert (fx.quote_checked_on, fx.quote_expired) == (None, None)


# --------------------------------------------------------------------------- further checks


async def test_missing_rate_is_a_finding_not_a_guess(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """No USD BoL lock on 2026-08-23 → fx_rate_unavailable; the other stages still resolve."""
    sid = create(session_factory, bol_date=date(2026, 8, 23),
                 documents=[*CLEAN.documents[:2],
                            doc(DocType.BILL_OF_LADING, issued_date=date(2026, 8, 23))])  # fmt: skip

    state = await verify(sid)

    [finding] = state.document_discrepancies
    assert (finding.code, finding.field) == ("fx_rate_unavailable", "fx_rate")
    assert finding.message.startswith("No USD/INR rate for the bill of lading date 2026-08-23")
    assert state.fx_context is not None
    assert [r.stage for r in state.fx_context.rates] == [FxStage.QUOTATION, FxStage.INVOICE]


async def test_value_mismatch_in_the_settled_currency(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, declared_value=Decimal("85000.00"))

    state = await verify(sid)

    [finding] = state.document_discrepancies
    assert (finding.code, finding.field, finding.delta) == (
        "value_mismatch",
        "value",
        Decimal(1000),
    )
    assert finding.message == (
        "Value disagrees: 84000 USD on commercial invoice vs 85000 USD on shipment record "
        "(delta 1000 USD)."
    )


async def test_currency_mismatch_skips_value_comparison_and_fx(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, documents=[doc(DocType.INVOICE, currency="EUR",
                                                 value=Decimal("77000")), *CLEAN.documents[1:]])  # fmt: skip

    with structlog.testing.capture_logs() as logs:
        state = await verify(sid)

    [finding] = state.document_discrepancies
    assert finding.code == "currency_mismatch" and finding.delta is None
    assert observed(finding) == {"commercial invoice": "EUR", "shipment record": "USD"}
    assert "resolve_rate" not in tool_calls(logs)
    assert state.fx_context is not None
    assert state.fx_context.skipped_reason == "no rate resolved: documents disagree on the currency"


async def test_document_date_differs_from_the_fx_lock_date(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, documents=[*CLEAN.documents[:2], doc(
        DocType.BILL_OF_LADING, issued_date=date(2026, 8, 24))])  # fmt: skip

    state = await verify(sid)

    [finding] = state.document_discrepancies
    assert (finding.code, finding.field, finding.delta) == (
        "date_mismatch",
        "issued_date",
        Decimal(2),
    )
    assert observed(finding) == {"bill of lading": "2026-08-24",
                                 "shipment record (bol_date)": "2026-08-22"}  # fmt: skip
    assert finding.message == (
        "The bill of lading issued 2026-08-24 does not match the shipment record's bol_date "
        "2026-08-22 (2 day(s) apart); the bill of lading FX rate locks to bol_date."
    )


async def test_documents_are_not_due_before_the_goods_are_on_board(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, documents=[], status_path=(S.BOOKED,))

    state = await verify(sid)

    assert state.document_outcome == "clear"


async def test_inr_shipment_needs_no_conversion(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = create(session_factory, declared_currency="INR",
                 documents=[doc(DocType.INVOICE, currency="INR"), *CLEAN.documents[1:]])  # fmt: skip

    with structlog.testing.capture_logs() as logs:
        state = await verify(sid)

    assert state.document_outcome == "clear"
    assert "resolve_rate" not in tool_calls(logs)
    assert state.fx_context is not None
    assert state.fx_context.skipped_reason == "INR shipment: no conversion needed"


# --------------------------------------------------------------------------- MCP, audit, LLM


async def test_the_agent_reads_through_its_two_servers_only_and_audits_the_run(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = shipment_id(session_factory, "MSCU7002002")
    with session_factory() as session:
        before = len(session.scalars(select(AuditEvent)).all())

    with structlog.testing.capture_logs() as logs:
        state = await verify(sid)

    assert agent.SERVERS == ("shipping_records_mcp", "fx_rates_mcp")
    calls = [e for e in logs if e["event"] == "agent.tool_call"]
    assert {e["server"] for e in calls} == set(agent.SERVERS)
    assert {e["actor"] for e in calls} == {agent.AGENT}
    assert {e["run_id"] for e in calls} == {state.run_id}
    with session_factory() as session:
        events = session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)).all()
    [completed] = events[before:]  # read-only: the run's own event is the only new row
    assert (completed.action, completed.actor) == ("document_verification.completed", agent.AGENT)
    assert completed.entity_id == UUID(state.run_id)
    assert completed.payload["outcome"] == "flagged"
    assert completed.payload["discrepancies"][0]["code"] == "quantity_mismatch"
    assert completed.payload["fx_rates"] == {
        "quotation": "87.730000", "bol": "87.940000", "invoice": "87.780000",
    }  # fmt: skip


async def test_llm_explains_findings_using_only_given_figures(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    text = "The packing list shows 760 units against 800 on the invoice; get it re-issued."
    state = await verify(shipment_id(session_factory, "MSCU7002002"),
                         model=FakeListChatModel(responses=[text]))  # fmt: skip

    assert state.document_explanation is not None
    assert (state.document_explanation.method, state.document_explanation.text) == ("llm", text)


async def test_llm_explanation_with_an_invented_number_falls_back_to_the_template(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    model = FakeListChatModel(responses=["The packing list is 5% short.", "It is 5% short."])

    state = await verify(shipment_id(session_factory, "MSCU7002002"), model=model)

    explanation = state.document_explanation
    assert explanation is not None and explanation.method == "template"
    assert "numbers not in the texts (after one retry)" in (explanation.fallback_reason or "")
    assert explanation.text.startswith("MSCU7002002: 1 discrepancy(ies) to resolve")
    assert "760 on packing list" in explanation.text


async def test_clear_shipment_needs_no_llm_call(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    state = await verify(shipment_id(session_factory, "MSKU7001001"),
                         model=ExplodingModel(responses=[""]))  # fmt: skip

    explanation = state.document_explanation
    assert explanation is not None
    assert (explanation.method, explanation.fallback_reason) == ("template", None)
    assert "nothing to resolve" in explanation.text


async def test_without_an_llm_the_template_explains(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    state = await verify(shipment_id(session_factory, "CMAU7003003"))

    explanation = state.document_explanation
    assert explanation is not None and explanation.method == "template"
    assert explanation.fallback_reason == "no LLM configured"
    assert "Commercial invoice missing" in explanation.text


def test_explanation_prompt_forbids_new_numbers_and_picking_a_side() -> None:
    """A live explanation once told the operator to "correct the invoices to show 600" — the
    majority value. The agent reports disagreements without preferring one; so must the LLM."""
    prompt = agent.EXPLAIN_PROMPT
    assert "do not decide which value is correct" in prompt
    assert "never add a quantity, amount, rate, date or any other number" in prompt
    assert "do not calculate anything" in prompt
