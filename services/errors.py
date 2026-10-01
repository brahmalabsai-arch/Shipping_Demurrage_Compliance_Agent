"""Typed domain exceptions — one class per failure mode, named ``<Reason>Error``.

Never raise a bare ``Exception`` or ``ValueError`` for a domain failure, so tests can assert on
the exact type (conventions §2). Concrete subclasses are added in the phase that introduces them.
"""


class CustomsCopilotError(Exception):
    """Root of every domain exception, so callers can catch all domain failures in one place."""


class DatabaseUnavailableError(CustomsCopilotError):
    """The database could not be reached or the connection dropped mid-call; safe to retry."""


# --------------------------------------------------------------------------- shipping (Phase 1)


class ShipmentNotFoundError(CustomsCopilotError):
    """No shipment exists with the given id or container_id."""


class DuplicateContainerError(CustomsCopilotError):
    """A shipment with this container_id already exists (surfaced from the unique constraint)."""


class InvalidStatusTransitionError(CustomsCopilotError):
    """The requested status change is not an allowed lifecycle transition."""


class ShipmentCancelledError(CustomsCopilotError):
    """The shipment was cancelled; analysis and edits must stop rather than proceed."""


class InvalidShipmentDataError(CustomsCopilotError):
    """An edit would leave the shipment internally inconsistent (e.g. eta before etd)."""


class DocumentNotFoundError(CustomsCopilotError):
    """No document exists with the given id."""


class DocumentSupersededError(CustomsCopilotError):
    """Only the latest version of a document can be amended."""


# --------------------------------------------------------------------------- demurrage (Phase 2)


class PortRateNotFoundError(CustomsCopilotError):
    """No tariff is configured for this port + container type on the requested date."""


class OverlappingRateError(CustomsCopilotError):
    """An explicit effective_to would overlap the next scheduled tariff period."""


class NegativeStayError(CustomsCopilotError):
    """A demurrage calculation was asked for a negative number of days in port."""


# --------------------------------------------------------------------------- fx (Phase 3)


class RateNotFoundError(CustomsCopilotError):
    """No FX rate satisfies the stage's rule for this pair and date (never a nearest-date guess)."""


class InvalidRateRequestError(CustomsCopilotError):
    """The stage's required context is missing, or context the stage does not use was given."""


class RateAlreadyLockedError(CustomsCopilotError):
    """A rate is already locked for this pair, lock type and date; locked rates are immutable."""


class GazettedRateNotFoundError(CustomsCopilotError):
    """No gazetted rate exists with the given id (e.g. the row a revision claims to revise)."""


class OverlappingGazettedPeriodError(CustomsCopilotError):
    """The period overlaps another original notification, or a sibling revision of the same row."""


class InvalidGazettedRevisionError(CustomsCopilotError):
    """A revision must be for the same pair and lie within the period of the row it revises."""


# --------------------------------------------------------------------------- compliance (Phase 4)


class RuleNotFoundError(CustomsCopilotError):
    """No compliance rule exists with the given id."""


class RuleAlreadyDecidedError(CustomsCopilotError):
    """Only a pending rule can be decided; approvals and rejections are final."""


class StaleProposalError(CustomsCopilotError):
    """A newer version of the rule is already approved, so this older proposal cannot be."""


class DuplicateProposalError(CustomsCopilotError):
    """The proposal is identical to the approved version or to a pending proposal."""


class InvalidDecisionError(CustomsCopilotError):
    """A decision must name the human reviewer who made it."""


class ChangeLogEntryNotFoundError(CustomsCopilotError):
    """No bulletin change_log entry exists with the given id."""


class RuleNotIndexableError(CustomsCopilotError):
    """Only the approved version in force may be embedded into the RAG index (Phase 9)."""


# --------------------------------------------------------------------------- bulletin (Phase 4)


class BulletinNotPublishedError(CustomsCopilotError):
    """The simulated customs bulletin has not been published yet."""


class InvalidBulletinError(CustomsCopilotError):
    """The bulletin text does not follow the section format (see bulletin_service)."""


class BulletinSnapshotNotFoundError(CustomsCopilotError):
    """No scan has captured a bulletin snapshot yet."""


# --------------------------------------------------------------------------- notifications (Phase 5)


class EmailDeliveryError(CustomsCopilotError):
    """The review email could not be sent (backend unavailable, rejected, or not configured)."""


# --------------------------------------------------------------------------- agents (Phase 6)


class McpToolError(CustomsCopilotError):
    """An MCP tool call returned an error; ``error_type`` is the server-side error class name
    (e.g. ``DuplicateProposalError``, ``EmailDeliveryError``, ``ValidationError``)."""

    def __init__(self, tool: str, error_type: str, message: str) -> None:
        super().__init__(f"{tool}: {error_type}: {message}")
        self.tool = tool
        self.error_type = error_type
        self.message = message


# --------------------------------------------------------------------------- governance (Phase 11)


class RuleNotInForceError(CustomsCopilotError):
    """Only the approved version in force of a rule can be revoked."""


class UnknownReviewerError(CustomsCopilotError):
    """The named reviewer is not in the configured reviewer registry (``REVIEWERS``)."""


class ClearanceRequiresApprovalError(CustomsCopilotError):
    """A shipment reaches ``cleared`` only through an approved filing decision
    (``shipping_service.clear_shipment``), never through a plain status change."""


class IncompleteCaseError(CustomsCopilotError):
    """The clearance checklist is not ready (blockers, data gaps, missing inputs or conflicts),
    so the shipment cannot be filed."""


class CaseRunNotFoundError(CustomsCopilotError):
    """No supervised clearance run exists with the given run_id."""


class RunAlreadyOpenError(CustomsCopilotError):
    """The shipment already has an open clearance run; finish or abandon it first."""


class RunAlreadyFinishedError(CustomsCopilotError):
    """The clearance run has reached a terminal state; a new run is needed to try again."""


class RunNotAtGateError(CustomsCopilotError):
    """The run is not waiting at the gate this action is for (or not on that rule)."""


class DecisionPendingError(CustomsCopilotError):
    """The compliance rule the run waits on has not been decided yet."""


class FilingDecisionNotFoundError(CustomsCopilotError):
    """No filing decision exists with the given id."""


class FilingAlreadyDecidedError(CustomsCopilotError):
    """The run's filing gate already has its (final) decision."""


class InvalidFilingDecisionError(CustomsCopilotError):
    """A filing decision must name a reviewer; a rejection must give a reason; an approval must
    be an approval of this shipment's run."""


# --------------------------------------------------------------------------- roi (Phase 12)


class RoiSnapshotNotFoundError(CustomsCopilotError):
    """No ROI snapshot exists with the given id or name."""


class DuplicateSnapshotNameError(CustomsCopilotError):
    """An ROI snapshot with this name already exists (snapshots are never overwritten)."""


class SnapshotIntegrityError(CustomsCopilotError):
    """A stored ROI snapshot no longer matches its recorded SHA-256 — it was altered."""


class InvalidAssumptionsError(CustomsCopilotError):
    """The ROI scenario assumptions are incomplete or invalid (e.g. an issue type has no hold
    duration)."""
