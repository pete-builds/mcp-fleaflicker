"""HTTP client tests: error mapping, retry, and param handling."""

from __future__ import annotations

import httpx
import pytest
import respx

from clients.errors import (
    FleaflickerAPIError,
    FleaflickerInputError,
    FleaflickerNotFoundError,
    FleaflickerRateLimitError,
)
from clients.fleaflicker import DEFAULT_BASE_URL, FleaflickerClient

ROUTE = f"{DEFAULT_BASE_URL}/FetchLeagueRules"


@pytest.fixture
async def api():
    client = FleaflickerClient(league_id=14153)
    yield client
    await client.close()


# --- league id resolution ------------------------------------------------


def test_per_call_league_id_wins(api: FleaflickerClient):
    assert api.resolve_league_id(999) == 999
    assert api.resolve_league_id(None) == 14153


def test_missing_league_id_names_the_remedy():
    client = FleaflickerClient(league_id=None)
    with pytest.raises(FleaflickerInputError) as exc:
        client.resolve_league_id(None)
    assert "FLEAFLICKER_LEAGUE_ID" in str(exc.value)


def test_non_numeric_league_id_is_rejected(api: FleaflickerClient):
    with pytest.raises(FleaflickerInputError):
        api.resolve_league_id("not-an-id")  # type: ignore[arg-type]


# --- request shaping -----------------------------------------------------


@respx.mock
async def test_none_params_are_dropped(api: FleaflickerClient):
    route = respx.get(ROUTE).mock(return_value=httpx.Response(200, json={"ok": True}))
    await api.call("FetchLeagueRules", league_id=14153, season=None)

    query = route.calls.last.request.url.params
    assert "season" not in query
    assert query["league_id"] == "14153"
    # sport is always sent; the API requires it.
    assert query["sport"] == "NFL"


# --- error mapping -------------------------------------------------------


@respx.mock
async def test_200_with_error_body_is_a_failure(api: FleaflickerClient):
    """A bad league id returns HTTP 200 with an error object, not a 4xx."""
    respx.get(ROUTE).mock(
        return_value=httpx.Response(
            200, json={"error": {"message": "League not found."}}
        )
    )
    with pytest.raises(FleaflickerNotFoundError) as exc:
        await api.league_rules(999999)
    assert "not found" in str(exc.value).lower()


@respx.mock
async def test_429_maps_to_rate_limited(api: FleaflickerClient):
    respx.get(ROUTE).mock(
        return_value=httpx.Response(429, headers={"retry-after": "30"}, json={})
    )
    with pytest.raises(FleaflickerRateLimitError) as exc:
        await api.league_rules()
    assert exc.value.details["retry_after"] == "30"


@respx.mock
async def test_500_maps_to_upstream_down(api: FleaflickerClient):
    respx.get(ROUTE).mock(return_value=httpx.Response(503, text="down"))
    with pytest.raises(FleaflickerAPIError) as exc:
        await api.league_rules()
    assert exc.value.details["status"] == 503


@respx.mock
async def test_non_json_body_is_an_upstream_error(api: FleaflickerClient):
    respx.get(ROUTE).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(FleaflickerAPIError):
        await api.league_rules()


@respx.mock
async def test_json_array_body_is_rejected(api: FleaflickerClient):
    respx.get(ROUTE).mock(return_value=httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(FleaflickerAPIError):
        await api.league_rules()


# --- retry ---------------------------------------------------------------


@respx.mock
async def test_transient_connect_error_is_retried_once(api: FleaflickerClient):
    route = respx.get(ROUTE).mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert await api.league_rules() == {"ok": True}
    assert route.call_count == 2


@respx.mock
async def test_two_failures_give_up(api: FleaflickerClient):
    route = respx.get(ROUTE).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(FleaflickerAPIError) as exc:
        await api.league_rules()
    assert route.call_count == 2
    assert exc.value.code == "UPSTREAM_DOWN"


# --- error codes are from the fixed enum ---------------------------------


def test_error_codes_stay_in_the_enum():
    allowed = {
        "UPSTREAM_DOWN",
        "AUTH_FAILED",
        "INVALID_INPUT",
        "NOT_FOUND",
        "RATE_LIMITED",
        "INTERNAL",
    }
    from clients import errors

    for name in dir(errors):
        obj = getattr(errors, name)
        if isinstance(obj, type) and issubclass(obj, errors.FleaflickerError):
            assert obj.code in allowed, f"{name} invented code {obj.code}"
