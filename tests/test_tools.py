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
from tools.team import MAX_SEARCH_PAGES, register_team_tools

EXPECTED_TOOLS = {
    "get_boxscore",
    "get_draft_board",
    "get_league_rules",
    "get_roster",
    "get_standings",
    "list_matchups",
    "get_available_players",
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


async def raw(mcp: FastMCP, tool: str, /, **kwargs) -> str:
    """The tool's response as the wire string, before JSON parsing.

    Needed by the payload-size and whitespace tests, which are assertions about
    the serialisation itself rather than about the decoded value.
    """
    result = await mcp.call_tool(tool, kwargs or {})
    blocks = result.content if hasattr(result, "content") else result
    return blocks[0].text


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
    assert payload["data"]["picks"][0]["overall"] == 1
    assert payload["data"]["draft_type"] == "SNAKE"


@respx.mock
async def test_get_draft_board_delta_carries_only_new_picks(app):
    """The live-draft path: poll with a cursor, get back only what landed."""
    mcp, _ = app
    route("FetchLeagueDraftBoard").mock(
        return_value=httpx.Response(200, json=load_fixture("draft_board"))
    )
    payload = await call(mcp, "get_draft_board", since_overall=27)
    data = payload["data"]
    assert [p["overall"] for p in data["picks"]] == [28, 29]
    # The cursor still describes the whole draft, not the slice.
    assert data["picks_made"] == 29
    assert data["next_overall"] == 30


@respx.mock
async def test_get_draft_board_delta_is_far_smaller_than_the_full_board(app):
    """The reason this change exists, pinned as a number.

    A poll that costs as much as a full fetch is not a delta.
    """
    mcp, _ = app
    route("FetchLeagueDraftBoard").mock(
        return_value=httpx.Response(200, json=load_fixture("draft_board"))
    )
    full = await raw(mcp, "get_draft_board")
    delta = await raw(mcp, "get_draft_board", since_overall=27)
    assert len(delta) < len(full) / 4


@respx.mock
async def test_get_draft_board_is_serialised_compactly(app):
    """Indentation was ~40% of a 73 KB live board. It is not coming back."""
    mcp, _ = app
    route("FetchLeagueDraftBoard").mock(
        return_value=httpx.Response(200, json=load_fixture("draft_board"))
    )
    body = await raw(mcp, "get_draft_board")
    assert '\n' not in body
    assert ', ' not in body
    json.loads(body)  # still valid JSON, only the whitespace is gone


@respx.mock
async def test_get_draft_board_reports_roster_needs(app):
    mcp, _ = app
    route("FetchLeagueDraftBoard").mock(
        return_value=httpx.Response(200, json=load_fixture("draft_board"))
    )
    payload = await call(mcp, "get_draft_board", needs_for_team=1)
    needs = payload["data"]["needs"]["positions"]
    assert needs["RB"]["still_required"] == 2
    assert needs["QB"]["room"] == 1


# --- get_available_players -----------------------------------------------


@respx.mock
async def test_get_available_players_returns_data_envelope(app):
    mcp, _ = app
    route("FetchPlayerListing").mock(
        return_value=httpx.Response(200, json=load_fixture("player_listing"))
    )
    payload = await call(mcp, "get_available_players")
    data = payload["data"]
    assert data["players"]
    assert data["returned"] == len(data["players"])
    assert "pool_size" in data and "scanned" in data
    first = data["players"][0]
    assert {"id", "name", "position", "projected_points"} <= set(first)
    # The rank key must match search_players; a private spelling here means
    # the field silently never appears.
    assert "rank_draft" in first


@respx.mock
async def test_get_available_players_is_sorted_by_projection(app):
    mcp, _ = app
    route("FetchPlayerListing").mock(
        return_value=httpx.Response(200, json=load_fixture("player_listing"))
    )
    payload = await call(mcp, "get_available_players")
    projections = [p["projected_points"] for p in payload["data"]["players"]]
    assert projections == sorted(projections, reverse=True)


@respx.mock
async def test_get_available_players_filters_position_client_side(app):
    """Upstream accepts filter.position.label and ignores it, so we must not.

    The fixture is a mixed-position page; asking for one position has to come
    back single-position anyway.
    """
    mcp, _ = app
    route("FetchPlayerListing").mock(
        return_value=httpx.Response(200, json=load_fixture("player_listing"))
    )
    payload = await call(mcp, "get_available_players", position="QB")
    positions = {p["position"] for p in payload["data"]["players"]}
    assert positions <= {"QB"}


@respx.mock
async def test_get_available_players_rejects_a_bad_limit(app):
    mcp, _ = app
    payload = await call(mcp, "get_available_players", limit=9999)
    assert payload["code"] == "INVALID_INPUT"


@respx.mock
async def test_player_listing_never_forwards_season(app):
    """FetchPlayerListing 400s on any season value, so it must never be sent.

    Verified live 2026-09-07: with season, HTTP 400 and an HTML body; without
    it, HTTP 200. The tools still accept the argument, and must swallow it.
    """
    mcp, _ = app
    listing = route("FetchPlayerListing").mock(
        return_value=httpx.Response(200, json=load_fixture("player_listing"))
    )
    await call(mcp, "search_players", season=2026)
    assert listing.called
    for request in listing.calls:
        assert "season" not in request.request.url.params


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


@respx.mock
async def test_search_players_reports_more_to_read_when_the_page_cap_stops_it(app):
    """A capped name search must not look like an exhausted pool.

    Name matching is client-side, so a search scans at most MAX_SEARCH_PAGES
    upstream pages. When that cap ends the scan rather than `limit` doing it,
    the caller gets fewer results than asked for -- which reads exactly like
    "there is nothing more". `next_offset` is the only thing that separates the
    two, so it must survive the cap, and the docstring now tells callers to
    read it instead of `returned`.
    """
    mcp, _ = app
    page = load_fixture("player_listing")
    # A pool that always has a next page and never contains the needle, so only
    # the page cap can end the loop.
    page["resultOffsetNext"] = 30
    fetch = route("FetchPlayerListing").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                **page,
                "resultOffsetNext": 30 * (fetch.call_count + 1),
            },
        )
    )
    payload = await call(mcp, "search_players", name="nobodynamedthis", limit=30)

    assert fetch.call_count == MAX_SEARCH_PAGES
    # Short of `limit`, which on its own would suggest the pool ran out...
    assert payload["data"]["returned"] == 0
    # ...but this is what actually says whether it did, and it says no.
    assert payload["data"]["next_offset"] is not None
    assert payload["data"]["next_offset"] > 0
