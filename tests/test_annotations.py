"""Every tool declares itself read-only, and that claim is checked.

Eight tools, all reads against a fantasy league, and not one writes anything
anywhere. That is worth declaring rather than leaving to be inferred: an
unannotated read-only server and an unannotated server full of delete tools are
indistinguishable in the manifest, so a client trying to be careful has to be
careful about everything -- which in practice means being careful about
nothing.
"""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

from clients.fleaflicker import FleaflickerClient
from tools.league import register_league_tools
from tools.matchup import register_matchup_tools
from tools.scoring import register_scoring_tools
from tools.team import register_team_tools

EXPECTED = {
    "get_league_rules", "get_standings", "get_draft_board", "get_roster",
    "search_players", "list_matchups", "get_boxscore", "score_stat_line",
}


@pytest.fixture
async def tools():
    """The live manifest, not the source. What a client would receive.

    Registers onto a bare FastMCP rather than importing the test_tools `app`
    fixture, so this file stays independent of that module's setup and makes
    no HTTP call: the manifest comes from the decorators.
    """
    client = FleaflickerClient(league_id=14153)
    mcp = FastMCP("Fleaflicker-annotations")
    register_league_tools(mcp, client)
    register_team_tools(mcp, client)
    register_matchup_tools(mcp, client)
    register_scoring_tools(mcp, client)
    yield {t.name: t for t in await mcp.list_tools()}
    await client.close()


async def test_the_expected_eight_are_present(tools):
    """Guards the guard: an empty manifest would pass everything below."""
    assert set(tools) == EXPECTED


async def test_every_tool_is_annotated(tools):
    assert sorted(n for n, t in tools.items() if t.annotations is None) == []


async def test_every_tool_is_read_only(tools):
    """The whole surface. A write tool added later fails here first.

    The failure is a prompt to classify the new tool deliberately, not an
    obstacle to adding one.
    """
    assert sorted(n for n, t in tools.items() if not t.annotations.readOnlyHint) == []


async def test_score_stat_line_is_read_only_too(tools):
    """It reads like a computation, but it fetches the league's published rules.

    So it is a read of the same league state as the rest, and pinning it here
    stops someone reclassifying it on the strength of its name.
    """
    assert tools["score_stat_line"].annotations.readOnlyHint is True


async def test_nothing_claims_to_be_destructive(tools):
    assert sorted(n for n, t in tools.items() if t.annotations.destructiveHint) == []


async def test_open_world_and_idempotent_together(tools):
    """An answer can change because a game finished, not because the call did."""
    assert sorted(n for n, t in tools.items() if not t.annotations.openWorldHint) == []
    assert sorted(n for n, t in tools.items() if not t.annotations.idempotentHint) == []
