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


def test_draft_board_computes_overall_pick_numbers():
    data = normalize.draft_board(load_fixture("draft_board"))
    assert data["rounds"]
    first = data["rounds"][0]["picks"]
    assert first[0]["overall"] == 1
    assert first[0]["pick_in_round"] == 1
    assert first[0]["player"]["name"]
    # Overall numbering continues across the round boundary.
    if len(data["rounds"]) > 1:
        second = data["rounds"][1]["picks"]
        assert second[0]["overall"] == len(first) + 1


def test_draft_board_handles_an_undrafted_slot():
    payload = {"rows": [{"round": 1, "cells": [{"team": {"id": 1, "name": "A"}}]}]}
    data = normalize.draft_board(payload)
    assert data["rounds"][0]["picks"][0]["player"] is None
    assert data["total_picks"] == 1


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
