"""Normaliser tests, run against real captured payloads."""

from __future__ import annotations

import pytest

from clients import normalize
from clients.scoring import RuleSet
from tests.conftest import load_fixture

# --- omitted-zero handling ----------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0.0),  # absent == zero, the whole protobuf contract
        ({}, 0.0),
        ({"value": 22.04}, 22.04),
        (7, 7.0),
        ("nope", 0.0),
        ({"value": None}, 0.0),
    ],
)
def test_num_treats_absent_as_zero(value, expected):
    assert normalize._num(value) == pytest.approx(expected)


def test_points_round_to_two_places():
    assert normalize._points({"value": 27.140001}) == 27.14


# --- standings -----------------------------------------------------------


def test_standings_flattens_and_ranks():
    data = normalize.standings(load_fixture("standings"))
    assert data["league"]["name"]
    assert data["teams"]
    # Ranked by wins then points for.
    wins = [t["record"]["wins"] for t in data["teams"]]
    assert wins == sorted(wins, reverse=True)
    assert [t["rank"] for t in data["teams"]] == list(
        range(1, len(data["teams"]) + 1)
    )
    for team in data["teams"]:
        assert team["name"]
        assert team["owner"]
        assert isinstance(team["points_for"], float)


def test_standings_tolerate_an_unnamed_division():
    """Single-division leagues omit the division name; empty is correct."""
    data = normalize.standings(load_fixture("standings"))
    assert all(t["division"] == "" for t in data["teams"])


# --- matchups ------------------------------------------------------------


def test_matchups_lift_scores_out_of_wrappers():
    data = normalize.matchups(load_fixture("scoreboard"))
    assert data["games"]
    game = data["games"][0]
    assert isinstance(game["home_score"], float)
    assert isinstance(game["away_score"], float)
    assert game["id"] > 0
    assert game["is_final"] is True


# --- boxscore ------------------------------------------------------------


def test_boxscore_pairs_both_sides():
    data = normalize.boxscore(load_fixture("boxscore"))
    assert data["game_id"] > 0
    assert data["home_total"] == pytest.approx(123.4)
    assert data["away_total"] > 0
    slot = data["slots"][0]
    assert slot["slot"]
    assert slot["home"] and slot["away"]
    assert slot["home"]["points"] > 0


def test_boxscore_unwraps_the_double_nested_total():
    """`pointsHome.total.value.value` — two wrappers, unlike the scoreboard's one.

    Hardcoding a single unwrap here returned 0.0 for every matchup while every
    other field looked right.
    """
    data = normalize.boxscore(load_fixture("boxscore"))
    assert data["home_total"] == pytest.approx(123.4)
    # Optimum is the perfect-lineup score, so it can never be below actual.
    assert data["home_optimum"] >= data["home_total"]
    assert data["home_optimum"] == pytest.approx(142.82)


def test_boxscore_carries_fleaflickers_own_points():
    """These totals are what the scoring oracle tests assert against."""
    data = normalize.boxscore(load_fixture("boxscore"))
    names = {
        side["name"]: side["points"]
        for slot in data["slots"]
        for side in (slot["home"], slot["away"])
        if side
    }
    assert names["Justin Fields"] == pytest.approx(31.52)
    assert names["Joe Burrow"] == pytest.approx(10.82)


# --- roster --------------------------------------------------------------


def test_roster_keeps_slot_structure():
    data = normalize.roster(load_fixture("roster"))
    assert data["slots"]
    starters = [s for s in data["slots"] if s["group"] == "START"]
    assert starters
    assert starters[0]["slot"] == "QB"
    assert starters[0]["eligibility"] == ["QB"]


def test_roster_tolerates_empty_slots():
    payload = {"groups": [{"group": "START", "slots": [{"position": {"label": "QB"}}]}]}
    data = normalize.roster(payload)
    assert data["slots"][0]["player"] is None


# --- draft board ---------------------------------------------------------


def test_draft_board_reads_the_true_overall_from_the_upstream():
    """The upstream states the real pick number; never recount it.

    This fixture is a snake. Round 2 column 1 belongs to Team 01 and is
    genuinely pick 28, because a snake reverses. Counting cells left to right
    calls it pick 15, and that wrong number is what a live draft acts on.
    """
    data = normalize.draft_board(load_fixture("draft_board"))
    assert data["draft_type"] == "SNAKE"

    by_overall = {p["overall"]: p for p in data["picks"]}
    assert by_overall[1]["team"] == "Team 01"
    assert by_overall[1]["pick_in_round"] == 1

    # The control: a positional counter would put Team 01 at 15 here.
    assert by_overall[28]["team"] == "Team 01"
    assert by_overall[28]["round"] == 2
    assert by_overall[28]["pick_in_round"] == 14
    assert 15 not in by_overall or by_overall[15]["team"] == "Team 14"


def test_draft_board_is_sorted_by_true_selection_order():
    data = normalize.draft_board(load_fixture("draft_board"))
    overalls = [p["overall"] for p in data["picks"]]
    assert overalls == sorted(overalls)


def test_draft_board_omits_undrafted_seats_but_still_counts_them():
    data = normalize.draft_board(load_fixture("draft_board"))
    # The fixture's last seat (round 3, overall 30) has no player.
    assert data["total_picks"] == 30
    assert data["picks_made"] == 29
    assert all(p["player"] is not None for p in data["picks"])
    assert data["next_overall"] == 30


