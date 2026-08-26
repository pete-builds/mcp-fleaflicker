"""League-wide reads: rules, standings, and the draft board.

Docstring layout note: FastMCP sends the prose *above* ``Args:`` as the tool
description and folds ``Args:`` into the parameter schema, discarding anything
after it. So the return shape and example live in the prose, where the model
actually sees them, and ``Args:`` comes last.
"""

from __future__ import annotations

from fastmcp import FastMCP

from clients import normalize
from clients.fleaflicker import FleaflickerClient
from clients.scoring import RuleSet
from tools.common import READ_ONLY, ok, tool_guard


def register_league_tools(mcp: FastMCP, client: FleaflickerClient) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @tool_guard
    async def get_league_rules(league_id: int | None = None) -> str:
        """Get a league's roster construction and its complete scoring rules.

        Read this before scoring anything: it returns the exact stat keys
        `score_stat_line` accepts. The two tools pair up, this one says what the
        league scores, that one applies it.

        Returns JSON with `data.roster` (`starters`, `bench`, `max_active`,
        `max_roster_size`, and `positions[]` carrying each slot's `label`,
        `eligibility`, `starts`, `roster_min`, `roster_max`) and `data.scoring`
        (`rule_count`, `stat_keys[]`, and `rules[]` with `category`,
        `category_id`, `group`, `points`, `kind`, `multi_value`, `applies_to`,
        `description`).

        `kind` explains how a rule fires: `linear` is points per unit,
        `total_threshold` is a one-time bonus when a single game's total clears
        a bound (300 passing yards), `total_range` is a one-time bonus inside a
        closed range (a shutout), and `per_event` is a bonus per qualifying
        event (each touchdown of 40+ yards).

        Idempotent: yes, read-only.

        Example: get_league_rules(league_id=14153)

        Args:
            league_id: Fleaflicker league id. Defaults to FLEAFLICKER_LEAGUE_ID.
        """
        payload = await client.league_rules(league_id)
        return ok(normalize.league_rules(payload, RuleSet.from_api(payload)))

    @mcp.tool(annotations=READ_ONLY)
    @tool_guard
    async def get_standings(
        league_id: int | None = None, season: int | None = None
    ) -> str:
        """Get league standings: records, points for and against, streaks.

        Returns JSON with `data.league` (`id`, `name`, `size`), `data.season`,
        and `data.teams[]` sorted by wins then points for. Each team carries
        `rank`, `id`, `name`, `owner`, `division`, `record` (`wins`, `losses`,
        `ties`, `win_percentage`, `formatted`), `points_for`, `points_against`,
        `streak`, `draft_position`, `waiver_position`.

        Team ids from here are what `get_roster` takes. `division` is an empty
        string in single-division leagues.

        Idempotent: yes, read-only.

        Example: get_standings(season=2025)

        Args:
            league_id: Fleaflicker league id. Defaults to FLEAFLICKER_LEAGUE_ID.
            season: Season year. Defaults to the league's current season.
        """
        payload = await client.league_standings(league_id, season)
        return ok(normalize.standings(payload))

    @mcp.tool(annotations=READ_ONLY)
    @tool_guard
    async def get_draft_board(
        league_id: int | None = None, season: int | None = None
    ) -> str:
        """Get every pick from a league's draft, round by round.

        Returns JSON with `data.rounds[]` (`round`, `picks[]`),
        `data.total_picks`, and `data.draft_order[]`. Each pick carries
        `overall`, `pick_in_round`, `team`, `team_id`, and `player` (`id`,
        `name`, `position`, `pro_team`, `bye_week`, `percent_owned`) or null.

        A draft that has not happened yet returns rounds whose picks all carry
        `player: null`, which is how to check whether a season has drafted.

        Idempotent: yes, read-only.

        Example: get_draft_board(season=2025)

        Args:
            league_id: Fleaflicker league id. Defaults to FLEAFLICKER_LEAGUE_ID.
            season: Season year. Defaults to the league's current season.
        """
        payload = await client.draft_board(league_id, season)
        return ok(normalize.draft_board(payload))
