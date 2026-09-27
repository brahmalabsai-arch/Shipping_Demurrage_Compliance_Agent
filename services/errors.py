"""Typed domain exceptions — one class per failure mode, named ``<Reason>Error``.

Never raise a bare ``Exception`` or ``ValueError`` for a domain failure, so tests can assert on
the exact type (conventions §2). Concrete subclasses are added in the phase that introduces them.
"""


class CustomsCopilotError(Exception):
    """Root of every domain exception, so callers can catch all domain failures in one place."""
