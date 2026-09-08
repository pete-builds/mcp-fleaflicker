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


def _projected(raw: dict[str, Any] | None) -> float:
    """The league's own projected points per game for a listing entry.

    ``viewingProjectedPoints`` is computed by Fleaflicker under THIS league's
    scoring rules, so it already reflects superflex, 6-point passing TDs, and
    every bonus. It sits on the listing entry, not on ``proPlayer``.
    """
    return _points((raw or {}).get("viewingProjectedPoints"))


def _rank(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Overall and positional rank, when the listing carries one."""
    raw = raw or {}
    overall = raw.get("ordinal")
    positions = raw.get("positions") or []
    positional = positions[0].get("ordinal") if positions else None
    if overall is None and positional is None:
        return None
    out: dict[str, Any] = {
        "overall": _int(overall) or None,
        "positional": _int(positional) or None,
    }
    if positions:
        # "QB29" and RATING_VERY_BAD are how the room sees him on the default
        # board, which is exactly the number a mispricing read turns on.
        if positions[0].get("formatted"):
            out["label"] = positions[0]["formatted"]
        if positions[0].get("rating"):
            out["rating"] = positions[0]["rating"]
    return out


def league_player(raw: dict[str, Any] | None) -> dict[str, Any]:
    """A player as the league sees him: identity plus fantasy production."""
    raw = raw or {}
    out = player(raw.get("proPlayer"))
    out.update(
        {
            "points": _points(raw.get("viewingActualPoints")),
            "projected_points": _projected(raw),
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


def _draft_slot(cell: dict[str, Any]) -> dict[str, int]:
    """The cell's TRUE position in the draft, as the upstream states it.

    Each cell carries ``slot`` = ``{"round", "slot", "overall"}`` and that
    ``overall`` is the real selection number, already snake-aware: in a snake,
    round 2 column 1 is ``overall 28``, not ``overall 15``.

    This used to be recomputed by counting cells left-to-right, which is the
    grid layout and NOT the pick order. On 2026-09-07 that cost a live draft
    several minutes of inferring the snake from which cells were empty, because
    the board rendered every team at its static column while picks were landing
    in reverse. Read the field; never count.
    """
    slot = cell.get("slot") or {}
    return {
        "round": _int(slot.get("round")),
        "pick_in_round": _int(slot.get("slot")),
        "overall": _int(slot.get("overall")),
    }


def _draft_type(rows: list[dict[str, Any]]) -> str:
    """SNAKE, LINEAR, or UNKNOWN, decided from the stated pick numbers.

    A snake reverses even rounds, so the ``pick_in_round`` sequence runs
    backwards relative to grid order. One reversed round is enough to decide.
    """
    for raw_round in rows:
        cells = raw_round.get("cells") or []
        if len(cells) < 2:
            continue
        seq = [_draft_slot(c)["pick_in_round"] for c in cells]
        if 0 in seq:
            continue
        if seq == sorted(seq, reverse=True) and seq != sorted(seq):
            return "SNAKE"
    return "LINEAR" if rows else "UNKNOWN"


def draft_board(
    payload: dict[str, Any],
    *,
    since_overall: int | None = None,
    team_id: int | None = None,
) -> dict[str, Any]:
    """Every pick as one flat list in TRUE selection order.

    Undrafted cells are omitted rather than returned as ``player: null``. A
    fifteen-round board is 210 cells, so before the draft starts the old shape
    spent its entire payload saying "nothing has happened"; ``picks_made: 0``
    says the same thing in one field. Check ``picks_made``, not the length of
    ``picks``, to decide whether a season has drafted.

    ``since_overall`` makes this pollable: pass back the previous call's
    ``next_overall - 1`` and only newer picks come over the wire.
    """
    picks: list[dict[str, Any]] = []
    total = 0
    for raw_round in payload.get("rows") or []:
        for cell in raw_round.get("cells") or []:
            total += 1
            position = _draft_slot(cell)
            drafted = cell.get("player")
            if not drafted:
                continue
            picks.append(
                {
                    **position,
                    "team": (cell.get("team") or {}).get("name", ""),
                    "team_id": _int((cell.get("team") or {}).get("id")),
                    "player": player(drafted.get("proPlayer")),
                }
            )

    picks.sort(key=lambda p: p["overall"])
    picks_made = len(picks)
    next_overall = picks[-1]["overall"] + 1 if picks else 1

    # Filter AFTER counting, so picks_made and next_overall always describe the
    # whole draft. A delta poll that reported its own slice as the draft state
    # would rewind the caller's cursor on every call.
    if since_overall is not None:
        picks = [p for p in picks if p["overall"] > since_overall]
    if team_id is not None:
        picks = [p for p in picks if p["team_id"] == team_id]

    # draftOrder is a bare list of team stubs, not a {"teams": [...]} envelope.
    raw_order = payload.get("draftOrder") or []
    if isinstance(raw_order, dict):
        raw_order = raw_order.get("teams") or []
    # Only identity is kept. The full team stub carries a record, points for and
    # against, and a streak, every one of them zero before week 1, which is 14
    # teams' worth of zeros on every poll of a live draft.
    order = [
        {"slot": index, "id": _int(entry.get("id")), "name": entry.get("name", "")}
        for index, entry in enumerate(raw_order, start=1)
    ]

    return {
        "draft_type": _draft_type(payload.get("rows") or []),
        "total_picks": total,
        "picks_made": picks_made,
        "next_overall": next_overall,
        "returned": len(picks),
        "picks": picks,
        "draft_order": order,
    }


def draft_roster_needs(payload: dict[str, Any], team_id: int) -> dict[str, Any]:
    """What one team still has to draft, from the board's own roster block.

    ``FetchLeagueDraftBoard`` ships a ``rosters`` array carrying each team's
    live lineup with every slot's ``min``/``max``/``start``. That is the roster
    cap arithmetic the draft actually turns on, and it was previously being
    tracked by hand in a markdown table.
    """
    for entry in payload.get("rosters") or []:
        if _int(entry.get("teamId")) != team_id:
            continue
        counts: dict[str, int] = {}
        limits: dict[str, dict[str, int]] = {}
        filled = 0
        for slot in entry.get("lineup") or []:
            position = slot.get("position") or {}
            label = position.get("label", "")
            # Only real roster positions carry a min/max cap. Flex slots and
            # bench slots never match a player's position, so they would report
            # have: 0 forever and read as an unfilled need. Decide on
            # eligibility count, not on the label: "D/ST" has a slash and is a real
            # real position, while "RB/WR/TE" lists three.
            countable = label and len(position.get("eligibility") or []) == 1
            if countable and label not in limits:
                limits[label] = {
                    "min": _int(position.get("min")),
                    "max": _int(position.get("max")),
                    "starts": _int(position.get("start")),
                }
            drafted = slot.get("player")
            if drafted:
                filled += 1
                actual = (drafted.get("proPlayer") or {}).get("position", label)
                counts[actual] = counts.get(actual, 0) + 1
        needs = {
            label: {
                **bounds,
                "have": counts.get(label, 0),
                "still_required": max(0, bounds["min"] - counts.get(label, 0)),
                "room": max(0, bounds["max"] - counts.get(label, 0)),
            }
            for label, bounds in limits.items()
        }
        return {"team_id": team_id, "rostered": filled, "positions": needs}
    return {"team_id": team_id, "rostered": 0, "positions": {}}


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
