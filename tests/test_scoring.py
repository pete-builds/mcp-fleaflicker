"""Scoring engine tests.

The oracle tests at the bottom are the important ones: they score a stat line
and compare the result against the number Fleaflicker itself computed for that
player in that game. Everything above them tests a single rule kind in
isolation so a failure points at the rule, not just at the total.
"""

from __future__ import annotations

import math

import pytest

from clients.errors import FleaflickerInputError
from clients.scoring import (
    LINEAR,
    PER_EVENT,
    TOTAL_RANGE,
    TOTAL_THRESHOLD,
    RuleSet,
    score_stats,
    slugify,
)


def score(rules: RuleSet, stats: dict, position: str = "WR") -> float:
    return score_stats(rules, stats, position).total


# --- parsing -------------------------------------------------------------


def test_parses_every_scored_rule(rules: RuleSet):
    # 46 rules across 7 groups; "Punting" ships with no scoringRules key at all.
    assert len(rules.rules) == 46
    assert {r.group for r in rules.rules} == {
        "Passing",
        "Rushing",
        "Receiving",
        "Misc",
        "Kicking",
        "Returning",
        "Defense",
    }


def test_rule_kinds_are_classified(rules: RuleSet):
    kinds = {r.kind for r in rules.rules}
    assert kinds == {LINEAR, TOTAL_THRESHOLD, TOTAL_RANGE, PER_EVENT}


def test_slugify():
    assert slugify("Passing Yard") == "passing_yard"
    assert slugify("2 Pt Conversion Passing") == "2_pt_conversion_passing"
    assert slugify("D/ST") == "d_st"


@pytest.mark.parametrize("alias", ["passing_yard", "passing_yards", "3"])
def test_stat_keys_accept_aliases(rules: RuleSet, alias: str):
    """Singular slug, natural plural, and numeric category id all resolve."""
    assert rules.resolve(alias)


# --- ambiguity -----------------------------------------------------------


def test_interception_is_ambiguous_and_refuses_to_guess(rules: RuleSet):
    """The collision that would otherwise net two picks to exactly zero.

    "Interception" is a Passing category worth -2 (the quarterback threw it)
    and a Defense category worth +2 (the defense caught it). Resolving a bare
    `interception` to both would return 0.0 — a confident, wrong number.
    """
    with pytest.raises(FleaflickerInputError) as exc:
        rules.resolve("interception")
    message = str(exc.value)
    assert "ambiguous" in message
    assert "passing_interception" in message
    assert "defense_interception" in message


def test_qualified_interception_keys_score_correctly(rules: RuleSet):
    assert score(rules, {"passing_interception": 2}, "QB") == pytest.approx(-4.0)
    assert score(rules, {"defense_interception": 2}, "D/ST") == pytest.approx(4.0)


@pytest.mark.parametrize("abbrev", ["td", "yd", "int", "2pc", "ty"])
def test_ambiguous_abbreviations_are_refused(rules: RuleSet, abbrev: str):
    """Abbreviations repeat across groups; none of them may resolve silently."""
    with pytest.raises(FleaflickerInputError):
        rules.resolve(abbrev)


def test_unambiguous_abbreviation_still_works(rules: RuleSet):
    # "Cmp" appears only in Passing, so it is safe to accept.
    assert rules.resolve("cmp")


def test_stat_keys_are_all_unambiguous(rules: RuleSet):
    """Every advertised key must resolve. This is the tool's public contract."""
    for key in rules.stat_keys():
        assert rules.resolve(key), f"advertised key {key!r} does not resolve"


def test_ambiguous_keys_are_published(rules: RuleSet):
    assert rules.ambiguous_keys["interception"] == [
        "defense_interception",
        "passing_interception",
    ]


# --- linear rules --------------------------------------------------------


def test_linear_rate_uses_for_every(rules: RuleSet):
    # 1 point per 25 passing yards.
    assert score(rules, {"passing_yard": 250}, "QB") == pytest.approx(10.0)


def test_linear_flat_per_event(rules: RuleSet):
    # 6 points per passing TD, and no distance bonus for a bare count.
    assert score(rules, {"passing_td": 2}, "QB") == pytest.approx(12.0)


def test_negative_rules_subtract(rules: RuleSet):
    assert score(rules, {"passing_interception": 2}, "QB") == pytest.approx(-4.0)


def test_fumble_lost_is_double_taxed(rules: RuleSet):
    # A lost fumble trips both "Fumble" (-1) and "Fumble Lost" (-1).
    result = score_stats(rules, {"fumble": 1, "fumble_lost": 1}, "RB")
    assert result.total == pytest.approx(-2.0)


