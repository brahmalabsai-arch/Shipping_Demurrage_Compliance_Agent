"""Document Verification agent — cross-checks a shipment's documents against each other and
against the shipment record, and pins down the FX rates they convert at (problem statement FR3;
implementation plan Phase 7; edge cases, Phases 1, 3 and 7).

Through MCP only (``shipping_records_mcp``, ``fx_rates_mcp``), for one shipment:

1. ``get_active_shipment`` — a cancelled shipment stops the run with ``ShipmentCancelledError``
   (a pipeline that started before the cancellation does no work on it).
2. ``get_documents`` — current versions only, so an amended document's superseded versions are
   never compared.
3. Deterministic cross-checks, each a field-level :class:`~agents.state.Discrepancy` listing
   every value seen, where it was read, and the delta — never a boolean, never one value
   silently preferred over another:

   * ``missing_document`` — once the goods are on board (``in_transit`` and later) the
     commercial invoice, packing list and bill of lading must all be attached; before that they
     are not yet due. Nothing is ever compared against a missing document.
   * ``quantity_mismatch`` — qty on the invoice, packing list and bill of lading, and the
     shipment record's declared qty.
   * ``invalid_currency`` — a currency that is not ISO 4217 (e.g. ``XYZ``); it is never sent to
     FX resolution. ``currency_mismatch`` — valid but different currencies. ``value_mismatch`` —
     invoice value vs declared value, compared only once the currency is settled.
   * ``date_mismatch`` — the invoice / bill of lading issue date vs the shipment's
     ``invoice_date`` / ``bol_date``, the dates the FX rates lock to.

4. ``resolve_rate`` for ``<currency>/INR`` at each lifecycle date the shipment has: quotation
   (spot + validity window), bill of lading, commercial invoice. A missing rate is
   ``fx_rate_unavailable`` (raise, don't guess). ``quote_expired``: a quote must still be valid
   when it is acted on — on the commercial invoice date, or, for a shipment that is still only
   quoted, on the analysis date (``fx_service.is_quote_expired``: valid *through* ``expires``).

The checks are deterministic; the LLM (summary tier) only explains the findings in plain
language, behind the shared number guard (:mod:`agents.guarded_llm`), with a template fallback —
and is not called at all when there is nothing to explain. The agent changes no domain data;
its outcome is an ``agent_run`` audit event (``document_verification.completed``, or
``.stopped`` for a cancelled shipment).

State slice: reads ``CaseState.run_id``, ``shipment_id``, ``as_of``; writes ``shipment``,
``documents``, ``fx_context``, ``document_discrepancies``, ``document_outcome`` and
``document_explanation``.

Run by hand: ``python -m scripts.verify_documents --all`` (MCP servers as stdio subprocesses);
on the dashboard: Shipping Ops → a shipment → "Verify documents".
"""

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import TypeAdapter

from agents import llm
from agents.audit import record_run_event
from agents.guarded_llm import guarded_reply
from agents.mcp_clients import Connector, McpToolbox, open_toolbox, stdio_connector
from agents.state import (
    CaseState,
    Discrepancy,
    DiscrepancyCode,
    Explanation,
    FxContext,
    ObservedValue,
)
from models.currencies import is_iso_currency
from models.enums import DocType, FxStage, InvoiceKind, ShipmentStatus
from models.schemas import DocumentRecord, ResolvedRate, ShipmentRecord
from services.errors import McpToolError, ShipmentCancelledError
from services.fx_service import is_quote_expired

AGENT = "document_verification_agent"
SHIPPING, FX = "shipping_records_mcp", "fx_rates_mcp"
SERVERS = (SHIPPING, FX)

HOME_CURRENCY = "INR"
RECORD = "shipment record"
IST = ZoneInfo("Asia/Kolkata")

CORE_DOCUMENTS = (DocType.INVOICE, DocType.PACKING_LIST, DocType.BILL_OF_LADING)
LABELS: dict[DocType, str] = {
    DocType.INVOICE: "commercial invoice",
    DocType.PACKING_LIST: "packing list",
    DocType.BILL_OF_LADING: "bill of lading",
    DocType.CERTIFICATE: "certificate",
    DocType.OTHER: "other document",
}
# The goods are on board: export clearance at origin has needed all three core documents.
ON_BOARD = frozenset(
    {
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.DOCKED,
        ShipmentStatus.UNDER_REVIEW,
        ShipmentStatus.HELD,
        ShipmentStatus.CLEARED,
        ShipmentStatus.CLOSED,
    }
)

