"""The scoring tool: apply a league's own rules to a stat line."""

from __future__ import annotations

import json
import time
from typing import Any

from fastmcp import FastMCP

from clients.errors import FleaflickerInputError
from clients.fleaflicker import FleaflickerClient
from clients.scoring import RuleSet, score_stats
from tools.common import ok, tool_guard

# Scoring rules change at most once a season, and every score_stat_line call
# needs them. Cache per league so a batch of scoring calls costs one fetch.
RULES_TTL_SECONDS = 3600


class RuleCache:
    """Per-league rule sets with a wall-clock TTL."""

    def __init__(self, client: FleaflickerClient, ttl: float = RULES_TTL_SECONDS):
        self._client = client
        self._ttl = ttl
        self._entries: dict[int, tuple[float, RuleSet]] = {}

    async def get(self, league_id: int | None) -> RuleSet:
        resolved = self._client.resolve_league_id(league_id)
        cached = self._entries.get(resolved)
        now = time.monotonic()
        if cached and now - cached[0] < self._ttl:
            return cached[1]

        payload = await self._client.league_rules(resolved)
        rule_set = RuleSet.from_api(payload)
        self._entries[resolved] = (now, rule_set)
        return rule_set


def _coerce_stats(stats: Any) -> dict[str, Any]:
    """Accept a mapping, or the JSON string some clients send instead.

    MCP clients vary in whether an object-typed argument survives as an object
    or arrives serialised. Handling both here keeps the failure mode out of the
    model's way.
    """
    if isinstance(stats, str):
        try:
            stats = json.loads(stats)
        except json.JSONDecodeError as exc:
            raise FleaflickerInputError(
                f"stats was a string but not valid JSON: {exc}."
            ) from exc
    if not isinstance(stats, dict):
        raise FleaflickerInputError(
            f"stats must be an object of stat_key -> value; got {type(stats).__name__}."
        )
    return stats


def register_scoring_tools(mcp: FastMCP, client: FleaflickerClient) -> None:
    cache = RuleCache(client)

    @mcp.tool()
    @tool_guard
    async def score_stat_line(
        stats: dict[str, Any] | str,
        position: str = "WR",
        league_id: int | None = None,
    ) -> str:
        """Score one game's stat line under a league's real scoring rules.

        Fetches the league's published rules and applies them, so bonuses,
        thresholds, and position restrictions are the league's own, not a
        generic PPR approximation.

        **Score one game at a time.** Every bonus in a Fleaflicker rule set
        fires on a single game's box score, so a season total run through this
        tool will miscount them: a receiver with 2,000 yards over 17 games earns
        the 150-yard bonus only in the games he actually cleared 150.

        Call `get_league_rules` for the valid stat keys
        (`data.scoring.stat_keys`). A plain number is a count or total. A
        **list** is the per-event yardage that distance bonuses need:
        `{"passing_td": [45, 12]}` is two passing touchdowns, one of 45 yards
        and one of 12, scoring the flat rule twice and the 40-to-79 bonus once.
        Passing `{"passing_td": 2}` scores the flat rule and reports the bonus
        under `unresolved_bonuses` rather than silently scoring it as zero.

        Returns JSON with `data.total` (the score), `data.breakdown[]` (one
        entry per contributing rule, with `category`, `group`, `kind`, `points`,
        `rule`, and `detail` showing the arithmetic), `data.unscored_keys[]`
        (keys this league does not score, usually a typo), and
        `data.unresolved_bonuses[]` (distance bonuses that needed a list).

        A stat key that matches more than one scoring group fails with
        INVALID_INPUT and names the alternatives, rather than guessing. In most
        leagues `interception` is the one that collides: use
        `passing_interception` (thrown, negative) or `defense_interception`
        (caught, positive).

        Idempotent: yes, pure computation over a read-only rule fetch.

        Example: score_stat_line(stats={"passing_yard": 318,
        "passing_td": [45, 12], "passing_interception": 1}, position="QB")

        Args:
            stats: Stat key to value, as an object (or a JSON object string).
            position: Roster position, for position-restricted rules such as
                receptions (skill positions only) or fumble recoveries (D/ST
                only). Defaults to "WR".
            league_id: Fleaflicker league id. Defaults to FLEAFLICKER_LEAGUE_ID.
        """
        payload = _coerce_stats(stats)
        rule_set = await cache.get(league_id)
        result = score_stats(rule_set, payload, position)
        return ok(result.as_dict())
