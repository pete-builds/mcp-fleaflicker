"""The ``GET /healthz`` route: upstream reachability as a status code.

Not an MCP tool. An HTTP monitor polls status codes and has no way to call an
MCP tool, so this registers a plain Starlette route on FastMCP's app.

**Unauthenticated by design.** FastMCP wraps only ``/mcp`` in the bearer-auth
middleware; custom routes mount outside it. That is what makes the endpoint
pollable, and it is safe because the body carries no credential material — this
server has no upstream credential at all.

**Deliberately cheap.** The probe reports process liveness and configuration,
and does *not* call Fleaflicker. A healthcheck that hits a third party turns
someone else's outage into a restart loop on this container, and at a 30s
interval it would also be an unpaid 2,880 requests a day against a free public
API. Upstream reachability belongs in a tool call, where a human asked for it.
"""

from __future__ import annotations

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from clients.fleaflicker import FleaflickerClient


def register_health_route(
    mcp: FastMCP, client: FleaflickerClient, *, version: str
) -> None:
    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        """Liveness and configuration. Always 200 while the process is up.

        Returns the configured default league (or null when unset, which is
        legal — every tool accepts a per-call `league_id`).
        """
        return JSONResponse(
            {
                "status": "ok",
                "service": "mcp-fleaflicker",
                "version": version,
                "sport": client.sport,
                "default_league_id": client.default_league_id,
                "upstream": client.base_url,
            },
            status_code=200,
        )