EXPLAIN_PROMPT = (
    "You brief the import operations team of an Indian auto-parts importer on document "
    "problems found for one shipment before customs clearance. You get the shipment and a list "
    "of verified findings. In 2-4 plain-English sentences say what is wrong and which document "
    "or correction is needed to resolve it. Where documents disagree, do not decide which value "
    "is correct — say which documents must be reconciled. Use only the facts given: never add a "
    "quantity, amount, rate, date or any other number that is not in them, and do not calculate "
    "anything. No preamble, no markdown."
)

logger = structlog.get_logger("agents")


@dataclass(frozen=True)
class VerificationDeps:
    """What the node needs besides state: MCP access and the (optional) explanation model."""

    toolbox: McpToolbox
    model: BaseChatModel | None


def today_ist() -> date:
    """The business calendar day now (IST) — the default analysis date of a live run."""
    return datetime.now(IST).date()


# --------------------------------------------------------------------------- cross-checks


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _found(
    code: DiscrepancyCode,
    field: str,
    message: str,
    observed: Iterable[ObservedValue] = (),
    delta: Decimal | None = None,
) -> Discrepancy:
    return Discrepancy(code=code, field=field, message=message, observed=list(observed),
                       delta=delta, agent=AGENT)  # fmt: skip


def _labels(documents: list[DocumentRecord]) -> dict[UUID, str]:
    """Readable source names; documents of a type that appears twice are numbered."""
    counts = Counter(d.doc_type for d in documents)
    seen: Counter[DocType] = Counter()
    labels = {}
    for d in documents:
        seen[d.doc_type] += 1
        label = LABELS[d.doc_type]
        if counts[d.doc_type] > 1:
            label += f" #{seen[d.doc_type]} (issued {d.issued_date})"
        labels[d.id] = label
    return labels


def _seen(document: DocumentRecord, labels: dict[UUID, str], value: str) -> ObservedValue:
    return ObservedValue(source=labels[document.id], value=value, document_id=document.id,
                         version=document.version)  # fmt: skip


def _disagreement(
    code: DiscrepancyCode,
    field: str,
    title: str,
    observed: list[ObservedValue],
    *,
    numeric: bool,
    unit: str = "",
) -> Discrepancy | None:
    """One finding listing every distinct value with its sources, or None if all agree."""
    groups: dict[str, list[str]] = {}
    for o in observed:
        groups.setdefault(o.value, []).append(o.source)
    if len(groups) < 2:
        return None
    suffix = f" {unit}" if unit else ""
    parts = [f"{value}{suffix} on {', '.join(sources)}" for value, sources in groups.items()]
    message = f"{title} disagrees: {' vs '.join(parts)}"
    delta = None
    if numeric:
        amounts = [Decimal(v) for v in groups]
        delta = max(amounts) - min(amounts)
        message += f" (delta {_fmt(delta)}{suffix})"
    return _found(code, field, message + ".", observed, delta)


@dataclass(frozen=True)
class _CrossCheck:
    findings: list[Discrepancy]
    currency: str | None  # the one valid currency everything agrees on, else None
    currency_problem: str | None  # why there is no such currency


def _missing_documents(shipment: ShipmentRecord, docs: list[DocumentRecord]) -> list[Discrepancy]:
    if shipment.status not in ON_BOARD:
        return []  # not due yet: documents are still being prepared before loading
    present = {d.doc_type for d in docs}
    attached = ", ".join(LABELS[t] for t in CORE_DOCUMENTS if t in present) or "none"
    return [
        _found(
            "missing_document",
            "doc_type",
            f"{LABELS[t].capitalize()} missing: required once the goods are on board "
            f"(status {shipment.status.value}; attached: {attached}).",
            [ObservedValue(source=RECORD, value=shipment.status.value)],
        )
        for t in CORE_DOCUMENTS
        if t not in present
    ]


