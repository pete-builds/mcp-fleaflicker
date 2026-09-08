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
from tools.common import READ_ONLY, ok, ok_compact, tool_guard


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
        league_id: int | None = None,
        season: int | None = None,
        since_overall: int | None = None,
        team_id: int | None = None,
        needs_for_team: int | None = None,
    ) -> str:
        """Get a draft's picks as one flat list in true selection order.

        Built for polling a LIVE draft. Call it once with no `since_overall` to
        get the board so far, then pass back `data.next_overall - 1` on every
        later call to receive only what is new. On a 14-team board that is the
        difference between roughly 42 KB and a few hundred bytes per poll.

        Returns JSON with `data.draft_type` (`SNAKE`, `LINEAR` or `UNKNOWN`),
        `data.total_picks` (every seat on the board), `data.picks_made`,
        `data.next_overall` (the pick number that lands next), `data.returned`
        (how many picks this call carried), and `data.picks[]`.
        `data.draft_order[]` (`slot`, `id`, `name`) is present only on a call
        with no `since_overall`, because the seat order cannot change mid-draft
        and resending it would be most of a delta poll's payload.

        Each pick carries `overall`, `round`, `pick_in_round`, `team`,
        `team_id`, and `player` (`id`, `name`, `position`, `pro_team`,
        `bye_week`, `percent_owned`).

        `overall` is the TRUE selection number read from the upstream, already
        snake-aware: in a snake, round 2 column 1 is overall 28, not 15. Do not
        infer pick order from a board's left-to-right layout, which is a static
        grid and reverses against the real sequence in even rounds.

        `picks_made` and `next_overall` always describe the whole draft, never
        the filtered slice, so they stay valid as a polling cursor. Undrafted
        seats are omitted rather than sent as nulls: check `picks_made == 0` to
        tell whether a season has drafted at all.

        Pass `needs_for_team` to get `data.needs` alongside the picks: that
        team's live roster counts against the league's own position minimums
        and maximums, as `positions[LABEL]` with `min`, `max`, `starts`,
        `have`, `still_required` and `room`.

        Idempotent: yes, read-only.

        Example: get_draft_board(since_overall=104, needs_for_team=1255738)

        Args:
            league_id: Fleaflicker league id. Defaults to FLEAFLICKER_LEAGUE_ID.
            season: Season year. Defaults to the league's current season.
            since_overall: Return only picks after this overall number. Pass
                the previous call's `next_overall - 1` to poll for new picks.
            team_id: Return only this team's picks.
            needs_for_team: Also return this team's roster-cap position counts.
        """
        payload = await client.draft_board(league_id, season)
        data = normalize.draft_board(
            payload, since_overall=since_overall, team_id=team_id
        )
        if needs_for_team is not None:
            data["needs"] = normalize.draft_roster_needs(payload, needs_for_team)
        return ok_compact(data)
