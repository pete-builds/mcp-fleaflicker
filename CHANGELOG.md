# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html); while the version is
below 1.0, a minor bump can break things, and this one does.

## [0.3.0] (2026-11-09)

### Breaking

- **The server will not start over HTTP without a bearer token.** Until now it
  ran with no token and answered anyone who could reach the port. From this
  version, if `MCP_AUTH_TOKEN` is missing, empty, or blank, the process exits
  with `Refusing to start streamable-http with no auth provider` before it
  opens a port. Under `restart: unless-stopped` that shows up as a container
  that restarts in a loop until the token is there.

  Before you upgrade:

  1. Generate a token:
     `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
  2. Put it in `.env` on the host as `MCP_AUTH_TOKEN=<token>` and run
     `chmod 600 .env`. Keep it out of `docker-compose.yml` and out of git.
  3. Add `Authorization: Bearer <token>` to every client that talks to this
     server. A client without it now gets `401` on every request to `/mcp`.

- **`MCP_AUTH_REQUIRED=false` no longer runs the server open.** It is ignored
  for HTTP. There is no setting that allows HTTP without a token.

### Unchanged

- `GET /healthz` still answers `200` with no token, so the container
  healthcheck and uptime monitors keep working.
- stdio needs no token. `MCP_TRANSPORT=stdio python server.py` starts exactly
  as before.

### Changed

- `pete-mcp-core` moves from commit `15d2106` to `18f2261`, which is where the
  token requirement comes from. The same bump brings two smaller things:
  - `run_server` now installs an idle-session reaper for HTTP. This server
    already set a 1800 second idle timeout itself, and still does.
  - The healthcheck's "healthy" status codes widen from 200, 400, 401, 405,
    406 to 200, 204, 400, 401, 404, 405, 406, 503. The probe path is still
    `/healthz`, and 500 still fails the container.
- `docker-compose.yml` names the image `mcp-fleaflicker:0.3.0`.

### Added

- `tests/test_auth_required.py` proves all of the above against the real
  server: the refusal with no token, `401` for a missing or wrong token, a
  working `initialize` with the right one, a healthy healthcheck with auth on,
  and stdio starting with no token.
- `tests/test_version.py` fails if the version in `pyproject.toml`,
  `server.py`, `docker-compose.yml`, and this file ever disagree.

Versions 0.2.0 and earlier predate this file; see the git history.
