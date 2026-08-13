"""Secret redaction for logs.

This server holds no upstream credential: Fleaflicker's read API is public and
unauthenticated, so there is no API key to leak. The redaction here exists for
the one secret that does exist, plus the ones a future contributor might add:

* ``MCP_AUTH_TOKEN`` — the bearer token that gates *this* server's ``/mcp``
  endpoint when ``MCP_AUTH_REQUIRED=true``. It arrives in an ``Authorization``
  header, and any middleware that logs a rejected request can carry it into
  the log.
* Anything a fork bolts on. If someone later adds an authenticated data source,
  the filter is already installed and the query-param list is the only edit.

Silencing ``httpx`` at INFO is the load-bearing half: it logs every request's
full URL, query string included, and that is how credentials usually reach logs.
"""

from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"

# Query params whose values would be credentials if present.
_SECRET_PARAMS = (
    "access_token",
    "api_key",
    "apikey",
    "auth_token",
    "client_secret",
    "password",
    "token",
)

_PARAM_RE = re.compile(r"(?i)\b(" + "|".join(_SECRET_PARAMS) + r")=([^&\s\"'\\]+)")

# `Authorization: Bearer xyz`. The 16-char floor keeps ordinary prose
# ("no bearer token configured") out of the match while catching real tokens.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/]{16,}=*)")


def redact_secrets(text: str) -> str:
    """Return ``text`` with credential values replaced by ``[REDACTED]``.

    Example::

        >>> redact_secrets("GET /mcp with Authorization: Bearer abcdef0123456789xyz")
        'GET /mcp with Authorization: Bearer [REDACTED]'
    """
    if not text:
        return text
    out = _PARAM_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", out)


class SecretRedactingFilter(logging.Filter):
    """Log filter that scrubs secrets from the *formatted* log message.

    Redacting ``msg`` and ``args`` separately is not enough, and the gap is
    easy to miss. ``log.info("Authorization: Bearer %s", token)`` holds no
    secret in ``msg`` (the token is not there yet) and nothing matchable in
    ``args`` (a bare token, with no ``Bearer`` prefix to anchor the pattern on).
    The credential only exists once the two are interpolated. So interpolate
    first, redact the result, and store it as a literal message with the args
    cleared.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            formatted = record.getMessage()
        except Exception:
            # A broken format string is the caller's bug, not ours to raise on.
            return True

        redacted = redact_secrets(formatted)
        if redacted != formatted:
            record.msg = redacted
            record.args = ()
        elif isinstance(record.msg, str):
            # Nothing matched after formatting, but scrub the template too in
            # case a handler renders it some other way.
            record.msg = redact_secrets(record.msg)
        return True


_HTTP_LOGGERS = ("httpx", "httpx._client", "httpcore", "httpcore.http11", "hpack")


def install_log_redaction() -> SecretRedactingFilter:
    """Silence HTTP-client URL logging and attach the redaction filter.

    Returns the installed filter so tests (and callers building their own
    handlers) can reuse the same instance.
    """
    flt = SecretRedactingFilter()

    root = logging.getLogger()
    if not any(isinstance(f, SecretRedactingFilter) for f in root.filters):
        root.addFilter(flt)
    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(flt)

    for name in _HTTP_LOGGERS:
        lg = logging.getLogger(name)
        # httpx logs "HTTP Request: GET <full url with query>" at INFO.
        lg.setLevel(logging.WARNING)
        if not any(isinstance(f, SecretRedactingFilter) for f in lg.filters):
            lg.addFilter(flt)

    return flt
