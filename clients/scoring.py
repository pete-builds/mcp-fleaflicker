"""The scoring engine: turn a league's published rules into fantasy points.

Nothing here is specific to any one league. Every rule, threshold, bonus, and
position restriction is parsed from what ``FetchLeagueRules`` actually returns,
so the same code scores a 6-point-passing-TD superflex league and a standard
half-PPR league without a branch.

Three things about Fleaflicker's rule model are load-bearing and were each
verified against the live API rather than assumed:

1. **Zero-valued fields are omitted from the JSON.** The API is protobuf-derived
   and serializes defaults by leaving them out. The shutout rule ("10 extra
   points when total Points Allowed is exactly 0") arrives with *no*
   ``boundLower`` and *no* ``boundUpper`` key, because both are ``0``. Default a
   missing bound to infinity and every defense scores the shutout bonus every
   week. Missing bounds therefore mean **zero**, never "unbounded".

2. **``RANGE_LOWER_BOUND`` is two different rules wearing one name.** Which one
   depends on ``category.multiValue``. On a single-valued category it is a
   once-per-game threshold on the summed total (300 passing yards → +1, once,
   no matter how far past 300). On a multi-valued category it fires once per
   qualifying *event* (every touchdown of 80+ yards → +4 each).

3. **A touchdown is two categories, not one.** The flat 6 points and the
   long-distance bonus live under separate category ids that share a
   ``nameSingular`` within a group ("Passing TD" is both id 5 and id 6). They
   are linked here by (group, name) so one caller-supplied stat key feeds both.

Per-game, not per-season
------------------------
Every threshold rule fires on a single game's box score. A receiver with 2,000
yards across 17 games earns the 150-yard bonus only in the games he actually
cleared 150. Feed this engine one game at a time; summing a season first
silently miscounts every bonus in the rule set.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from clients.errors import FleaflickerInputError

# Rule kinds, derived from (rangeType, category.multiValue).
LINEAR = "linear"
TOTAL_THRESHOLD = "total_threshold"
TOTAL_RANGE = "total_range"
PER_EVENT = "per_event"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """``"2 Pt Conversion Passing"`` -> ``"2_pt_conversion_passing"``."""
    return _SLUG_STRIP.sub("_", name.strip().lower()).strip("_")


def _plural_aliases(slug: str) -> set[str]:
    """Accept the plural a human would naturally type.

    ``passing_td`` also answers to ``passing_tds``; ``catch`` also answers to
    ``catches``. Purely additive convenience over the canonical singular slug.
    """
    out = {slug}
    if slug.endswith(("s", "x", "z", "ch", "sh")):
        out.add(f"{slug}es")
    elif slug.endswith("y") and len(slug) > 1 and slug[-2] not in "aeiou":
        out.add(f"{slug[:-1]}ies")
    else:
        out.add(f"{slug}s")
    return out


@dataclass(frozen=True)
class Category:
    """A scoring category, e.g. "Passing Yard" or "Solo Tackle"."""

    id: int
    abbreviation: str
    name_singular: str
    name_plural: str
    multi_value: bool = False
    lower_is_better: bool = False

    @property
    def key(self) -> str:
        """Canonical stat key: the slugified singular name."""
        return slugify(self.name_singular)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Category:
        name_singular = raw.get("nameSingular") or raw.get("abbreviation") or ""
        return cls(
            id=int(raw.get("id", 0)),
            abbreviation=raw.get("abbreviation", ""),
            name_singular=name_singular,
            name_plural=raw.get("namePlural") or name_singular,
            # Absent means False — protobuf omits zero/false.
            multi_value=bool(raw.get("multiValue", False)),
            lower_is_better=bool(raw.get("lowerIsBetter", False)),
        )


@dataclass(frozen=True)
class Rule:
    """One scoring rule, classified into one of four evaluable kinds."""

    category: Category
    group: str
    points: float
    kind: str
    for_every: float = 1.0
    bound_lower: float = 0.0
    bound_upper: float = 0.0
    apply_to_all: bool = False
    apply_to: frozenset[str] = field(default_factory=frozenset)
    is_bonus: bool = False
    description: str = ""

    def applies_to(self, position: str) -> bool:
        """Does this rule score for a player at ``position``?

        ``applyToAll`` wins outright. Fleaflicker sets it on rules whose
        ``applyTo`` list still names a subset (the 9-catch bonus is flagged ALL
        while listing only QB/RB/WR/TE); the flag is what the league UI shows,
        so it is what we honour. Harmless in practice — a defense records no
        catches — and it keeps us faithful to the published rule.
        """
        if self.apply_to_all or not self.apply_to:
            return True
        return position.upper() in self.apply_to

    @classmethod
    def from_api(cls, raw: dict[str, Any], group: str) -> Rule:
        category = Category.from_api(raw.get("category") or {})
        range_type = raw.get("rangeType")

        if range_type == "RANGE_LOWER_BOUND":
            kind = PER_EVENT if category.multi_value else TOTAL_THRESHOLD
        elif range_type == "RANGE_DOUBLE_BOUND":
            kind = PER_EVENT if category.multi_value else TOTAL_RANGE
        else:
            kind = LINEAR

        # A missing bound is ZERO, not unbounded. See module docstring.
        bound_lower = float(raw.get("boundLower") or 0)
        # For a lower-bound rule the upper edge really is unbounded; for a
        # double-bound rule an absent upper edge means the literal value 0.
        if range_type == "RANGE_LOWER_BOUND":
            bound_upper = math.inf
        else:
            bound_upper = float(raw.get("boundUpper") or 0)

        # Guard the divisor: "for every N" with N absent means N == 1, and a
        # zero would be a division fault on a hot path.
        for_every = float(raw.get("forEvery") or 1) or 1.0

        return cls(
            category=category,
            group=group,
            points=float((raw.get("points") or {}).get("value", 0.0)),
            kind=kind,
            for_every=for_every,
            bound_lower=bound_lower,
            bound_upper=bound_upper,
            apply_to_all=bool(raw.get("applyToAll", False)),
            apply_to=frozenset(p.upper() for p in raw.get("applyTo") or []),
            is_bonus=bool(raw.get("isBonus", False)),
            description=raw.get("description", ""),
        )


@dataclass
class RuleSet:
    """Every scoring rule in a league, indexed for lookup by stat key.

    Names are not unique across groups. "Interception" is a passing category
    worth -2 (a quarterback threw it) *and* a defensive category worth +2 (a
    defense caught it). Resolving a bare ``interception`` to both would net two
    picks to exactly zero and report a confident, wrong number — the worst
    possible failure for a scoring engine.

    So any alias that reaches more than one group is registered as **ambiguous**
    and refuses to resolve, naming the qualified alternatives instead. Colliding
    categories additionally get a group-qualified key (``passing_interception``,
    ``defense_interception``) that is unambiguous by construction.
    """

    rules: list[Rule]

    def __post_init__(self) -> None:
        # Name collisions and abbreviation collisions are different problems.
        # A colliding *name* has to rename the category, because the canonical
        # key itself is unusable. A colliding *abbreviation* only has to lose
        # the abbreviation: "Defensive TD" abbreviates to the wildly overloaded
        # "TD", but `defensive_td` is unique and stays the canonical key.
        name_groups: dict[str, set[str]] = {}
        abbrev_groups: dict[str, set[str]] = {}
        for rule in self.rules:
            for alias in _plural_aliases(rule.category.key):
                name_groups.setdefault(alias, set()).add(rule.group)
            if rule.category.abbreviation:
                abbrev = slugify(rule.category.abbreviation)
                abbrev_groups.setdefault(abbrev, set()).add(rule.group)

        ambiguous_names = {a for a, g in name_groups.items() if len(g) > 1}
        ambiguous_abbrevs = {a for a, g in abbrev_groups.items() if len(g) > 1}

        self._by_key: dict[str, list[Rule]] = {}
        self._canonical: dict[int, str] = {}
        self._ambiguous: dict[str, set[str]] = {
            a: set() for a in ambiguous_names | ambiguous_abbrevs
        }

        for rule in self.rules:
            category = rule.category
            names = _plural_aliases(category.key)

            if names & ambiguous_names:
                # Qualify with the group so the caller can say which one.
                canonical = f"{slugify(rule.group)}_{category.key}"
                aliases = _plural_aliases(canonical)
                for alias in names & ambiguous_names:
                    self._ambiguous[alias].add(canonical)
            else:
                canonical = category.key
                aliases = set(names)

            abbrev = slugify(category.abbreviation) if category.abbreviation else ""
            if abbrev and abbrev in ambiguous_abbrevs:
                self._ambiguous[abbrev].add(canonical)
            elif abbrev:
                aliases.add(abbrev)

            self._canonical.setdefault(category.id, canonical)
            # The numeric id is unique by definition and can never collide.
            aliases.add(str(category.id))
            for alias in aliases:
                self._by_key.setdefault(alias, []).append(rule)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> RuleSet:
        """Parse the ``groups`` array of a ``FetchLeagueRules`` response."""
        rules: list[Rule] = []
        for group in payload.get("groups") or []:
            label = group.get("label", "")
            # "Punting" ships with no scoringRules at all when the league
            # scores nothing there. Absent, not empty.
            for raw in group.get("scoringRules") or []:
                rules.append(Rule.from_api(raw, label))
        return cls(rules)

    @property
    def categories(self) -> list[Category]:
        """Distinct scored categories, ordered by first appearance."""
        seen: dict[int, Category] = {}
        for rule in self.rules:
            seen.setdefault(rule.category.id, rule.category)
        return list(seen.values())

    def stat_keys(self) -> list[str]:
        """Canonical, unambiguous stat keys a caller may supply, sorted."""
        return sorted(set(self._canonical.values()))

    @property
    def ambiguous_keys(self) -> dict[str, list[str]]:
        """Bare names that collide, mapped to their qualified alternatives."""
        return {a: sorted(v) for a, v in sorted(self._ambiguous.items())}

    def resolve(self, key: str) -> list[Rule]:
        """Rules fed by one caller-supplied stat key.

        Returns every rule across every linked category id, which is how a
        single ``passing_td`` entry reaches both the flat 6-point rule and the
        long-distance bonus.

        Raises:
            FleaflickerInputError: if the key reaches more than one scoring
                group, listing the qualified alternatives. Guessing here would
                silently net a thrown interception against a caught one.
        """
        slug = slugify(str(key))
        if slug in self._ambiguous:
            options = ", ".join(sorted(self._ambiguous[slug]))
            raise FleaflickerInputError(
                f"Stat key '{key}' is ambiguous in this league: it matches more "
                f"than one scoring group. Use one of: {options}."
            )
        return self._by_key.get(slug, [])


@dataclass
class RuleScore:
    """What one rule contributed, and why."""

    category: str
    category_id: int
    group: str
    kind: str
    points: float
    description: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "category_id": self.category_id,
            "group": self.group,
            "kind": self.kind,
            "points": round(self.points, 4),
            "rule": self.description,
            "detail": self.detail,
        }


@dataclass
class ScoreResult:
    """A scored stat line: the total, the arithmetic, and what we could not do."""

    total: float
    breakdown: list[RuleScore]
    unscored_keys: list[str]
    unresolved_bonuses: list[str]
    position: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "total": round(self.total, 2),
            "breakdown": [b.as_dict() for b in self.breakdown if b.points != 0],
            "unscored_keys": self.unscored_keys,
            "unresolved_bonuses": self.unresolved_bonuses,
        }


def _split_events(value: Any, key: str) -> tuple[float, list[float] | None]:
    """Normalise a stat value into ``(total, per_event_values)``.

    A scalar is a count or total with no per-event detail. A list is the
    per-event magnitudes — ``[45, 12]`` for two touchdowns of 45 and 12 yards —
    which is what long-distance bonuses need. ``len(list)`` becomes the count,
    so one entry serves both the flat rule and the bonus.
    """
    if isinstance(value, bool):
        raise FleaflickerInputError(
            f"Stat '{key}' is a boolean; expected a number or a list of numbers."
        )
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        events: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise FleaflickerInputError(
                    f"Stat '{key}' list must hold numbers (per-event yardage); got {item!r}."
                )
            events.append(float(item))
        return float(len(events)), events
    raise FleaflickerInputError(
        f"Stat '{key}' must be a number or a list of numbers; got {type(value).__name__}."
    )


def score_stats(
    rules: RuleSet,
    stats: dict[str, Any],
    position: str = "WR",
) -> ScoreResult:
    """Score one game's stat line under a league's own rules.

    Args:
        rules: Parsed league rule set.
        stats: Stat key -> value. A number is a count or total. A list is the
            per-event magnitudes needed by long-distance bonuses, e.g.
            ``{"passing_td": [45, 12]}`` is two passing touchdowns, one of 45
            yards and one of 12.
        position: Roster position, used to apply position-restricted rules such
            as receptions (skill positions) and fumble recoveries (D/ST).

    Returns:
        A :class:`ScoreResult` with the total, a per-rule breakdown, any stat
        keys the league does not score, and any bonuses that could not be
        evaluated because only a count was supplied where per-event detail was
        needed.
    """
    if not isinstance(stats, dict):
        raise FleaflickerInputError(
            f"stats must be an object of stat_key -> value; got {type(stats).__name__}."
        )

    position = (position or "").strip().upper()
    total = 0.0
    breakdown: list[RuleScore] = []
    unscored: list[str] = []
    unresolved: list[str] = []

    for key, raw_value in stats.items():
        matched = rules.resolve(key)
        if not matched:
            unscored.append(str(key))
            continue

        value_total, events = _split_events(raw_value, str(key))

        for rule in matched:
            if not rule.applies_to(position):
                continue

            points = 0.0
            detail = ""

            if rule.kind == LINEAR:
                points = rule.points * (value_total / rule.for_every)
                detail = (
                    f"{value_total:g} x {rule.points:g}"
                    if rule.for_every == 1
                    else f"{value_total:g} / {rule.for_every:g} x {rule.points:g}"
                )

            elif rule.kind == TOTAL_THRESHOLD:
                if value_total >= rule.bound_lower:
                    points = rule.points
                    detail = f"{value_total:g} >= {rule.bound_lower:g}"
                else:
                    detail = f"{value_total:g} < {rule.bound_lower:g}, no bonus"

            elif rule.kind == TOTAL_RANGE:
                if rule.bound_lower <= value_total <= rule.bound_upper:
                    points = rule.points
                    detail = f"{rule.bound_lower:g} <= {value_total:g} <= {rule.bound_upper:g}"
                else:
                    detail = (
                        f"{value_total:g} outside "
                        f"[{rule.bound_lower:g}, {rule.bound_upper:g}], no bonus"
                    )

            elif rule.kind == PER_EVENT:
                if events is None:
                    # A count alone cannot answer "how long was each one".
                    # Say so rather than quietly scoring zero.
                    if value_total > 0:
                        unresolved.append(
                            f"{rule.category.key}: supplied as a count ({value_total:g}); "
                            f"'{rule.description}' needs a list of per-event yardage"
                        )
                    continue
                hits = [e for e in events if rule.bound_lower <= e <= rule.bound_upper]
                points = rule.points * len(hits)
                bound_text = (
                    f">= {rule.bound_lower:g}"
                    if math.isinf(rule.bound_upper)
                    else f"{rule.bound_lower:g}-{rule.bound_upper:g}"
                )
                detail = f"{len(hits)} of {len(events)} events {bound_text}"

            total += points
            breakdown.append(
                RuleScore(
                    category=rule.category.key,
                    category_id=rule.category.id,
                    group=rule.group,
                    kind=rule.kind,
                    points=points,
                    description=rule.description,
                    detail=detail,
                )
            )

    return ScoreResult(
        total=total,
        breakdown=breakdown,
        unscored_keys=unscored,
        unresolved_bonuses=_dedupe(unresolved),
        position=position,
    )


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)
