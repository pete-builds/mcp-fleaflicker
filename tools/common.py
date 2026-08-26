"""Standard Error Contract helpers shared by every tool module."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pete_mcp_core import format_response

from clients.errors import FleaflickerError

log = logging.getLogger("mcp-fleaflicker.tools")

# --- Tool annotations ---
# Eight tools, all reads against a fantasy league, and not one writes anything
# anywhere. That is worth DECLARING rather than leaving to be inferred: an
# unannotated read-only server and an unannotated server full of delete tools
# are indistinguishable in the manifest, so a client trying to be careful has
# to be careful about everything, which in practice means being careful about
# nothing.
#
# score_stat_line is included deliberately. It reads like a computation rather
# than a lookup, but it fetches the league's published rules to do the sum, so
# it is a read of the same league state as the rest and carries the same
# hints.
#
# openWorldHint is True throughout: every tool reaches Fleaflicker, so an
# answer can change between two identical calls because a game finished, which
# is a different thing from the call having changed something.

#: Reads only. Safe to repeat, safe to call speculatively.
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def ok(data: Any) -> str:
    """Success envelope: ``{"data": ...}``."""
    return format_response({"data": data})


def fail(message: str, code: str, details: dict | None = None) -> str:
    """Failure envelope: ``{"error", "code", "details"}``."""
    payload: dict[str, Any] = {"error": message, "code": code}
    if details:
        payload["details"] = details
    return format_response(payload)


def tool_guard(func: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Turn any escaping exception into the Standard Error Contract.

    ``FleaflickerError`` subclasses carry their own ``code`` and ``details``;
    anything else becomes ``INTERNAL``. No exception ever reaches Claude.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await func(*args, **kwargs)
        except FleaflickerError as exc:
            log.error("tool %s failed (%s): %s", func.__name__, exc.code, exc)
            return fail(str(exc), exc.code, exc.details)
        except Exception as exc:
            log.error("tool %s raised %s: %s", func.__name__, type(exc).__name__, exc)
            return fail(
                f"Unexpected failure in {func.__name__}: {exc}",
                "INTERNAL",
                {"exception": type(exc).__name__},
            )

    return wrapper
