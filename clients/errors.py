"""Exception hierarchy for the Fleaflicker client.

Every class carries a ``code`` from the Standard Error Contract's fixed enum:
``UPSTREAM_DOWN``, ``AUTH_FAILED``, ``INVALID_INPUT``, ``NOT_FOUND``,
``RATE_LIMITED``, ``INTERNAL``. Adding a class means picking one of those,
never inventing a seventh.
"""

from __future__ import annotations


class FleaflickerError(Exception):
    """Base error. Carries a Standard Error Contract ``code``."""

    code = "INTERNAL"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class FleaflickerInputError(FleaflickerError):
    """Caller-side validation failure. Never reached the API."""

    code = "INVALID_INPUT"


class FleaflickerNotFoundError(FleaflickerError):
    """The league, team, game, or player does not exist."""

    code = "NOT_FOUND"


class FleaflickerRateLimitError(FleaflickerError):
    """Upstream asked us to slow down."""

    code = "RATE_LIMITED"


class FleaflickerAPIError(FleaflickerError):
    """Upstream returned an error response or is unreachable."""

    code = "UPSTREAM_DOWN"
