"""Redaction tests.

This server holds no upstream credential — Fleaflicker's read API is public —
but it can still be configured with `MCP_AUTH_TOKEN` to gate its own `/mcp`
endpoint, and a fork may add an authenticated source later. The filter is
installed unconditionally so that day is covered, and these tests prove it
actually fires rather than merely being wired up.

Both halves are tested on purpose: a redaction pattern that never matches is a
silent no-op that reads as coverage, so every case asserts the secret is gone
*and* a matching negative case asserts ordinary prose survives untouched.
"""

from __future__ import annotations

import logging

import pytest

from clients.redact import (
    REDACTED,
    SecretRedactingFilter,
    install_log_redaction,
    redact_secrets,
)

SECRET = "abcdef0123456789ABCDEF"


# --- the secret is removed ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"GET /x?access_token={SECRET}",
        f"GET /x?api_key={SECRET}&b=2",
        f"GET /x?apikey={SECRET}",
        f"GET /x?auth_token={SECRET}",
        f"GET /x?client_secret={SECRET}",
        f"GET /x?password={SECRET}",
        f"GET /x?token={SECRET}",
        f"Authorization: Bearer {SECRET}",
        f'{{"Authorization": "Bearer {SECRET}"}}',
    ],
)
def test_secret_never_survives(text: str):
    out = redact_secrets(text)
    assert SECRET not in out
    assert REDACTED in out


def test_redaction_preserves_surrounding_query_params():
    out = redact_secrets(f"GET /x?league_id=14153&api_key={SECRET}&season=2025")
    assert "league_id=14153" in out
    assert "season=2025" in out
    assert SECRET not in out


# --- ordinary text survives (the other half) -----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "no bearer token configured",
        "GET /api/FetchLeagueRules?sport=NFL&league_id=14153",
        "league_id=14153&season=2025",
        "Scoring rules parsed: 46 rules across 7 groups",
        "",
    ],
)
def test_ordinary_text_is_untouched(text: str):
    assert redact_secrets(text) == text


def test_short_bearer_words_are_not_mangled():
    """The 16-char floor keeps prose out of the match."""
    assert redact_secrets("bearer of bad news") == "bearer of bad news"


# --- the filter is wired into logging ------------------------------------


def test_filter_scrubs_the_record_message():
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, f"Bearer {SECRET}", None, None
    )
    SecretRedactingFilter().filter(record)
    assert SECRET not in record.getMessage()


def test_filter_scrubs_tuple_args():
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "auth=%s", (f"Bearer {SECRET}",), None
    )
    SecretRedactingFilter().filter(record)
    assert SECRET not in record.getMessage()


def test_filter_scrubs_dict_args():
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "auth=%(a)s", ({"a": f"Bearer {SECRET}"},), None
    )
    SecretRedactingFilter().filter(record)
    assert SECRET not in record.getMessage()


def test_httpx_logger_is_silenced_at_info():
    """httpx logs full request URLs at INFO; that is the usual leak path."""
    install_log_redaction()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_install_is_idempotent():
    """Restarts and repeated imports must not stack duplicate filters."""
    install_log_redaction()
    install_log_redaction()
    root = logging.getLogger()
    installed = [f for f in root.filters if isinstance(f, SecretRedactingFilter)]
    assert len(installed) == 1


def test_end_to_end_capture(caplog):
    """A real log call through a real logger must come out clean."""
    install_log_redaction()
    log = logging.getLogger("mcp-fleaflicker.test")
    log.addFilter(SecretRedactingFilter())
    with caplog.at_level(logging.INFO):
        log.info("calling upstream with Authorization: Bearer %s", SECRET)
    assert SECRET not in caplog.text
