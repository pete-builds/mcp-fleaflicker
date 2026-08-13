"""Weekly matchup reads: the scoreboard and a single boxscore.

See ``tools/league.py`` for why the return shape lives above ``Args:``.
"""

from __future__ import annotations

from fastmcp import FastMCP

from clients import normalize
from clients.fleaflicker import FleaflickerClient
from tools.common import ok, tool_guard


def register_matchup_tools(mcp: FastMCP, client: FleaflickerClient) -> None:
    @mcp.tool()
    @tool_guard
    async def list_matchups(
        week: int | None = None,
        league_id: int | None = None,
        season: int | None = None,
    ) -> str:
        """List a week's head-to-head matchups and scores.

        Returns JSON with `data.week` and `data.games[]`, each carrying `id`
        (pass it to `get_boxscore`), `home` and `away` team stubs,
        `home_score`, `away_score`, `home_result` / `away_result`
        (`WIN`, `LOSE`, or `TIE`), and `is_final`.

        Idempotent: yes, read-only.

        Example: list_matchups(week=1, season=2025)

        Args:
            week: Scoring period. Defaults to the current week.
            league_id: Fleaflicker league id. Defaults to FLEAFLICKER_LEAGUE_ID.
            season: Season year. Defaults to the current season.
        """
        payload = await client.league_scoreboard(league_id, season, week)
        return ok(normalize.matchups(payload))

    @mcp.tool()
    @tool_guard
    async def get_boxscore(fantasy_game_id: int, league_id: int | None = None) -> str:
        """Get one matchup's full boxscore: every slot, both teams, with points.

        Returns JSON with `data.game_id`, `data.week`, `data.home` and
        `data.away` team stubs, `data.home_total` / `data.away_total`,
        `data.home_optimum` / `data.away_optimum`, `data.is_final`, and
        `data.slots[]`. Each slot carries `slot`, `group`, and a `home` and
        `away` player (or null), each with `name`, `position`, `points`, and a
        `stats` summary keyed by category name.

        Two things here are worth knowing. The per-player `points` are
        Fleaflicker's own computed totals, so this is the authoritative check on
        anything `score_stat_line` produces for the same player and week. And
        `*_optimum` is what the roster would have scored with a perfect lineup,
        so the gap to the actual total is points left on the bench.

        Idempotent: yes, read-only.

        Example: get_boxscore(fantasy_game_id=56361068)

        Args:
            fantasy_game_id: Game id from `list_matchups`.
            league_id: Fleaflicker league id. Defaults to FLEAFLICKER_LEAGUE_ID.
        """
        payload = await client.boxscore(fantasy_game_id, league_id)
        return ok(normalize.boxscore(payload))