# --- threshold bonuses (single-valued categories) ------------------------


def test_total_threshold_fires_once_at_the_bound(rules: RuleSet):
    # 300+ passing yards is +1, exactly once, no matter how far past.
    at_bound = score(rules, {"passing_yard": 300}, "QB")
    below = score(rules, {"passing_yard": 299}, "QB")
    assert at_bound - below == pytest.approx(1.0 + 0.04)


def test_total_threshold_does_not_scale(rules: RuleSet):
    # 600 yards is still one +1 bonus, not two.
    assert score(rules, {"passing_yard": 600}, "QB") == pytest.approx(24.0 + 1.0)


def test_reception_bonus_at_nine_catches(rules: RuleSet):
    eight = score(rules, {"catch": 8}, "WR")
    nine = score(rules, {"catch": 9}, "WR")
    assert eight == pytest.approx(8.0)
    assert nine == pytest.approx(9.0 + 2.0)


# --- per-event bonuses (multi-valued categories) -------------------------


def test_per_event_bonus_needs_a_list(rules: RuleSet):
    """A count cannot answer "how long was each one", and we say so."""
    result = score_stats(rules, {"passing_td": 3}, "QB")
    assert result.total == pytest.approx(18.0)
    assert any("passing_td" in note for note in result.unresolved_bonuses)


def test_per_event_bonus_scores_from_a_list(rules: RuleSet):
    # Two TDs, one of 45 yards (40-79 → +2) and one of 12 (no bonus).
    result = score_stats(rules, {"passing_td": [45, 12]}, "QB")
    assert result.total == pytest.approx(12.0 + 2.0)
    assert result.unresolved_bonuses == []


def test_per_event_bonus_applies_per_qualifying_event(rules: RuleSet):
    # Three 40-to-79 yard TDs is +2 three times, not +2 once.
    result = score_stats(rules, {"rushing_td": [45, 50, 60]}, "RB")
    assert result.total == pytest.approx(18.0 + 6.0)


def test_eighty_plus_is_a_per_event_lower_bound(rules: RuleSet):
    """The 80+ rule is RANGE_LOWER_BOUND on a multiValue category.

    That means "each TD of 80+ yards", not "fires once if the total clears 80".
    A single 85-yard TD earns 6 + 4; it must not also earn the 40-to-79 bonus.
    """
    result = score_stats(rules, {"receiving_td": [85]}, "WR")
    assert result.total == pytest.approx(6.0 + 4.0)


def test_long_td_bands_do_not_overlap(rules: RuleSet):
    # 79 sits in the 40-79 band; 80 crosses into the 80+ band.
    assert score(rules, {"rushing_td": [79]}, "RB") == pytest.approx(6.0 + 2.0)
    assert score(rules, {"rushing_td": [80]}, "RB") == pytest.approx(6.0 + 4.0)


# --- the omitted-zero trap ----------------------------------------------


def test_shutout_bonus_bounds_default_to_zero(rules: RuleSet):
    """The regression this whole module exists to prevent.

    The shutout rule ("10 extra points when total Points Allowed is exactly 0")
    arrives from the API with **no** boundLower and **no** boundUpper key,
    because the API is protobuf-derived and omits zero-valued fields. Default a
    missing boundUpper to infinity and every defense scores +10 every week.
    """
    shutout = [r for r in rules.rules if r.category.id == 94]
    assert len(shutout) == 1
    rule = shutout[0]
    assert rule.kind == TOTAL_RANGE
    assert rule.bound_lower == 0
    assert rule.bound_upper == 0
    assert not math.isinf(rule.bound_upper)


def test_shutout_scores_only_on_an_actual_shutout(rules: RuleSet):
    assert score(rules, {"point_allowed": 0}, "D/ST") == pytest.approx(10.0)
    assert score(rules, {"point_allowed": 1}, "D/ST") == pytest.approx(0.0)
    assert score(rules, {"point_allowed": 24}, "D/ST") == pytest.approx(0.0)


def test_lower_bound_rules_stay_unbounded_above(rules: RuleSet):
    # RANGE_LOWER_BOUND genuinely has no upper edge, unlike RANGE_DOUBLE_BOUND.
    three_hundred = [
        r for r in rules.rules if r.category.id == 3 and r.kind == TOTAL_THRESHOLD
    ]
    assert math.isinf(three_hundred[0].bound_upper)


# --- position restrictions ----------------------------------------------


