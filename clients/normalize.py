"""Shape raw Fleaflicker payloads into lean dicts.

The raw responses are large and repetitive: a single roster call returns 60 KB,
most of it headshot URLs, colour enums, and a full NFL game object attached to
every player. Handing that to a model wastes context and buries the signal, so
each normaliser keeps the fields a fantasy decision actually turns on and drops
the rest.

Every accessor tolerates missing keys, because the upstream omits any field
whose value is zero, false, or empty. ``_num`` and ``_points`` centralise that:
absent and zero are the same thing, and both come back as ``0``.
"""

from __future__ import annotations

from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    """Read a number that may be absent (omitted zero) or wrapped.

    Fleaflicker wraps numbers as ``{"value": n, "formatted": "n"}``, and nests
    that wrapper to different depths per endpoint: a scoreboard score is one
    level deep, a boxscore total is two. Unwrap ``value`` until a scalar falls
    out rather than hardcoding a depth per call site.
    """
    for _ in range(4):  # bounded: no real payload nests deeper
        if not isinstance(value, dict):
            break
        value = value.get("value")
    if value is None or isinstance(value, dict):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    return int(_num(value, default))


def _points(value: Any) -> float:
    """Fleaflicker points arrive as ``{"value": 22.04, "formatted": "22.04"}``."""
    return round(_num(value), 2)


