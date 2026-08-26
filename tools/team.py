"""Team-level reads: one roster, and the player pool.

See ``tools/league.py`` for why the return shape lives above ``Args:``.
"""

from __future__ import annotations

from fastmcp import FastMCP

from clients import normalize
from clients.errors import FleaflickerInputError
from clients.fleaflicker import FleaflickerClient
from tools.common import ok, tool_guard

# The upstream page size. Fleaflicker ignores a requested limit and always
# returns 30, so paging is the only way to go deeper.
PAGE_SIZE = 30
MAX_LIMIT = 300
# Cap the walk when filtering by name, so a name that matches nothing costs a
# bounded number of upstream requests instead of paging the entire pool.
MAX_SEARCH_PAGES = 12


def register_team_tools(mcp: FastMCP, client: FleaflickerClient) -> None:
    @mcp.tool()
    @tool_guard
    async def get_roster(
        team_id: int,
        league_id: int | None = None,
        season: int | None = None,
        week: int | None = None,
    ) -> str:
        """Get one team's roster, organised by lineup slot.

        Returns JSON with `data.slots[]`, starters first. Each slot carries
        `slot` (the position label), `group` (`START`, or null for bench),
        `eligibility[]`, and `player` (or null for an empty slot) with `id`,
        `name`, `position`, `pro_team`, `bye_week`, `points`, `season_total`,
        `season_average`.

        Get team ids from `get_standings`.

        Idempotent: yes, read-only.

        Example: get_roster(team_id=1255738, season=2025, week=1)

        Args:
            team_id: Fleaflicker team id, from `get_standings`.
            league_id: Fleaflicker league id. Defaults to FLEAFLICKER_LEAGUE_ID.
            season: Season year. Defaults to the current season.
            week: Scoring period. Defaults to the current week. Set it to see
                what a team actually started in a past week.
        """
        payload = await client.roster(team_id, league_id, season, week)
        return ok(normalize.roster(payload))

    @mcp.tool()
    @tool_guard
    async def search_players(
        name: str | None = None,
        position: str | None = None,
        league_id: int | None = None,
        season: int | None = None,
        limit: int = PAGE_SIZE,
        offset: int = 0,
        free_agents_only: bool = False,
    ) -> str:
        """Search the league's player pool by name or position.

        Returns JSON with `data.players[]`, each carrying `id`, `name`,
        `position`, `pro_team`, `bye_week`, `percent_owned`, `owned_by`
        (empty string when unowned), `season_total`, `season_average`, and
        `rank_fantasy` / `rank_draft` as `{overall, positional}` when the league
        publishes them. Also `data.total` (pool size upstream),
        `data.returned`, and `data.next_offset` (null when exhausted).

        A name that matches nothing returns an empty list, not an error.

        **Read `data.next_offset` before concluding a name is not in the pool.**
        Name matching happens client-side over fetched pages, and a name search
        scans at most 12 upstream pages (360 players) per call. So a search can
        stop with fewer than `limit` results while matches remain further down
        the pool -- most likely for a common substring, or a position with a
        deep pool late in a season.

        `data.returned` does NOT distinguish those two cases and must not be
        used to decide: a short result means only "this call ended", never "the
        pool is exhausted". `data.next_offset` is the one that separates them.
        It is null only when the pool really is exhausted, and non-null whenever
        there is more to read -- whether because you hit `limit` or because the
        page cap stopped the scan. Call again with `offset=data.next_offset` to
        continue from where this one stopped.

        Idempotent: yes, read-only.

        Example: search_players(name="Chase", position="WR")

        Args:
            name: Case-insensitive substring match on the full name, applied
                over the fetched pages. Omit to browse by position.
            position: One of QB, RB, WR, TE, D/ST, K. Omit for all positions.
            league_id: Fleaflicker league id. Defaults to FLEAFLICKER_LEAGUE_ID.
            season: Season year. Defaults to the current season.
            limit: Maximum players to return, 1 to 300. Defaults to 30. The
                upstream pages at 30, so a larger limit costs one request per
                30 and is fetched automatically.
            offset: Starting offset for paging. Defaults to 0.
            free_agents_only: Restrict to unowned players. Defaults to False.
        """
        if limit < 1 or limit > MAX_LIMIT:
            raise FleaflickerInputError(
                f"limit must be between 1 and {MAX_LIMIT}; got {limit}."
            )
        if offset < 0:
            raise FleaflickerInputError(f"offset must be 0 or greater; got {offset}.")

        needle = (name or "").strip().lower()
        collected: list[dict] = []
        total = 0
        cursor: int | None = offset
        # Name filtering happens client-side, so a narrow match may need several
        # upstream pages; an unfiltered request needs only enough to fill limit.
        max_pages = MAX_SEARCH_PAGES if needle else max(1, -(-limit // PAGE_SIZE))

        for _ in range(max_pages):
            payload = await client.player_listing(
                league_id=league_id,
                position=position,
                season=season,
                result_offset=cursor or 0,
                free_agents_only=free_agents_only,
            )
            page = normalize.player_listing(payload)
            total = page["total"] or total

            for entry in page["players"]:
                if needle and needle not in entry["name"].lower():
                    continue
                collected.append(entry)
                if len(collected) >= limit:
                    break

            # The cursor must strictly advance. A non-advancing next_offset
            # would re-read the same page and duplicate every match on it, so
            # treat "did not move forward" as exhausted rather than trusting it.
            next_cursor = page["next_offset"]
            if next_cursor is not None and next_cursor <= (cursor or 0):
                next_cursor = None
            cursor = next_cursor

            if len(collected) >= limit or cursor is None:
                break

        return ok(
            {
                "players": collected,
                "total": total,
                "returned": len(collected),
                "next_offset": cursor,
            }
        )