def _currencies(
    shipment: ShipmentRecord, docs: list[DocumentRecord], labels: dict[UUID, str]
) -> tuple[list[Discrepancy], str | None, str | None]:
    observed = [_seen(d, labels, d.currency) for d in docs if d.currency]
    observed.append(ObservedValue(source=RECORD, value=shipment.declared_currency))
    findings = []
    invalid: dict[str, list[ObservedValue]] = {}
    for o in observed:
        if not is_iso_currency(o.value):
            invalid.setdefault(o.value, []).append(o)
    for code, where in invalid.items():
        sources = ", ".join(o.source for o in where)
        findings.append(_found(
            "invalid_currency", "currency",
            f"Currency '{code}' on {sources} is not an ISO 4217 currency code; nothing in it can "
            "be converted until it is corrected.",
            where,
        ))  # fmt: skip
    valid = [o for o in observed if is_iso_currency(o.value)]
    mismatch = _disagreement("currency_mismatch", "currency", "Currency", valid, numeric=False)
    if mismatch is not None:
        findings.append(mismatch)
    if invalid:
        return findings, None, f"invalid currency {', '.join(sorted(invalid))}"
    if mismatch is not None:
        return findings, None, "documents disagree on the currency"
    return findings, shipment.declared_currency, None


def _dates(
    shipment: ShipmentRecord, docs: list[DocumentRecord], labels: dict[UUID, str]
) -> list[Discrepancy]:
    findings = []
    for doc_type, expected, field, stage in (
        (DocType.INVOICE, shipment.invoice_date, "invoice_date", "commercial invoice"),
        (DocType.BILL_OF_LADING, shipment.bol_date, "bol_date", "bill of lading"),
    ):
        for d in docs:
            if d.doc_type is not doc_type or d.issued_date == expected:
                continue
            # The issue date identifies the document here, even when its type appears twice.
            issued = f"The {LABELS[doc_type]} issued {d.issued_date}"
            record = ObservedValue(source=f"{RECORD} ({field})",
                                   value="not set" if expected is None else expected.isoformat())  # fmt: skip
            if expected is None:
                message = (
                    f"{issued} has no matching {field} on the shipment record, so the {stage} FX "
                    "rate cannot be locked."
                )
                delta = None
            else:
                days = abs((d.issued_date - expected).days)
                message = (
                    f"{issued} does not match the shipment record's {field} {expected} ({days} "
                    f"day(s) apart); the {stage} FX rate locks to {field}."
                )
                delta = Decimal(days)
            findings.append(_found("date_mismatch", "issued_date", message,
                                   [_seen(d, labels, d.issued_date.isoformat()), record], delta))  # fmt: skip
    return findings


def cross_check(shipment: ShipmentRecord, documents: list[DocumentRecord]) -> _CrossCheck:
    """Every document check that needs no FX data (deterministic, no I/O)."""
    order = {t: i for i, t in enumerate(DocType)}
    docs = sorted(documents, key=lambda d: (order[d.doc_type], d.issued_date, d.version))
    labels = _labels(docs)
    core = [d for d in docs if d.doc_type in CORE_DOCUMENTS]
    findings = _missing_documents(shipment, docs)

    quantities = [_seen(d, labels, _fmt(d.qty)) for d in core if d.qty is not None]
    quantities.append(ObservedValue(source=RECORD, value=_fmt(shipment.declared_qty)))
    if (qty := _disagreement("quantity_mismatch", "qty", "Quantity", quantities,
                             numeric=True)) is not None:  # fmt: skip
        findings.append(qty)

    currency_findings, currency, problem = _currencies(shipment, docs, labels)
    findings += currency_findings
    if currency is not None:  # values in different or unknown currencies are not comparable
        values = [_seen(d, labels, _fmt(d.value)) for d in docs
                  if d.doc_type is DocType.INVOICE and d.value is not None]  # fmt: skip
        values.append(ObservedValue(source=RECORD, value=_fmt(shipment.declared_value)))
        if (value := _disagreement("value_mismatch", "value", "Value", values, numeric=True,
                                   unit=currency)) is not None:  # fmt: skip
            findings.append(value)

    findings += _dates(shipment, docs, labels)
    return _CrossCheck(findings, currency, problem)


# --------------------------------------------------------------------------- FX context


