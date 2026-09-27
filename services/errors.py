"""Typed domain exceptions — one class per failure mode, named ``<Reason>Error``.

Never raise a bare ``Exception`` or ``ValueError`` for a domain failure, so tests can assert on
the exact type (conventions §2). Concrete subclasses are added in the phase that introduces them.
"""


class CustomsCopilotError(Exception):
    """Root of every domain exception, so callers can catch all domain failures in one place."""


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
