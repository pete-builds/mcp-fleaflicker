"""Async HTTP client for Fleaflicker's public read API.

Fleaflicker exposes an unauthenticated, read-only JSON API at
``https://www.fleaflicker.com/api/<Method>``. There is no key, no token, and no
write surface, which is why this server has no credential store and no
destructive tool: there is nothing to leak and nothing to break.

Response shapes were mapped against the live API rather than documentation.
Two behaviours matter to every caller:

* **Zero-valued fields are omitted.** The payloads are protobuf-derived, so a
  field whose value is ``0``, ``false``, or ``""`` is absent from the JSON
  entirely. Never distinguish "absent" from "zero" — they are the same thing.
* **Errors arrive as HTTP 200.** A bad ``league_id`` returns a 200 whose body
  carries an ``error`` object. Status code alone is not success.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from clients.errors import (
    FleaflickerAPIError,
    FleaflickerInputError,
    FleaflickerNotFoundError,
    FleaflickerRateLimitError,
)

log = logging.getLogger("mcp-fleaflicker.client")

DEFAULT_BASE_URL = "https://www.fleaflicker.com/api"
DEFAULT_SPORT = "NFL"
DEFAULT_TIMEOUT = 30.0

# Fleaflicker publishes no documented rate limit. This server is read-only and
# low-volume, so a small connection ceiling plus one retry is the whole policy.
_MAX_CONNECTIONS = 10
_MAX_KEEPALIVE = 5


class FleaflickerClient:
    """Thin async wrapper over the Fleaflicker read API."""

    def __init__(
        self,
        *,
        league_id: int | None = None,
        sport: str = DEFAULT_SPORT,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = "mcp-fleaflicker",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.default_league_id = league_id
        self.sport = sport
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=_MAX_CONNECTIONS,
                max_keepalive_connections=_MAX_KEEPALIVE,
            ),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def resolve_league_id(self, league_id: int | None) -> int:
        """Per-call league id, else the configured default.

        Raises:
            FleaflickerInputError: if neither is set, with the remedy named.
        """
        resolved = league_id if league_id is not None else self.default_league_id
        if resolved is None:
            raise FleaflickerInputError(
                "No league specified. Pass league_id, or set FLEAFLICKER_LEAGUE_ID "
                "so it can be omitted. Find it in your league URL: "
                "fleaflicker.com/nfl/leagues/<league_id>"
            )
        try:
            return int(resolved)
        except (TypeError, ValueError) as exc:
            raise FleaflickerInputError(
                f"league_id must be an integer; got {resolved!r}."
            ) from exc

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        """GET one API method, with one retry on transient connection failure.

        ``None`` params are dropped so callers can pass optional filters
        straight through without pre-filtering.
        """
        url = f"{self.base_url}/{method}"
        query = {"sport": self.sport}
        query.update({k: v for k, v in params.items() if v is not None})

        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._client.get(url, params=query)
                return self._parse(response, method)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt == 0:
                    # One transient hiccup is normal; two is a real outage.
                    await asyncio.sleep(0.5)
                    continue

        raise FleaflickerAPIError(
            f"Fleaflicker is unreachable calling {method}.",
            details={"method": method, "exception": type(last_exc).__name__},
        )

    def _parse(self, response: httpx.Response, method: str) -> dict[str, Any]:
        """Turn an HTTP response into a payload or the right exception."""
        if response.status_code == 429:
            raise FleaflickerRateLimitError(
                "Fleaflicker is rate limiting this client.",
                details={"method": method, "retry_after": response.headers.get("retry-after")},
            )
        if response.status_code == 404:
            raise FleaflickerNotFoundError(
                f"Fleaflicker has no endpoint named {method}.",
                details={"method": method, "status": 404},
            )
        if response.status_code >= 500:
            raise FleaflickerAPIError(
                f"Fleaflicker returned {response.status_code} for {method}.",
                details={"method": method, "status": response.status_code},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise FleaflickerAPIError(
                f"Fleaflicker returned a non-JSON body for {method}.",
                details={"method": method, "status": response.status_code},
            ) from exc

        if not isinstance(payload, dict):
            raise FleaflickerAPIError(
                f"Fleaflicker returned a {type(payload).__name__}, expected an object.",
                details={"method": method},
            )

        # A 200 with an error body is the documented failure mode for a bad id.
        error = payload.get("error")
        if error:
            message = (
                error.get("message") if isinstance(error, dict) else str(error)
            ) or "Fleaflicker rejected the request."
            if response.status_code >= 400 or "not found" in message.lower():
                raise FleaflickerNotFoundError(
                    message, details={"method": method, "upstream_error": error}
                )
            raise FleaflickerAPIError(
                message, details={"method": method, "upstream_error": error}
            )

        return payload

    # ---- API methods -------------------------------------------------

    async def league_rules(self, league_id: int | None = None) -> dict[str, Any]:
        return await self.call(
            "FetchLeagueRules", league_id=self.resolve_league_id(league_id)
        )

    async def league_standings(
        self, league_id: int | None = None, season: int | None = None
    ) -> dict[str, Any]:
        return await self.call(
            "FetchLeagueStandings",
            league_id=self.resolve_league_id(league_id),
            season=season,
        )

    async def league_scoreboard(
        self,
        league_id: int | None = None,
        season: int | None = None,
        scoring_period: int | None = None,
    ) -> dict[str, Any]:
        return await self.call(
            "FetchLeagueScoreboard",
            league_id=self.resolve_league_id(league_id),
            season=season,
            scoring_period=scoring_period,
        )

    async def boxscore(
        self, fantasy_game_id: int, league_id: int | None = None
    ) -> dict[str, Any]:
        return await self.call(
            "FetchLeagueBoxscore",
            league_id=self.resolve_league_id(league_id),
            fantasy_game_id=fantasy_game_id,
        )

    async def roster(
        self,
        team_id: int,
        league_id: int | None = None,
        season: int | None = None,
        scoring_period: int | None = None,
    ) -> dict[str, Any]:
        return await self.call(
            "FetchRoster",
            league_id=self.resolve_league_id(league_id),
            team_id=team_id,
            season=season,
            scoring_period=scoring_period,
        )

    async def draft_board(
        self, league_id: int | None = None, season: int | None = None
    ) -> dict[str, Any]:
        return await self.call(
            "FetchLeagueDraftBoard",
            league_id=self.resolve_league_id(league_id),
            season=season,
        )

    async def player_listing(
        self,
        league_id: int | None = None,
        position: str | None = None,
        season: int | None = None,
        result_offset: int = 0,
        sort: str | None = None,
        free_agents_only: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "league_id": self.resolve_league_id(league_id),
            "season": season,
            "result_offset": result_offset or None,
            "sort": sort,
        }
        if position:
            params["filter.position.label"] = position.upper()
        if free_agents_only:
            params["filter.free_agent_only"] = "true"
        return await self.call("FetchPlayerListing", **params)
