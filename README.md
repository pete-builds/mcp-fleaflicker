# mcp-fleaflicker

An MCP server for [Fleaflicker](https://www.fleaflicker.com) fantasy leagues. It reads
rosters, standings, matchups, boxscores, and draft boards, and it scores stat lines using
**the league's own published scoring rules** rather than a hardcoded scoring system.

Fleaflicker's read API is public and unauthenticated, so this server needs no API key,
stores no credentials, and exposes no write surface. It cannot change your league.

## Why the scoring engine is the interesting part

Most fantasy tooling hardcodes a scoring system, or offers a handful of presets. This
server fetches `FetchLeagueRules` and evaluates whatever it finds, so the same code scores
a 6-point-passing-TD superflex league and a standard half-PPR league without a branch.

Three things about Fleaflicker's rule model are load-bearing, and each was verified
against the live API rather than assumed:

**Zero-valued fields are omitted from the JSON.** The API is protobuf-derived and
serializes defaults by leaving them out. The shutout rule ("10 extra points when total
Points Allowed is exactly 0") arrives with *no* `boundLower` and *no* `boundUpper` key,
because both are `0`. Default a missing upper bound to infinity and every defense scores
the shutout bonus every week. Missing bounds mean **zero**, never "unbounded".

**`RANGE_LOWER_BOUND` is two different rules wearing one name.** Which one depends on
`category.multiValue`. On a single-valued category it is a once-per-game threshold on the
summed total (300 passing yards is +1, once, no matter how far past). On a multi-valued
category it fires once per qualifying *event* (every touchdown of 80+ yards is +4 each).

**A touchdown is two categories, not one.** The flat 6 points and the long-distance bonus
live under separate category ids that share a name within a group. They are linked
automatically by (group, name), so one caller-supplied stat key feeds both.

### Score one game at a time

Every threshold rule fires on a single game's box score. A receiver with 2,000 yards
across 17 games earns the 150-yard bonus only in the games he actually cleared 150.
Feeding the engine a season total silently miscounts every bonus in the rule set.

### Ambiguous stat keys fail loudly

"Interception" is a passing category worth −2 (the quarterback threw it) *and* a defensive
category worth +2 (the defense caught it). Resolving a bare `interception` to both nets
two picks to exactly zero and reports a confident, wrong number. So any key reaching more
than one scoring group refuses to resolve and names the alternatives instead:
`passing_interception` and `defense_interception`.

## Tools

| Tool | What it does |
|---|---|
| `get_league_rules` | Roster construction and the complete scoring rule set, including the valid stat keys |
| `get_standings` | Records, points for and against, streaks, draft and waiver position |
| `get_roster` | One team's lineup by slot, for any week or season |
| `list_matchups` | A week's head-to-head games and scores |
| `get_boxscore` | Full matchup detail, both lineups, with Fleaflicker's own computed points |
| `get_draft_board` | Every pick, round by round, with overall pick numbers |
| `search_players` | The player pool by name or position, with ranks and ownership |
| `score_stat_line` | Score a stat line under the league's real rules, with a per-rule breakdown |

All tools are read-only and idempotent. Every tool returns a JSON string in one of two
shapes:

```jsonc
// success
{"data": ...}

// failure — code is from a fixed enum
{"error": "human-readable message", "code": "UPSTREAM_DOWN", "details": {...}}
```

Codes: `UPSTREAM_DOWN`, `AUTH_FAILED`, `INVALID_INPUT`, `NOT_FOUND`, `RATE_LIMITED`,
`INTERNAL`. No exception ever reaches the caller.

## Quick start

```bash
git clone https://github.com/pete-builds/mcp-fleaflicker.git
cd mcp-fleaflicker
cp .env.example .env          # set FLEAFLICKER_LEAGUE_ID, or leave it blank
docker compose up -d --build
curl -s localhost:3727/healthz
```

Then register it with your MCP client:

```bash
claude mcp add fleaflicker --transport http --scope user \
  --url http://localhost:3727/mcp
```

Running from source instead:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.in
.venv/bin/python server.py
```

## Finding your league id

It is in the league URL: `fleaflicker.com/nfl/leagues/14153` is league `14153`.

Setting `FLEAFLICKER_LEAGUE_ID` makes every tool's `league_id` argument optional. Leaving
it unset is also valid: callers then pass `league_id` per call, which is how one
deployment serves several leagues.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FLEAFLICKER_LEAGUE_ID` | *(unset)* | Default league. Optional; every tool takes a per-call override |
| `FLEAFLICKER_SPORT` | `NFL` | Only NFL has been verified |
| `FLEAFLICKER_BASE_URL` | `https://www.fleaflicker.com/api` | Override for testing against a mock |
| `FLEAFLICKER_TIMEOUT` | `30` | Per-request timeout, seconds |
| `MCP_PORT` | `3727` | Listen port |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `text` | `text` or `json` |
| `MCP_AUTH_REQUIRED` | `false` | Require a bearer token on `/mcp` |
| `MCP_AUTH_TOKEN` | *(unset)* | The bearer token, when auth is required |

## Scoring examples

Get the valid stat keys first:

```
get_league_rules()  ->  data.scoring.stat_keys
```

A plain number is a count or total. A **list** is per-event yardage, which is what
distance bonuses need:

```python
score_stat_line(
    stats={"passing_yard": 318, "passing_td": [45, 12], "passing_interception": 1},
    position="QB",
)
```

That is two passing touchdowns, one of 45 yards and one of 12: the flat rule scores twice
and the 40-to-79 bonus once. Passing `{"passing_td": 2}` instead scores the flat rule and
reports the distance bonus under `unresolved_bonuses`, rather than silently scoring it as
zero.

The response carries the arithmetic, not just the total:

```jsonc
{"data": {
  "position": "QB",
  "total": 31.52,
  "breakdown": [
    {"category": "passing_yard", "kind": "linear", "points": 8.72,
     "detail": "218 / 25 x 1", "rule": "1 point for every 25 Passing Yards (0.04 per)"},
    ...
  ],
  "unscored_keys": [],
  "unresolved_bonuses": []
}}
```

## Testing

```bash
.venv/bin/python -m pytest -q     # 121 tests
.venv/bin/ruff check .
```

Fixtures under `tests/fixtures/` are real captures from the live API, trimmed to the
fields the code reads. They are real rather than hand-written on purpose: the behaviours
that bite here — omitted zero fields, one touchdown spanning two category ids, a bonus
whose bounds are both absent — are exactly the ones a hand-written fixture would smooth
over.

The scoring tests include **oracle tests** that reconstruct real stat lines and assert
against the totals Fleaflicker itself published for those games, so the engine is checked
against the platform rather than against its own arithmetic.

## What this does not do

It reads and scores; it does not project. There is no ranking model, no waiver
recommendation, and no start/sit advice. Feeding projected stat lines into
`score_stat_line` gives you projected points under the real rules, but the projections
have to come from somewhere else.

It is also unofficial and not affiliated with Fleaflicker. The API it uses is public but
undocumented, so a shape change upstream can break a normaliser; the tests are built to
catch that quickly.

## License

MIT. See [LICENSE](LICENSE).