async def _fx_context(
    tools: McpToolbox, shipment: ShipmentRecord, check: _CrossCheck, as_of: date
) -> tuple[FxContext, list[Discrepancy]]:
    """Resolve the rates for each lifecycle date the shipment has; check the quote window."""
    if check.currency is None:
        return FxContext(skipped_reason=f"no rate resolved: {check.currency_problem}"), []
    if check.currency == HOME_CURRENCY:
        return FxContext(skipped_reason="INR shipment: no conversion needed"), []
    pair = f"{check.currency}/{HOME_CURRENCY}"

    requests: list[tuple[str, dict[str, Any]]] = []
    if shipment.quote_date is not None and shipment.quote_validity_days is not None:
        requests.append((f"quotation date {shipment.quote_date}", {
            "as_of": shipment.quote_date, "stage": FxStage.QUOTATION,
            "quote_validity_days": shipment.quote_validity_days}))  # fmt: skip
    if shipment.bol_date is not None:
        requests.append((f"bill of lading date {shipment.bol_date}",
                         {"as_of": shipment.bol_date, "stage": FxStage.BOL}))  # fmt: skip
    if shipment.invoice_date is not None:
        requests.append((f"commercial invoice date {shipment.invoice_date}", {
            "as_of": shipment.invoice_date, "stage": FxStage.INVOICE,
            "invoice_kind": InvoiceKind.COMMERCIAL}))  # fmt: skip

    rates: list[ResolvedRate] = []
    findings: list[Discrepancy] = []
    for when, arguments in requests:
        try:
            raw = await tools.call(FX, "resolve_rate", {"pair": pair, **arguments})
        except McpToolError as exc:
            if exc.error_type != "RateNotFoundError":
                raise
            findings.append(_found(
                "fx_rate_unavailable", "fx_rate",
                f"No {pair} rate for the {when} ({arguments['stage'].value} stage): {exc.message}",
                [ObservedValue(source=RECORD, value=arguments["as_of"].isoformat())],
            ))  # fmt: skip
            continue
        rates.append(ResolvedRate.model_validate(raw))

    context = FxContext(pair=pair, rates=rates)
    quote = next((r for r in rates if r.stage is FxStage.QUOTATION), None)
    if quote is None:
        return context, findings
    if shipment.invoice_date is not None:
        checked_on, acted = shipment.invoice_date, "the commercial invoice is dated"
    elif shipment.status is ShipmentStatus.QUOTED:
        checked_on, acted = as_of, "the shipment is still only quoted on"
    else:
        return context.model_copy(update={"quote_expires": quote.expires}), findings
    expired = is_quote_expired(quote, checked_on)
    context = context.model_copy(update={"quote_expires": quote.expires,
                                         "quote_checked_on": checked_on, "quote_expired": expired})  # fmt: skip
    if expired:
        findings.append(_found(
            "quote_expired", "quote",
            f"Quote expired: the quotation of {shipment.quote_date} ({pair} {_fmt(quote.rate)}, "
            f"valid {shipment.quote_validity_days} days) was valid through {quote.expires}, but "
            f"{acted} {checked_on}. Re-quote before relying on its price and rate.",
            [ObservedValue(source=f"{RECORD} (quote_expiry)", value=str(quote.expires)),
             ObservedValue(source=f"{RECORD} (checked on)", value=checked_on.isoformat())],
            Decimal((checked_on - quote.expires).days) if quote.expires else None,
        ))  # fmt: skip
    return context, findings


# --------------------------------------------------------------------------- explanation


def template_explanation(shipment: ShipmentRecord, findings: list[Discrepancy]) -> str:
    """Deterministic explanation of the findings (the LLM fallback)."""
    if not findings:
        return (
            f"{shipment.container_id}: the documents agree with each other and with the shipment "
            "record; nothing to resolve."
        )
    return (
        f"{shipment.container_id}: {len(findings)} discrepancy(ies) to resolve before clearance. "
        + " ".join(f.message for f in findings)
    )


async def explain(
    shipment: ShipmentRecord, findings: list[Discrepancy], model: BaseChatModel | None
) -> Explanation:
    """LLM explanation of the findings if a model is configured and it invents no numbers;
    the template otherwise. A clean shipment needs no LLM call."""
    text = template_explanation(shipment, findings)
    if not findings:
        return Explanation(text=text, method="template")
    if model is None:
        return Explanation(text=text, method="template", fallback_reason="no LLM configured")
    prompt = (
        f"Shipment {shipment.container_id}: {shipment.cargo_type}, HS {shipment.hs_code}, "
        f"{shipment.origin_port} to {shipment.destination_port}, status {shipment.status.value}."
        "\nFindings:\n" + "\n".join(f"- {f.message}" for f in findings)
    )
    reply = await guarded_reply(model, EXPLAIN_PROMPT, prompt, prompt)
    if reply.text is None:
        return Explanation(text=text, method="template", fallback_reason=reply.fallback_reason)
    return Explanation(text=reply.text, method="llm")