def test_receptions_are_skill_positions_only(rules: RuleSet):
    # "Catch" applies to QB/RB/WR/TE, so a defense scores nothing for it. The
    # 9-catch bonus is flagged ALL and would apply, but 5 is under the bound.
    assert score(rules, {"catch": 5}, "WR") == pytest.approx(5.0)
    assert score(rules, {"catch": 5}, "D/ST") == pytest.approx(0.0)


def test_fumble_recovery_is_defense_only(rules: RuleSet):
    assert score(rules, {"fumble_recovered": 2}, "D/ST") == pytest.approx(2.0)
    assert score(rules, {"fumble_recovered": 2}, "RB") == pytest.approx(0.0)


def test_apply_to_all_overrides_the_subset_list(rules: RuleSet):
    """Fleaflicker flags some rules ALL while still listing a subset.

    The league UI shows ALL, so ALL is what we honour. Harmless in practice —
    a defense records no catches — but it must match the published rule.
    """
    nine_catch = next(
        r for r in rules.rules if r.category.id == 41 and r.kind == TOTAL_THRESHOLD
    )
    assert nine_catch.apply_to_all
    assert nine_catch.applies_to("D/ST")


# --- input handling ------------------------------------------------------


def test_unknown_keys_are_reported_not_silently_dropped(rules: RuleSet):
    result = score_stats(rules, {"passing_yard": 100, "touchdownz": 3}, "QB")
    assert result.unscored_keys == ["touchdownz"]
    assert result.total == pytest.approx(4.0)


def test_breakdown_explains_the_arithmetic(rules: RuleSet):
    result = score_stats(rules, {"passing_yard": 250}, "QB")
    entry = next(b for b in result.breakdown if b.category == "passing_yard")
    assert entry.detail == "250 / 25 x 1"
    assert entry.points == pytest.approx(10.0)


def test_zero_valued_rules_are_omitted_from_the_breakdown(rules: RuleSet):
    # A 100-yard game evaluates the 300-yard rule but contributes nothing,
    # so it should not clutter the breakdown.
    payload = score_stats(rules, {"passing_yard": 100}, "QB").as_dict()
    assert all(entry["points"] != 0 for entry in payload["breakdown"])


@pytest.mark.parametrize("bad", ["not-a-number", True, {"a": 1}, None])
def test_bad_stat_values_are_rejected(rules: RuleSet, bad):
    with pytest.raises(FleaflickerInputError):
        score_stats(rules, {"passing_yard": bad}, "QB")


def test_bad_event_list_is_rejected(rules: RuleSet):
    with pytest.raises(FleaflickerInputError):
        score_stats(rules, {"passing_td": [40, "long"]}, "QB")


def test_stats_must_be_a_mapping(rules: RuleSet):
    with pytest.raises(FleaflickerInputError):
        score_stats(rules, [("passing_yard", 100)], "QB")  # type: ignore[arg-type]


# --- oracle: match Fleaflicker's own computed points ---------------------
#
# Both lines below are real players from the real league, week 1 of 2025,
# reconstructed from tests/fixtures/boxscore.json. The expected totals are the
# `viewingActualPoints` values Fleaflicker itself published for that game, so
# these assert against the platform rather than against our own arithmetic.


def test_oracle_justin_fields_week1_2025(rules: RuleSet):
    """Fleaflicker computed 31.52 for this line."""
    total = score(
        rules,
        {
            "passing_yard": 218,  # 218 * 0.04 = 8.72
            "passing_td": [1, 1, 1],  # 3 * 6 = 18, none long enough to bonus
            "rushing_yard": 48,  # 48 * 0.1 = 4.8
        },
        "QB",
    )
    assert total == pytest.approx(31.52, abs=0.01)


def test_oracle_joe_burrow_week1_2025(rules: RuleSet):
    """Fleaflicker computed 10.82 for this line."""
    total = score(
        rules,
        {
            "passing_yard": 113,  # 4.52
            "passing_td": [1],  # 6.0
            "rushing_yard": 3,  # 0.3
        },
        "QB",
    )
    assert total == pytest.approx(10.82, abs=0.01)


def test_oracle_totals_match_the_fixture(rules: RuleSet):
    """Guard the oracle itself: if the fixture changes, these tests must too."""
    from tests.conftest import load_fixture

    box = load_fixture("boxscore")
    published = {}
    for lineup in box["lineups"]:
        for slot in lineup["slots"]:
            for side in ("home", "away"):
                entry = slot.get(side)
                if entry:
                    name = entry["proPlayer"]["nameFull"]
                    published[name] = entry["viewingActualPoints"]["value"]

    assert published["Justin Fields"] == pytest.approx(31.52)
    assert published["Joe Burrow"] == pytest.approx(10.82)
