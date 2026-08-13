"""Shared fixtures.

Every JSON file under ``fixtures/`` is a real capture from the live Fleaflicker
API (league 14153, seasons 2025/2026), trimmed to the fields the code reads.
They are real rather than hand-written on purpose: the behaviours that bite
here — omitted zero fields, one touchdown spanning two category ids, a bonus
whose bounds are both absent — are exactly the ones a hand-written fixture
would smooth over.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clients.fleaflicker import FleaflickerClient
from clients.scoring import RuleSet

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a captured API response by file stem."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def league_rules_payload() -> dict:
    return load_fixture("league_rules")


@pytest.fixture
def rules(league_rules_payload: dict) -> RuleSet:
    """The real rule set: 46 rules across 7 scored groups."""
    return RuleSet.from_api(league_rules_payload)


@pytest.fixture
def client() -> FleaflickerClient:
    return FleaflickerClient(league_id=14153)
