"""Every place the version is written down has to say the same thing.

This repo has no release workflow: the host deploy is a copy of the tree plus
``docker compose up -d --build``, and the ``image:`` tag in docker-compose.yml
is what names the result. A version bump that misses that line rebuilds new
code under the old tag, and ``docker images`` then reports a version that is
not what is running. Nothing else would notice, so this does.

Read as text on purpose. Importing ``server`` builds the whole app, and the
point is that the files agree, not that one process agrees with itself.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _one(pattern: str, filename: str) -> str:
    matches = re.findall(pattern, (REPO / filename).read_text(), flags=re.MULTILINE)
    assert len(matches) == 1, f"expected exactly one version in {filename}, found {matches}"
    return matches[0]


def version_surfaces() -> dict[str, str]:
    changelog = re.findall(
        r"^## \[([^\]]+)\]", (REPO / "CHANGELOG.md").read_text(), flags=re.MULTILINE
    )
    assert changelog, "CHANGELOG.md has no version heading"
    return {
        "pyproject.toml [project].version": tomllib.loads((REPO / "pyproject.toml").read_text())[
            "project"
        ]["version"],
        "server.py VERSION": _one(r'^VERSION = "([^"]+)"$', "server.py"),
        "docker-compose.yml image tag": _one(
            r"^\s*image:\s*mcp-fleaflicker:(\S+)\s*$", "docker-compose.yml"
        ),
        # Newest entry first, so the top heading is the current version.
        "CHANGELOG.md top entry": changelog[0],
    }


def test_every_version_surface_agrees() -> None:
    surfaces = version_surfaces()
    assert len(set(surfaces.values())) == 1, surfaces


def test_version_is_plain_semver() -> None:
    """An image tag like ``latest`` or ``0.3`` would agree with nothing useful."""
    for name, value in version_surfaces().items():
        assert SEMVER.match(value), f"{name} is {value!r}"