# --------------------------------------------------------------------------- the node


async def document_verification_agent(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    """LangGraph node: read the active shipment and its current documents → cross-check →
    resolve FX per lifecycle date → explain."""
    deps: VerificationDeps = config["configurable"]["deps"]
    tools = deps.toolbox
    try:
        raw = await tools.call(SHIPPING, "get_active_shipment", {"shipment_id": state.shipment_id})
    except McpToolError as exc:
        if exc.error_type != "ShipmentCancelledError":
            raise
        record_run_event(AGENT, state.run_id, "document_verification.stopped",
                         {"shipment_id": str(state.shipment_id), "reason": exc.message})  # fmt: skip
        raise ShipmentCancelledError(exc.message) from exc
    shipment = ShipmentRecord.model_validate(raw)
    documents = TypeAdapter(list[DocumentRecord]).validate_python(
        await tools.call(SHIPPING, "get_documents", {"shipment_id": state.shipment_id})
    )

    check = cross_check(shipment, documents)
    fx_context, fx_findings = await _fx_context(tools, shipment, check, state.as_of)
    findings = [*check.findings, *fx_findings]
    explanation = await explain(shipment, findings, deps.model)
    outcome = "flagged" if findings else "clear"

    record_run_event(AGENT, state.run_id, "document_verification.completed", {
        "shipment_id": str(shipment.id),
        "container_id": shipment.container_id,
        "as_of": state.as_of.isoformat(),
        "outcome": outcome,
        "discrepancies": [{"code": f.code, "field": f.field, "message": f.message}
                          for f in findings],
        "documents": [f"{d.doc_type.value} v{d.version}" for d in documents],
        "fx_pair": fx_context.pair,
        "fx_rates": {r.stage.value: str(r.rate) for r in fx_context.rates},
        "fx_skipped": fx_context.skipped_reason,
        "explanation_method": explanation.method,
        "explanation_fallback": explanation.fallback_reason,
    })  # fmt: skip
    logger.info("agent.run_completed", agent=AGENT, run_id=state.run_id,
                container_id=shipment.container_id, outcome=outcome, discrepancies=len(findings))  # fmt: skip
    return {
        "shipment": shipment,
        "documents": documents,
        "fx_context": fx_context,
        "document_discrepancies": findings,
        "document_outcome": outcome,
        "document_explanation": explanation,
    }


def build_graph() -> CompiledStateGraph[CaseState, Any, CaseState, CaseState]:
    """This agent alone; the Phase 11 supervisor wires it into the shipment pipeline."""
    graph = StateGraph(CaseState)
    graph.add_node("document_verification_agent", document_verification_agent)
    graph.add_edge(START, "document_verification_agent")
    graph.add_edge("document_verification_agent", END)
    return graph.compile()


async def run(deps: VerificationDeps, shipment_id: UUID, *, as_of: date, run_id: str) -> CaseState:
    """Verify one shipment and return the final state."""
    result = await build_graph().ainvoke(
        CaseState(run_id=run_id, shipment_id=shipment_id, as_of=as_of),
        config={"configurable": {"deps": deps}},
    )
    return CaseState.model_validate(result)


async def verify_shipment(
    shipment_id: UUID,
    *,
    connector: Connector | None = None,
    model_factory: Callable[[], BaseChatModel | None] | None = None,
    as_of: date | None = None,
    run_id: str | None = None,
) -> CaseState:
    """Connect to this agent's MCP servers and verify one shipment.

    Defaults: servers as stdio subprocesses; the configured summary model (``None`` without an
    API key → template explanation); today's IST date as the analysis date.
    """
    run_id = run_id or str(uuid4())
    async with open_toolbox(
        SERVERS, actor=AGENT, run_id=run_id, connector=connector or stdio_connector
    ) as toolbox:
        deps = VerificationDeps(toolbox=toolbox, model=(model_factory or llm.summary_model)())
        return await run(deps, shipment_id, as_of=as_of or today_ist(), run_id=run_id)
