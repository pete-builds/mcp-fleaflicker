"""Health check script for Docker HEALTHCHECK.

Wraps ``pete_mcp_core.healthcheck.check`` so the probe targets ``/healthz``,
never ``/mcp``.

A bare ``GET /mcp`` makes the MCP SDK create a transport session *before*
returning 406, and nothing reaps it — roughly 40 KB leaked per request,
measured. At a 30s interval that is 2,880 probes a day, about 115 MiB/day of
permanently leaked memory on a server meant to run unattended for years. The
HTTP verb is not a workaround: the session is created before method dispatch,
so HEAD and OPTIONS leak slightly worse than GET. Only staying off the ``/mcp``
mount avoids it. ``/healthz`` is a plain custom route with no session cost.

The probe never carries a token. HTTP now always runs with bearer auth, and
``/healthz`` still answers 200 because custom routes mount outside the auth
middleware (pinned by ``tests/test_auth_required.py``). 401 is treated as
alive regardless, so that if a later FastMCP moves custom routes behind auth
the container is not marked unhealthy for being correctly locked. The rest of
the set is pete-mcp-core's ``DEFAULT_HEALTHY_CODES``, which since ``18f2261``
is 200, 204, 400, 401, 404, 405, 406 and 503: "the process answered". 500
stays out: a real fault must still fail.

Env precedence for the port matches ``pete_mcp_core.serve`` exactly
(``FASTMCP_PORT`` > ``MCP_PORT`` > default) so the probe can never target a
different port than the server.
"""

from __future__ import annotations

import os
import sys

from pete_mcp_core.healthcheck import DEFAULT_HEALTHY_CODES, check

DEFAULT_PORT = 3727

# "Responds at all" == alive. See the module docstring before narrowing this.
HEALTHY_CODES = frozenset(DEFAULT_HEALTHY_CODES | {401})


def main(default_port: int = DEFAULT_PORT) -> int:
    port_str = os.getenv("FASTMCP_PORT") or os.getenv("MCP_PORT") or str(default_port)
    path = os.getenv("MCP_HEALTH_PATH", "/healthz")
    try:
        port = int(port_str)
    except ValueError:
        return 1
    return check(port, path=path, healthy_codes=HEALTHY_CODES)


if __name__ == "__main__":
    sys.exit(main())