def _record(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    return {
        "wins": _int(raw.get("wins")),
        "losses": _int(raw.get("losses")),
        "ties": _int(raw.get("ties")),
        "win_percentage": round(_num(raw.get("winPercentage")), 3),
        "formatted": raw.get("formatted", ""),
    }


def team(raw: dict[str, Any] | None) -> dict[str, Any]:
    """A team stub: identity, record, and points for/against."""
    raw = raw or {}
    owners = raw.get("owners") or []
    return {
        "id": _int(raw.get("id")),
        "name": raw.get("name", ""),
        "owner": (owners[0].get("displayName", "") if owners else ""),
        "record": _record(raw.get("recordOverall")),
        "points_for": _points(raw.get("pointsFor")),
        "points_against": _points(raw.get("pointsAgainst")),
        "streak": (raw.get("streak") or {}).get("formatted", ""),
        "draft_position": _int(raw.get("draftPosition")),
        "waiver_position": _int(raw.get("waiverPosition")),
    }


def player(raw: dict[str, Any] | None) -> dict[str, Any]:
    """A pro player: who he is, where he plays, and his bye."""
    raw = raw or {}
    return {
        "id": _int(raw.get("id")),
        "name": raw.get("nameFull", ""),
        "position": raw.get("position", ""),
        "pro_team": raw.get("proTeamAbbreviation", ""),
        "bye_week": _int(raw.get("nflByeWeek")),
        "percent_owned": round(_num(raw.get("percentOwnedRatio")) * 100, 1),
    }


def _rank(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Overall and positional rank, when the listing carries one."""
    raw = raw or {}
    overall = raw.get("ordinal")
    positions = raw.get("positions") or []
    positional = positions[0].get("ordinal") if positions else None
    if overall is None and positional is None:
        return None
    return {
        "overall": _int(overall) or None,
        "positional": _int(positional) or None,
    }


def league_player(raw: dict[str, Any] | None) -> dict[str, Any]:
    """A player as the league sees him: identity plus fantasy production."""
    raw = raw or {}
    out = player(raw.get("proPlayer"))
    out.update(
        {
            "points": _points(raw.get("viewingActualPoints")),
            "season_total": _points(raw.get("seasonTotal")),
            "season_average": _points(raw.get("seasonAverage")),
        }
    )
    owner = raw.get("owner")
    out["owned_by"] = (owner or {}).get("name", "") if owner else ""

    fantasy_rank = _rank(raw.get("rankFantasy"))
    draft_rank = _rank(raw.get("rankDraft"))
    if fantasy_rank:
        out["rank_fantasy"] = fantasy_rank
    if draft_rank:
        out["rank_draft"] = draft_rank

    stats = raw.get("viewingActualStats") or []
    if stats:
        out["stats"] = {
            (s.get("category") or {}).get("nameSingular", "?"): _num(s.get("value"))
            for s in stats
        }
    return out


def roster_position(raw: dict[str, Any] | None) -> dict[str, Any]:
    """A lineup slot definition, including the league's roster caps."""
    raw = raw or {}
    return {
        "label": raw.get("label", ""),
        "group": raw.get("group", ""),
        "eligibility": list(raw.get("eligibility") or []),
        "starts": _int(raw.get("start")),
        "roster_min": _int(raw.get("min")),
        "roster_max": _int(raw.get("max")),
    }


def standings(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten divisions into a single ranked team list."""
    teams: list[dict[str, Any]] = []
    for division in payload.get("divisions") or []:
        division_name = division.get("name", "")
        for raw_team in division.get("teams") or []:
            entry = team(raw_team)
            entry["division"] = division_name
            teams.append(entry)

    teams.sort(key=lambda t: (-t["record"]["wins"], -t["points_for"]))
    for index, entry in enumerate(teams, start=1):
        entry["rank"] = index

    league = payload.get("league") or {}
    return {
        "league": {
            "id": _int(league.get("id")),
            "name": league.get("name", ""),
            "size": _int(league.get("size")) or len(teams),
        },
        "season": _int(payload.get("season")) or None,
        "teams": teams,
    }


def matchups(payload: dict[str, Any]) -> dict[str, Any]:
    """Scoreboard games, with both scores lifted out of their wrappers."""
    games = []
    for raw in payload.get("games") or []:
        games.append(
            {
                "id": _int(raw.get("id")),
                "home": team(raw.get("home")),
                "away": team(raw.get("away")),
                "home_score": _points((raw.get("homeScore") or {}).get("score")),
                "away_score": _points((raw.get("awayScore") or {}).get("score")),
                "home_result": raw.get("homeResult", ""),
                "away_result": raw.get("awayResult", ""),
                "is_final": bool(raw.get("isFinalScore", False)),
            }
        )
    period = payload.get("schedulePeriod") or {}
    return {
        "week": _int(period.get("ordinal") if isinstance(period, dict) else None) or None,
        "games": games,
    }


def _slots(groups: list[dict[str, Any]], sides: tuple[str, ...]) -> list[dict[str, Any]]:
    """Walk lineup groups into flat slot rows.

    ``sides`` is ``("leaguePlayer",)`` for a single roster and
    ``("home", "away")`` for a head-to-head boxscore, which is the only
    structural difference between the two payloads.
    """
    rows: list[dict[str, Any]] = []
    for group in groups or []:
        group_label = group.get("group") or ""
        for slot in group.get("slots") or []:
            position = slot.get("position") or {}
            row: dict[str, Any] = {
                "slot": position.get("label", ""),
                "group": group_label,
                "eligibility": list(position.get("eligibility") or []),
            }
            for side in sides:
                occupant = slot.get(side)
                key = "player" if side == "leaguePlayer" else side
                row[key] = league_player(occupant) if occupant else None
            rows.append(row)
    return rows


def roster(payload: dict[str, Any]) -> dict[str, Any]:
    """One team's lineup, starters first."""
    return {"slots": _slots(payload.get("groups") or [], ("leaguePlayer",))}


def boxscore(payload: dict[str, Any]) -> dict[str, Any]:
    """A head-to-head matchup with both lineups side by side.

    ``home_total`` and ``away_total`` are Fleaflicker's own computed totals,
    which makes them the authoritative check on anything the scoring engine
    produces for the same players.

    ``*_optimum`` is what the roster would have scored with a perfect lineup.
    The gap between it and the actual total is points left on the bench, which
    is the only start/sit feedback the platform publishes.
    """
    game = payload.get("game") or {}
    period = payload.get("scoringPeriod") or {}
    home_points = (payload.get("pointsHome") or {}).get("total") or {}
    away_points = (payload.get("pointsAway") or {}).get("total") or {}
    return {
        "game_id": _int(game.get("id")),
        "week": _int(period.get("ordinal")) or None,
        "home": team(game.get("home")),
        "away": team(game.get("away")),
        "home_total": _points(home_points.get("value")),
        "away_total": _points(away_points.get("value")),
        "home_optimum": _points(home_points.get("optimum")),
        "away_optimum": _points(away_points.get("optimum")),
        "is_final": bool(game.get("isFinalScore", False)),
        "slots": _slots(payload.get("lineups") or [], ("home", "away")),
    }


def draft_board(payload: dict[str, Any]) -> dict[str, Any]:
    """Every pick, flattened to a round-by-round list.

    Overall pick number is computed from position rather than read from the
    payload, which does not carry one.
    """
    rounds = []
    overall = 0
    for raw_round in payload.get("rows") or []:
        picks = []
        for index, cell in enumerate(raw_round.get("cells") or [], start=1):
            overall += 1
            drafted = cell.get("player")
            picks.append(
                {
                    "overall": overall,
                    "pick_in_round": index,
                    "team": (cell.get("team") or {}).get("name", ""),
                    "team_id": _int((cell.get("team") or {}).get("id")),
                    "player": player((drafted or {}).get("proPlayer")) if drafted else None,
                }
            )
        rounds.append({"round": _int(raw_round.get("round")), "picks": picks})

    # draftOrder is a bare list of team stubs, not a {"teams": [...]} envelope.
    raw_order = payload.get("draftOrder") or []
    if isinstance(raw_order, dict):
        raw_order = raw_order.get("teams") or []

    return {
        "rounds": rounds,
        "total_picks": overall,
        "draft_order": [team(entry) for entry in raw_order],
    }


def player_listing(payload: dict[str, Any]) -> dict[str, Any]:
    """A page of players, with the cursor needed to fetch the next one."""
    players = [league_player(p) for p in payload.get("players") or []]
    next_offset = payload.get("resultOffsetNext")
    return {
        "players": players,
        "total": _int(payload.get("resultTotal")),
        "next_offset": _int(next_offset) if next_offset is not None else None,
    }


def league_rules(payload: dict[str, Any], rule_set: Any) -> dict[str, Any]:
    """Roster construction plus the full scoring rule set."""
    return {
        "roster": {
            "starters": _int(payload.get("numStarters")),
            "bench": _int(payload.get("numBench")),
            "max_active": _int(payload.get("maxActive")),
            "max_roster_size": _int(payload.get("maxRosterSize")),
            "positions": [
                roster_position(p) for p in payload.get("rosterPositions") or []
            ],
        },
        "scoring": {
            "rule_count": len(rule_set.rules),
            "stat_keys": rule_set.stat_keys(),
            "rules": [
                {
                    "category": rule.category.key,
                    "category_id": rule.category.id,
                    "group": rule.group,
                    "points": rule.points,
                    "kind": rule.kind,
                    "multi_value": rule.category.multi_value,
                    "applies_to": (
                        "ALL" if rule.apply_to_all else sorted(rule.apply_to)
                    ),
                    "description": rule.description,
                }
                for rule in rule_set.rules
            ],
        },
    }
