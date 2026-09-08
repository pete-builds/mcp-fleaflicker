"""Team-level reads: one roster, and the player pool.

See ``tools/league.py`` for why the return shape lives above ``Args:``.
"""

from __future__ import annotations

from fastmcp import FastMCP

from clients import normalize
from clients.errors import FleaflickerInputError
from clients.fleaflicker import FleaflickerClient
from tools.common import READ_ONLY, ok, ok_compact, tool_guard

# The upstream page size. Fleaflicker ignores a requested limit and always
# returns 30, so paging is the only way to go deeper.
PAGE_SIZE = 30
MAX_LIMIT = 300
# Cap the walk when filtering by name, so a name that matches nothing costs a
# bounded number of upstream requests instead of paging the entire pool.
MAX_SEARCH_PAGES = 12
# The free-agent pool is a few hundred deep. Cap the walk so a bad filter costs
# a bounded number of upstream requests rather than the whole pool.
MAX_POOL_PAGES = 20


def register_team_tools(mcp: FastMCP, client: FleaflickerClient) -> None:
    @mcp.tool(annotations=READ_ONLY)
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

    @mcp.tool(annotations=READ_ONLY)
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

    @mcp.tool(annotations=READ_ONLY)
    @tool_guard
    async def get_available_players(
        position: str | None = None,
        league_id: int | None = None,
        limit: int = 25,
        min_projected: float | None = None,
    ) -> str:
        """Get the best undrafted players left, ranked by projected points.

        This is the during-a-draft tool: it answers "who is actually still on
        the board" in one call, without diffing a draft board against a
        ranking list. Everything returned is unowned right now.

        Returns JSON with `data.players[]` sorted by `projected_points`
        descending, plus `data.returned`, `data.pool_size` (the whole free
        agent pool upstream) and `data.scanned` (how many were examined).

        Each player carries `id`, `name`, `position`, `pro_team`, `bye_week`,
        `percent_owned`, `projected_points`, and `rank_draft` (`overall`,
        `positional`, `label` like "QB29", `rating`) when the league publishes
        one. The key matches `search_players` so both tools read the same.

        `projected_points` is Fleaflicker's own projection computed under THIS
        league's scoring rules, so it already accounts for superflex, passing
        TD value and every bonus. `rank_draft` is the default board the rest of
        the room is drafting from. A large gap between the two is a mispriced
        player.

        Cost: the pool is unsorted upstream and pages at 30, so one call walks
        the whole free agent pool (roughly a dozen requests) to rank it. Call
        it when you need the board, not on a tight poll loop.

        Position filtering is applied here rather than upstream, because
        Fleaflicker accepts `filter.position.label` and then ignores it.

        Idempotent: yes, read-only.

        Example: get_available_players(position="TE", limit=10)

        Args:
            position: One of QB, RB, WR, TE, D/ST, K. Omit for all positions.
            league_id: Fleaflicker league id. Defaults to FLEAFLICKER_LEAGUE_ID.
            limit: Maximum players to return, 1 to 300. Defaults to 25.
            min_projected: Drop anyone projected below this many points.
        """
        if limit < 1 or limit > MAX_LIMIT:
            raise FleaflickerInputError(
                f"limit must be between 1 and {MAX_LIMIT}; got {limit}."
            )

        wanted = (position or "").strip().upper()
        collected: list[dict] = []
        scanned = 0
        total = 0
        cursor = 0

        for _ in range(MAX_POOL_PAGES):
            payload = await client.player_listing(
                league_id=league_id,
                position=position,
                result_offset=cursor,
                free_agents_only=True,
            )
            page = normalize.player_listing(payload)
            total = page["total"] or total

            for entry in page["players"]:
                scanned += 1
                if wanted and entry.get("position", "").upper() != wanted:
                    continue
                if min_projected is not None and entry["projected_points"] < min_projected:
                    continue
                collected.append(
                    {
                        key: entry[key]
                        for key in (
                            "id",
                            "name",
                            "position",
                            "pro_team",
                            "bye_week",
                            "percent_owned",
                            "projected_points",
                        )
                        if key in entry
                    }
                    | ({"rank_draft": entry["rank_draft"]} if "rank_draft" in entry else {})
                )

            # Same non-advancing-cursor guard as search_players: a cursor that
            # does not move forward would re-read the page and duplicate it.
            next_cursor = page["next_offset"]
            if next_cursor is None or next_cursor <= cursor:
                break
            cursor = next_cursor

        collected.sort(key=lambda p: p["projected_points"], reverse=True)
        return ok_compact(
            {
                "players": collected[:limit],
                "returned": min(len(collected), limit),
                "pool_size": total,
                "scanned": scanned,
            }
        )
