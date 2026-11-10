"""HTTP needs a bearer token, and there is no way to switch that off.

Until pete-mcp-core ``18f2261`` this server ran on the LAN with no token and
answered every caller. Now a network transport with no credential refuses to
start, and with a token every request to ``/mcp`` must present it. A regression
here is silent: the server starts, tools answer, and the only difference is
that anyone on the network can call them. So each property is asserted against
the real ``server`` module and the real ASGI app, not against the library.

Each boot runs ``server.main()`` in a fresh interpreter with uvicorn's
``Server.serve`` replaced by a recorder, the same way ``test_lan_host.py``
does. That captures the exact app ``run_server`` hands uvicorn without opening
a socket, and it is then driven through httpx's ASGI transport inside the app's
own lifespan. A refused start is reported with the real exception type and
message, plus whether uvicorn was ever handed an app.

Hermetic on purpose: the subprocess runs in an empty directory with
python-dotenv disabled, so a developer's own ``.env`` (which now has to hold a
token) cannot make a "no token" case pass or fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from pete_mcp_core import healthcheck as core_healthcheck

import healthcheck

PORT = 3727
REPO = Path(__file__).resolve().parent.parent
# Obviously fake on purpose. Nothing here is a credential.
TOKEN = "test-token-not-a-secret"
WRONG_TOKEN = "wrong-token-not-a-secret"

REFUSAL = "Refusing to start streamable-http with no auth provider"
TOKEN_MISSING = "Bearer token required for client 'fleaflicker' but not provided"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "auth-probe", "version": "0"},
    },
}

_PROBE = r"""
import asyncio, json, sys
import httpx, uvicorn

captured = {}

async def _record(self, sockets=None):
    captured["app"] = self.config.app

uvicorn.Server.serve = _record

requests = json.loads(sys.argv[1])
port = int(sys.argv[2])
out = {"stage": "import"}

def emit():
    out["handed_to_uvicorn"] = "app" in captured
    print("PROBE_RESULT " + json.dumps(out))

try:
    import server
    out["stage"] = "main"
    out["auth_provider"] = type(server.mcp.auth).__name__
    server.main()
except BaseException as exc:
    out["error_type"] = f"{type(exc).__module__}.{type(exc).__qualname__}"
    out["error"] = str(exc)
    emit()
    sys.exit(0)

out["stage"] = "served"
app = captured.get("app")

async def drive():
    out["responses"] = {}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=f"http://127.0.0.1:{port}"
        ) as client:
            for name, spec in requests.items():
                resp = await client.request(
                    spec["method"],
                    spec["path"],
                    json=spec.get("json"),
                    headers=spec.get("headers") or {},
                )
                out["responses"][name] = {
                    "status": resp.status_code,
                    "headers": dict(resp.headers),
                    "text": resp.text[:4000],
                }

if app is not None:
    asyncio.run(drive())
