"""MCP Fleaflicker — read a Fleaflicker fantasy league, and score it by its own rules.

FastMCP wiring only. Tool bodies live in ``tools/``; API and scoring logic
lives in ``clients/``.

Fleaflicker's read API is public and unauthenticated, so this server holds no
upstream credential and exposes no write surface. The one interesting piece is
``clients/scoring.py``: rather than hardcoding a scoring system, it parses the
league's published rules and evaluates them, so the same code scores a
6-point-passing-TD superflex league and a standard half-PPR league unchanged.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastmcp import FastMCP
from pete_mcp_core import build_auth_provider, configure_logging, run_server
from pete_mcp_core.settings import BaseCoreSettings
from pydantic import AliasChoices, Field, ValidationError

from clients.fleaflicker import DEFAULT_BASE_URL, DEFAULT_SPORT, FleaflickerClient
from clients.redact import install_log_redaction
from tools.health import register_health_route
from tools.league import register_league_tools
from tools.matchup import register_matchup_tools
from tools.scoring import register_scoring_tools
from tools.team import register_team_tools

load_dotenv()

DEFAULT_PORT = 3727
VERSION = "0.1.0"


class FleaflickerSettings(BaseCoreSettings):
    # Optional on purpose. With it set, every tool's league_id is optional;
    # without it, callers pass league_id per call. Both are supported so one
    # deployment can serve a single league conveniently or many leagues
    # explicitly.
    fleaflicker_league_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "FLEAFLICKER_LEAGUE_ID", "MCP_FLEAFLICKER_LEAGUE_ID"
        ),
    )
    fleaflicker_sport: str = Field(
        default=DEFAULT_SPORT,
        validation_alias=AliasChoices("FLEAFLICKER_SPORT", "MCP_FLEAFLICKER_SPORT"),
    )
    fleaflicker_base_url: str = Field(
        default=DEFAULT_BASE_URL,
        validation_alias=AliasChoices(
            "FLEAFLICKER_BASE_URL", "MCP_FLEAFLICKER_BASE_URL"
        ),
    )
    fleaflicker_timeout: float = Field(
        default=30.0,
        validation_alias=AliasChoices("FLEAFLICKER_TIMEOUT", "MCP_FLEAFLICKER_TIMEOUT"),
    )


try:
    settings = FleaflickerSettings()
except ValidationError as exc:
    print(f"FATAL: invalid configuration: {exc}", file=sys.stderr)
    sys.exit(1)

configure_logging(settings.log_level, settings.log_format)
install_log_redaction()

log = logging.getLogger("mcp-fleaflicker")

client = FleaflickerClient(
    league_id=settings.fleaflicker_league_id,
    sport=settings.fleaflicker_sport,
    base_url=settings.fleaflicker_base_url,
    timeout=settings.fleaflicker_timeout,
    user_agent=f"mcp-fleaflicker/{VERSION}",
)

if settings.fleaflicker_league_id is None:
    log.info(
        "No FLEAFLICKER_LEAGUE_ID set; every tool will require an explicit league_id."
    )
else:
    log.info("Default league: %s (%s)", settings.fleaflicker_league_id, client.sport)


@asynccontextmanager
async def lifespan(_app):
    try:
        yield
    finally:
        await client.close()


mcp = FastMCP(
    "Fleaflicker",
    lifespan=lifespan,
    auth=build_auth_provider(
        settings.auth_token,
        client_id="fleaflicker",
        required=settings.auth_required,
        logger=log,
    ),
)

register_league_tools(mcp, client)
register_team_tools(mcp, client)
register_matchup_tools(mcp, client)
register_scoring_tools(mcp, client)
# Plain HTTP, not a tool: an uptime monitor polls status codes and cannot call
# an MCP tool. See tools/health.py for why it never touches the upstream.
register_health_route(mcp, client, version=VERSION)


def main() -> None:
    run_server(
        mcp,
        default_port=DEFAULT_PORT,
        default_transport="streamable-http",
        default_host="0.0.0.0",
    )


if __name__ == "__main__":
    main()
