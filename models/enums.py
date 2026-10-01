"""Domain enumerations shared by the ORM (Postgres ENUM types) and the Pydantic schemas."""

from enum import StrEnum


class ShipmentStatus(StrEnum):
    """Shipment lifecycle. ``quoted`` precedes ``booked`` (problem statement §5).

    ``cancelled`` is terminal; it exists so an in-flight agent pipeline can detect a
    cancellation at read time and stop (edge cases, Phase 1).
    """

    QUOTED = "quoted"
    BOOKED = "booked"
    IN_TRANSIT = "in_transit"
    DOCKED = "docked"
    UNDER_REVIEW = "under_review"
    CLEARED = "cleared"
    HELD = "held"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ContainerType(StrEnum):
    """ISO container sizes/types that port tariffs price separately (stored as TEXT)."""

    DRY_20 = "20ft"
    DRY_40 = "40ft"
    HIGH_CUBE_40 = "40hc"
    REEFER_20 = "20rf"
    REEFER_40 = "40rf"


class DocType(StrEnum):
    """Kinds of shipment document that verification compares against each other."""

    INVOICE = "invoice"
    PACKING_LIST = "packing_list"
    BILL_OF_LADING = "bill_of_lading"
    CERTIFICATE = "certificate"
    OTHER = "other"


class FxStage(StrEnum):
    """Shipment lifecycle stage an FX rate is asked for; each has its own rule (arch §8)."""

    QUOTATION = "quotation"
    BOL = "bol"
    INVOICE = "invoice"
    PORT_CHARGES = "port_charges"


class LockType(StrEnum):
    """Event that locks a historical FX rate (``fx.locked_rates.lock_type``)."""

    BOL = "bol"
    COMMERCIAL_INVOICE = "commercial_invoice"


class InvoiceKind(StrEnum):
    """Invoice-stage variants: a commercial invoice locks to its own date, a freight invoice
    re-uses the rate locked on the bill of lading date."""

    COMMERCIAL = "commercial"
    FREIGHT = "freight"


class RateSource(StrEnum):
    """Which ``fx.*`` table a resolved rate came from."""

    SPOT = "spot"
    LOCKED = "locked"
    GAZETTED = "gazetted"


class RuleStatus(StrEnum):
    """Compliance rule review state. Every rule is created ``pending``; only an explicit human
    decision (``compliance_service.decide``) moves it, once, to ``approved`` or ``rejected``.
    An approved version in force can later be ``revoked`` (``compliance_service.revoke_decision``,
    Phase 11) — a separate, final act that leaves the original decision fields untouched."""

    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    REVOKED = "revoked"


class RuleDecision(StrEnum):
    """A reviewer's verdict on a pending compliance rule."""

    APPROVE = "approve"
    REJECT = "reject"


class SectionChangeKind(StrEnum):
    """How one rule section of the customs bulletin changed between two snapshots."""

    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"


class RunState(StrEnum):
    """Where a supervised clearance run stands (``governance.case_runs``, Phase 11).

    ``running`` and the two ``awaiting_*`` gates are open; every other state is terminal — a new
    run (new ``run_id``) is needed to try again. ``filing_rejected`` is a human's final "no", never
    re-queued as an approval.
    """

    RUNNING = "running"
    AWAITING_COMPLIANCE = (
        "awaiting_compliance"  # a pending rule covering the goods needs a decision
    )
    AWAITING_FILING = "awaiting_filing"  # the checklist is ready; a reviewer must approve filing
    FILED = "filed"  # approved and the shipment moved to `cleared`
    FILING_REJECTED = "filing_rejected"  # a reviewer refused the filing
    BLOCKED = "blocked"  # the checklist is not ready for clearance (blockers, gaps, conflicts)
    NOT_FILEABLE = "not_fileable"  # the shipment is not at a stage that can be filed
    STOPPED = "stopped"  # the shipment was cancelled
    FAILED = "failed"  # the run raised; see its detail and the audit trail

    @property
    def is_open(self) -> bool:
        return self in OPEN_RUN_STATES


OPEN_RUN_STATES = frozenset(
    {RunState.RUNNING, RunState.AWAITING_COMPLIANCE, RunState.AWAITING_FILING}
)
