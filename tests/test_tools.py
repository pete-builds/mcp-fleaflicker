"""Tool contract tests.

These pin the wire contract every caller depends on: the tool set, the
Standard Error Contract envelope, and the promise that no exception ever
escapes a tool. They drive the real FastMCP instance and mock only the HTTP
layer, so the decorators, argument coercion, and serialisation are all
exercised rather than bypassed.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastmcp import FastMCP

from clients.fleaflicker import DEFAULT_BASE_URL, FleaflickerClient
from tests.conftest import load_fixture
from tools.league import register_league_tools
from tools.matchup import register_matchup_tools
from tools.scoring import register_scoring_tools
from tools.team import register_team_tools

EXPECTED_TOOLS = {
    "get_boxscore",
    "get_draft_board",
    "get_league_rules",
    "get_roster",
    "get_standings",
    "list_matchups",
    "score_stat_line",
    "search_players",
}


@pytest.fixture
async def app():
    client = FleaflickerClient(league_id=14153)
    mcp = FastMCP("Fleaflicker-test")
    register_league_tools(mcp, client)
    register_team_tools(mcp, client)
    register_matchup_tools(mcp, client)
    register_scoring_tools(mcp, client)
    yield mcp, client
    await client.close()


async def call(mcp: FastMCP, tool: str, /, **kwargs) -> dict:
    """Invoke a tool and parse its JSON envelope.

    Positional-only params: `name` is itself a `search_players` argument, and a
    keyword collision here silently shadows the tool's own parameter.
    """
    result = await mcp.call_tool(tool, kwargs or {})
    blocks = result.content if hasattr(result, "content") else result
    return json.loads(blocks[0].text)


def route(method: str):
    return respx.get(f"{DEFAULT_BASE_URL}/{method}")


def exhausted_page() -> dict:
    """The player listing with its paging cursor cleared.

    The captured fixture carries a `resultOffsetNext`, so replaying it at every
    offset would model an upstream that never ends. Clearing the cursor models
    the last page, which is what a small result set actually returns.
    """
    page = load_fixture("player_listing")
    page.pop("resultOffsetNext", None)
    return page


# --- the tool set --------------------------------------------------------


async def test_exactly_the_expected_tools_are_registered(app):
    mcp, _ = app
    names = {t.name for t in await mcp.list_tools()}
    assert names == EXPECTED_TOOLS


async def test_every_tool_documents_itself(app):
    """Docstrings are the ACI. A tool without one is unusable by a model."""
    mcp, _ = app
    for tool in await mcp.list_tools():
        assert tool.description, f"{tool.name} has no description"
        # FastMCP sends the prose above `Args:` as the description and folds
        # `Args:` into the parameter schema, so the return shape has to live in
        # the prose to reach the model at all.
        assert "Returns JSON with" in tool.description, (
            f"{tool.name} does not document its return shape where a model sees it"
        )
        assert "Example:" in tool.description, f"{tool.name} has no example"
        assert "Idempotent:" in tool.description, f"{tool.name} omits idempotency"


async def test_tool_count_is_within_the_soft_cap(app):
    mcp, _ = app
    assert len(await mcp.list_tools()) <= 10


# --- success envelope ----------------------------------------------------


@respx.mock
async def test_get_league_rules_returns_data_envelope(app):
    mcp, _ = app
    route("FetchLeagueRules").mock(
        return_value=httpx.Response(200, json=load_fixture("league_rules"))
    )
    payload = await call(mcp, "get_league_rules")
    assert set(payload) == {"data"}
    assert payload["data"]["roster"]["starters"] == 8
    assert payload["data"]["scoring"]["rule_count"] == 46


@respx.mock
async def test_get_standings_returns_data_envelope(app):
    mcp, _ = app
    route("FetchLeagueStandings").mock(
        return_value=httpx.Response(200, json=load_fixture("standings"))
    )
    payload = await call(mcp, "get_standings", season=2025)
    assert payload["data"]["teams"][0]["rank"] == 1


@respx.mock
async def test_list_matchups_returns_data_envelope(app):
    mcp, _ = app
    route("FetchLeagueScoreboard").mock(
        return_value=httpx.Response(200, json=load_fixture("scoreboard"))
    )
    payload = await call(mcp, "list_matchups", week=1)
    assert payload["data"]["games"]


@respx.mock
async def test_get_boxscore_returns_data_envelope(app):
    mcp, _ = app
    route("FetchLeagueBoxscore").mock(
        return_value=httpx.Response(200, json=load_fixture("boxscore"))
    )
    payload = await call(mcp, "get_boxscore", fantasy_game_id=56361068)
    assert payload["data"]["home_total"] == pytest.approx(123.4)


@respx.mock
async def test_get_roster_returns_data_envelope(app):
    mcp, _ = app
    route("FetchRoster").mock(
        return_value=httpx.Response(200, json=load_fixture("roster"))
    )
    payload = await call(mcp, "get_roster", team_id=1255738)
    assert payload["data"]["slots"]


@respx.mock
async def test_get_draft_board_returns_data_envelope(app):
    mcp, _ = app
    route("FetchLeagueDraftBoard").mock(
        return_value=httpx.Response(200, json=load_fixture("draft_board"))
    )
    payload = await call(mcp, "get_draft_board", season=2025)
    assert payload["data"]["rounds"][0]["picks"][0]["overall"] == 1


# --- failure envelope ----------------------------------------------------


@respx.mock
async def test_upstream_down_returns_the_error_contract(app):
    mcp, _ = app
    route("FetchLeagueStandings").mock(return_value=httpx.Response(503, text="down"))
    payload = await call(mcp, "get_standings")
    assert set(payload) >= {"error", "code"}
    assert payload["code"] == "UPSTREAM_DOWN"
    assert "data" not in payload


@respx.mock
async def test_not_found_returns_the_error_contract(app):
    mcp, _ = app
    route("FetchLeagueRules").mock(
        return_value=httpx.Response(200, json={"error": {"message": "League not found."}})
    )
    payload = await call(mcp, "get_league_rules", league_id=999999)
    assert payload["code"] == "NOT_FOUND"


async def test_invalid_input_returns_the_error_contract(app):
    mcp, _ = app
    payload = await call(mcp, "search_players", limit=9999)
    assert payload["code"] == "INVALID_INPUT"
    assert "limit" in payload["error"]


@respx.mock
async def test_no_exception_escapes_a_tool(app):
    """Even an upstream returning nonsense yields an envelope, never a raise."""
    mcp, _ = app
    route("FetchLeagueStandings").mock(return_value=httpx.Response(200, text="<html>"))
    payload = await call(mcp, "get_standings")
    assert "code" in payload


# --- search_players paging ----------------------------------------------


@respx.mock
async def test_search_players_filters_by_name(app):
    mcp, _ = app
    route("FetchPlayerListing").mock(
        return_value=httpx.Response(200, json=exhausted_page())
    )
    payload = await call(mcp, "search_players", name="stafford")
    names = [p["name"] for p in payload["data"]["players"]]
    assert names == ["Matthew Stafford"]
    assert payload["data"]["returned"] == 1


@respx.mock
async def test_search_players_respects_limit(app):
    mcp, _ = app
    route("FetchPlayerListing").mock(
        return_value=httpx.Response(200, json=load_fixture("player_listing"))
    )
    payload = await call(mcp, "search_players", limit=2)
    assert payload["data"]["returned"] == 2


@respx.mock
async def test_search_players_no_match_is_empty_not_an_error(app):
    mcp, _ = app
    route("FetchPlayerListing").mock(
        return_value=httpx.Response(200, json=exhausted_page())
    )
    payload = await call(mcp, "search_players", name="zzzznotaplayer")
    assert payload["data"]["players"] == []
    assert payload["data"]["returned"] == 0


# --- score_stat_line -----------------------------------------------------


@respx.mock
async def test_score_stat_line_scores_against_live_rules(app):
    mcp, _ = app
    route("FetchLeagueRules").mock(
        return_value=httpx.Response(200, json=load_fixture("league_rules"))
    )
    payload = await call(
        mcp,
        "score_stat_line",
        stats={"passing_yard": 218, "passing_td": [1, 1, 1], "rushing_yard": 48},
        position="QB",
    )
    assert payload["data"]["total"] == pytest.approx(31.52, abs=0.01)


@respx.mock
async def test_score_stat_line_accepts_a_json_string(app):
    """Some MCP clients serialise object args; both spellings must work."""
    mcp, _ = app
    route("FetchLeagueRules").mock(
        return_value=httpx.Response(200, json=load_fixture("league_rules"))
    )
    payload = await call(
        mcp,
        "score_stat_line",
        stats=json.dumps({"passing_yard": 250}),
        position="QB",
    )
    assert payload["data"]["total"] == pytest.approx(10.0)


@respx.mock
async def test_score_stat_line_rejects_an_ambiguous_key(app):
    mcp, _ = app
    route("FetchLeagueRules").mock(
        return_value=httpx.Response(200, json=load_fixture("league_rules"))
    )
    payload = await call(
        mcp, "score_stat_line", stats={"interception": 1}, position="QB"
    )
    assert payload["code"] == "INVALID_INPUT"
    assert "passing_interception" in payload["error"]


@respx.mock
async def test_score_stat_line_caches_the_rule_fetch(app):
    """A batch of scoring calls must not refetch rules every time."""
    mcp, _ = app
    fetch = route("FetchLeagueRules").mock(
        return_value=httpx.Response(200, json=load_fixture("league_rules"))
    )
    for _ in range(3):
        await call(mcp, "score_stat_line", stats={"passing_yard": 100}, position="QB")
    assert fetch.call_count == 1


@respx.mock
async def test_search_players_stops_when_the_cursor_does_not_advance(app):
    """A non-advancing next_offset must not re-read the same page forever.

    Fleaflicker advances it in practice, but trusting that blindly turned one
    matching player into twelve copies of him.
    """
    mcp, _ = app
    stuck = load_fixture("player_listing")
    stuck["resultOffsetNext"] = 0  # never moves forward
    fetch = route("FetchPlayerListing").mock(
        return_value=httpx.Response(200, json=stuck)
    )
    payload = await call(mcp, "search_players", name="stafford")
    assert payload["data"]["returned"] == 1
    assert fetch.call_count == 1