emit()
"""


def _env(overrides: dict[str, str | None]) -> dict[str, str]:
    """A clean environment: nothing inherited that could carry a token."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("FASTMCP_", "MCP_", "FLEAFLICKER_")) and k != "PYTHONPATH"
    }
    env.update(
        {
            "MCP_PORT": str(PORT),
            "FASTMCP_SHOW_SERVER_BANNER": "false",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "PYTHONPATH": str(REPO),
            # server.py calls load_dotenv(), which finds a .env beside
            # server.py whatever the working directory is. Off, so the
            # environment built here is the whole configuration.
            "PYTHON_DOTENV_DISABLED": "1",
        }
    )
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _boot(
    tmp_path: Path,
    requests: dict[str, dict] | None = None,
    **env_overrides: str | None,
) -> dict:
    """Start the real server in a fresh interpreter and drive its ASGI app."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(requests or {}), str(PORT)],
        cwd=tmp_path,  # pydantic-settings reads .env from the working directory
        env=_env(env_overrides),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,  # a crashed probe is reported below with its stderr
    )
    for line in proc.stdout.splitlines():
        if line.startswith("PROBE_RESULT "):
            return json.loads(line.removeprefix("PROBE_RESULT "))
    pytest.fail(f"probe produced no result (rc={proc.returncode}):\n{proc.stderr[-3000:]}")


def _post_initialize(authorization: str | None = None) -> dict:
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if authorization is not None:
        headers["authorization"] = authorization
    return {"method": "POST", "path": "/mcp", "json": INITIALIZE, "headers": headers}


def _jsonrpc(text: str) -> dict:
    """The JSON-RPC message in a streamable-http reply, plain JSON or one SSE event."""
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    for line in stripped.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    pytest.fail(f"no JSON-RPC message in reply: {text[:300]!r}")


# --- no token: HTTP refuses to start --------------------------------------

# Unset, empty and whitespace are all "no token". A compose file that passes
# MCP_AUTH_TOKEN= through from an empty .env line is the likely real case.
NO_TOKEN = pytest.mark.parametrize("token", [None, "", "   "], ids=["unset", "empty", "blank"])


@NO_TOKEN
def test_http_without_a_token_refuses_to_start(tmp_path: Path, token: str | None) -> None:
    """The deployed shape: MCP_TRANSPORT is not set, so server.main() picks
    streamable-http. With no token run_server must raise rather than serve."""
    result = _boot(tmp_path, MCP_AUTH_TOKEN=token)
    assert result["stage"] == "main", result
    assert result["error_type"] == "builtins.ValueError", result
    assert result["error"].startswith(REFUSAL), result
    assert result["handed_to_uvicorn"] is False, result


@NO_TOKEN
def test_explicit_http_without_a_token_refuses_at_import(tmp_path: Path, token: str | None) -> None:
    """With MCP_TRANSPORT=streamable-http named outright, the module will not
    even build its FastMCP instance."""
    result = _boot(tmp_path, MCP_TRANSPORT="streamable-http", MCP_AUTH_TOKEN=token)
    assert result["stage"] == "import", result
    assert result["error_type"] == "pete_mcp_core.auth.AuthRequiredError", result
    assert result["error"] == TOKEN_MISSING, result
    assert result["handed_to_uvicorn"] is False, result


@pytest.mark.parametrize("transport", [None, "streamable-http"], ids=["default", "explicit"])
def test_auth_required_false_is_not_an_opt_out(tmp_path: Path, transport: str | None) -> None:
    """MCP_AUTH_REQUIRED=false used to mean "run open". It must not any more:
    an .env left over from the old deployment still gets a refusal."""
    result = _boot(
        tmp_path, MCP_TRANSPORT=transport, MCP_AUTH_REQUIRED="false", MCP_AUTH_TOKEN=None
    )
    assert result["error_type"] == "builtins.ValueError", result
    assert result["error"].startswith(REFUSAL), result
    assert result["handed_to_uvicorn"] is False, result


def test_server_process_exits_nonzero_without_a_token(tmp_path: Path) -> None:
    """No recorder, no patching: ``python server.py`` exactly as the container
    runs it. It has to die with the refusal on stderr, because a container
    that restarts unless-stopped turns anything quieter into a silent loop.

    Bound to loopback on an ephemeral port so that if the guard ever goes
    missing, the failure is a timeout here and not an open listener."""
    env = _env({"MCP_AUTH_TOKEN": None, "MCP_HOST": "127.0.0.1", "MCP_PORT": "0"})
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO / "server.py")],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("server.py kept running with no token: it started an open listener")
    assert proc.returncode != 0, proc.stderr[-2000:]
    assert REFUSAL in proc.stderr, proc.stderr[-2000:]


# --- stdio still works with no token ---------------------------------------


def test_stdio_starts_without_a_token(tmp_path: Path) -> None:
    """stdio is a local pipe, so local development needs MCP_TRANSPORT=stdio
    and nothing else. A real initialize round trip over the real pipes."""
    proc = subprocess.run(
        [sys.executable, str(REPO / "server.py")],
        cwd=tmp_path,
        env=_env({"MCP_TRANSPORT": "stdio", "MCP_AUTH_TOKEN": None}),
        input=json.dumps(INITIALIZE) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    reply = json.loads(proc.stdout.splitlines()[0])
    assert reply["id"] == 1, reply
    assert reply["result"]["serverInfo"]["name"] == "Fleaflicker", reply


# --- with a token: /mcp demands it ------------------------------------------


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """One boot with a token, driven with every request the tests below read."""
    _, health_path, _ = _healthcheck_target()
    result = _boot(
        tmp_path_factory.mktemp("served"),
        {
            "no_header": _post_initialize(),
            "wrong_token": _post_initialize(f"Bearer {WRONG_TOKEN}"),
            "not_bearer": _post_initialize(TOKEN),
            "right_token": _post_initialize(f"Bearer {TOKEN}"),
            "health": {"method": "GET", "path": health_path},
            "health_wrong_token": {
                "method": "GET",
                "path": health_path,
                "headers": {"authorization": f"Bearer {WRONG_TOKEN}"},
            },
            "transport_get": {"method": "GET", "path": "/mcp"},
        },
        MCP_AUTH_TOKEN=TOKEN,
    )
    assert result["stage"] == "served", result
    assert result["handed_to_uvicorn"] is True, result
    return result


def test_token_builds_a_real_auth_provider(served: dict) -> None:
    assert served["auth_provider"] == "StaticTokenVerifier", served["auth_provider"]


@pytest.mark.parametrize("case", ["no_header", "wrong_token", "not_bearer"])
def test_initialize_without_the_right_token_is_401(served: dict, case: str) -> None:
    response = served["responses"][case]
    assert response["status"] == 401, response
    assert "bearer" in response["headers"].get("www-authenticate", "").lower(), response
    # A rejected caller must not be handed a transport session.
    assert "mcp-session-id" not in response["headers"], response


def test_initialize_with_the_right_token_succeeds(served: dict) -> None:
    """The control for the three 401s above: the same request, same app, one
    header different, is served. So a 401 is the token check and not a route
    that rejects everything."""
    response = served["responses"]["right_token"]
    assert response["status"] != 401, response
    assert response["status"] == 200, response
    message = _jsonrpc(response["text"])
    assert "error" not in message, message
    assert message["result"]["serverInfo"]["name"] == "Fleaflicker", message
    assert response["headers"].get("mcp-session-id"), response


def test_alternate_explicit_transport_serves_with_a_token(tmp_path: Path) -> None:
    """Naming the transport outright changes where a missing token is caught
    (import instead of run_server), so prove that path serves and guards too."""
    result = _boot(
        tmp_path,
        {"no_header": _post_initialize(), "right_token": _post_initialize(f"Bearer {TOKEN}")},
        MCP_TRANSPORT="streamable-http",
        MCP_AUTH_TOKEN=TOKEN,
    )
    assert result["stage"] == "served", result
    assert result["responses"]["no_header"]["status"] == 401, result
    assert result["responses"]["right_token"]["status"] == 200, result


# --- the container healthcheck does not flap --------------------------------


def _healthcheck_target() -> tuple[int, str, frozenset[int]]:
    """Ask the real healthcheck.py what it would probe, and what it calls healthy.

    ``healthcheck.main()`` is run as the container runs it, with only the
    network call swapped for a recorder, so a change to the path or the code
    set in healthcheck.py changes what these tests request and accept.
    """
    seen: dict = {}

    def _record(port: int, *, path: str, healthy_codes: frozenset[int]) -> int:
        seen.update(port=port, path=path, codes=frozenset(healthy_codes))
        return 0

    saved = {k: os.environ.pop(k, None) for k in ("FASTMCP_PORT", "MCP_PORT", "MCP_HEALTH_PATH")}
    original = healthcheck.check
    healthcheck.check = _record
    try:
        assert healthcheck.main() == 0
    finally:
        healthcheck.check = original
        os.environ.update({k: v for k, v in saved.items() if v is not None})
    return seen["port"], seen["path"], seen["codes"]


def test_healthcheck_probes_this_server_off_the_transport() -> None:
    port, path, codes = _healthcheck_target()
    assert port == PORT
    assert path == "/healthz"
    # Any request to a transport mount creates a session; see healthcheck.py.
    assert path not in core_healthcheck.TRANSPORT_PATHS
    assert not path.startswith(("/mcp", "/sse"))
    # A real fault must still fail the container.
    assert 500 not in codes


def test_healthcheck_stays_healthy_with_auth_on_and_no_token(served: dict) -> None:
    """The probe carries no token and never will. With auth on it must still
    be judged healthy by healthcheck.py's own code set, or Docker marks the
    container unhealthy for being correctly locked."""
    _, _, codes = _healthcheck_target()
    response = served["responses"]["health"]
    assert response["status"] in codes, response
    # Stricter than the container needs: /healthz is a custom route outside
    # the auth middleware, and an uptime monitor reads it without a token.
    assert response["status"] == 200, response
    body = json.loads(response["text"])
    assert body["status"] == "ok", response
    # The version the running server reports is the one the package declares.
    declared = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    assert body["version"] == declared, response
    assert "mcp-session-id" not in response["headers"], response


def test_health_route_ignores_a_bad_token(served: dict) -> None:
    """It sits outside auth entirely: a wrong token is not held against it."""
    assert served["responses"]["health_wrong_token"]["status"] == 200, served["responses"]


def test_control_the_transport_path_is_guarded_where_health_is_not(served: dict) -> None:
    """Positive control for the 200 above: the same tokenless GET against the
    transport path is rejected, so the health route answering is a property of
    that route and not of auth being off."""
    response = served["responses"]["transport_get"]
    assert response["status"] == 401, response
    assert "mcp-session-id" not in response["headers"], response