def test_an_undrafted_season_reports_zero_rather_than_a_wall_of_nulls():
    payload = {"rows": [{"round": 1, "cells": [
        {"team": {"id": 1, "name": "A"}, "slot": {"round": 1, "slot": 1, "overall": 1}},
        {"team": {"id": 2, "name": "B"}, "slot": {"round": 1, "slot": 2, "overall": 2}},
    ]}]}
    data = normalize.draft_board(payload)
    assert data["picks"] == []
    assert data["picks_made"] == 0
    assert data["total_picks"] == 2
    assert data["next_overall"] == 1


def test_since_overall_returns_only_newer_picks():
    data = normalize.draft_board(load_fixture("draft_board"), since_overall=27)
    assert [p["overall"] for p in data["picks"]] == [28, 29]
    assert data["returned"] == 2


def test_the_polling_cursor_describes_the_draft_not_the_slice():
    """A delta poll that reported its own slice would rewind the caller."""
    full = normalize.draft_board(load_fixture("draft_board"))
    delta = normalize.draft_board(load_fixture("draft_board"), since_overall=27)
    assert delta["picks_made"] == full["picks_made"]
    assert delta["next_overall"] == full["next_overall"]
    assert delta["total_picks"] == full["total_picks"]


def test_team_id_filters_to_one_team():
    data = normalize.draft_board(load_fixture("draft_board"), team_id=1)
    assert data["picks"]
    assert {p["team_id"] for p in data["picks"]} == {1}


def test_a_delta_poll_omits_the_unchanging_seat_order():
    """Seat order is fixed for the draft; resending it defeats the delta."""
    full = normalize.draft_board(load_fixture("draft_board"))
    delta = normalize.draft_board(load_fixture("draft_board"), since_overall=27)
    assert "draft_order" in full
    assert "draft_order" not in delta


def test_draft_order_drops_the_zeroed_standings_block():
    data = normalize.draft_board(load_fixture("draft_board"))
    entry = data["draft_order"][0]
    assert set(entry) == {"slot", "id", "name"}
    assert entry["slot"] == 1


def test_a_linear_board_is_not_called_a_snake():
    payload = {"rows": [
        {"round": 1, "cells": [
            {"team": {"id": 1, "name": "A"}, "slot": {"round": 1, "slot": 1, "overall": 1}},
            {"team": {"id": 2, "name": "B"}, "slot": {"round": 1, "slot": 2, "overall": 2}}]},
        {"round": 2, "cells": [
            {"team": {"id": 1, "name": "A"}, "slot": {"round": 2, "slot": 1, "overall": 3}},
            {"team": {"id": 2, "name": "B"}, "slot": {"round": 2, "slot": 2, "overall": 4}}]},
    ]}
    assert normalize.draft_board(payload)["draft_type"] == "LINEAR"


def test_roster_needs_come_from_the_boards_own_roster_block():
    needs = normalize.draft_roster_needs(load_fixture("draft_board"), 1)
    assert needs["team_id"] == 1
    assert needs["rostered"] == 3
    rb = needs["positions"]["RB"]
    assert rb == {"min": 3, "max": 5, "starts": 1, "have": 1,
                  "still_required": 2, "room": 4}
    # QB is at its cap: one rostered, room for one more, none required.
    assert needs["positions"]["QB"]["still_required"] == 0
    assert needs["positions"]["WR"]["still_required"] == 4


def test_roster_needs_keeps_dst_and_drops_flex_slots():
    """"D/ST" has a slash and is real; "RB/WR/TE" has three eligibilities.

    Filtering on the label rather than the eligibility count silently deletes
    the D/ST requirement, which is the one position a draft can forget.
    """
    payload = {"rows": [], "rosters": [{"teamId": 7, "lineup": [
        {"position": {"label": "D/ST", "eligibility": ["D/ST"],
                      "min": 1, "max": 2, "start": 1}},
        {"position": {"label": "RB/WR/TE", "eligibility": ["RB", "WR", "TE"],
                      "min": 0, "max": 0, "start": 1}},
        {"position": {"label": "BN", "eligibility": [], "min": 0, "max": 7}},
    ]}]}
    positions = normalize.draft_roster_needs(payload, 7)["positions"]
    assert set(positions) == {"D/ST"}
    assert positions["D/ST"]["still_required"] == 1


# --- player listing ------------------------------------------------------


def test_player_listing_exposes_the_paging_cursor():
    data = normalize.player_listing(load_fixture("player_listing"))
    assert data["players"]
    assert data["total"] > 0
    player = data["players"][0]
    assert player["name"]
    assert player["position"]
    assert "rank_fantasy" in player


def test_player_listing_next_offset_is_none_when_exhausted():
    data = normalize.player_listing({"players": [], "resultTotal": 0})
    assert data["next_offset"] is None
    assert data["players"] == []


# --- league rules --------------------------------------------------------


def test_league_rules_expose_stat_keys_and_caps():
    payload = load_fixture("league_rules")
    data = normalize.league_rules(payload, RuleSet.from_api(payload))

    assert data["roster"]["starters"] == 8
    assert data["roster"]["max_roster_size"] == 15
    qb = next(p for p in data["roster"]["positions"] if p["label"] == "QB")
    assert qb["roster_max"] == 2

    assert data["scoring"]["rule_count"] == 46
    assert "passing_yard" in data["scoring"]["stat_keys"]
    assert all(
        rule["kind"]
        in {"linear", "total_threshold", "total_range", "per_event"}
        for rule in data["scoring"]["rules"]
    )
