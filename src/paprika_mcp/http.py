"""Serve the MCP tools over Streamable HTTP, behind the household's sign-in (037).

For clients that cannot launch a local process, such as cloud-hosted MCP
connectors. There is one Paprika account and any household member may use it, so
the gate is not a shared secret. Caddy asks Q Branch's `/verify` about the Cloudflare
Access assertion, deletes it, and forwards the request with headers naming the caller;
`control.identity.HubGate` reads them, and `members_only` refuses every tool call and
`prompts/get` from anyone who is not a member. This server verifies nothing and asks
Q Branch nothing. **It is safe only because nothing but Caddy can open a connection to
its port** (037 § Each server listens only for Caddy), which is the compose entry's
property and not this module's.

    MCP_HOST=... MCP_PATH=/paprika paprika-mcp http
"""

import contextlib
import copy
import logging
import os
import sys
import threading
from collections.abc import Mapping
from typing import Any

import uvicorn
from control.identity import HubGate, Settings, StartRefused, members_only
from control.logs import LogfmtFormatter
from control.spoke_client import Transport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route

from .credentials import Probe, install, open_vault
from .server import app as mcp_server

#: Seconds between the spoke client's ticks; a test makes it short.
SPOKE_STEP = 5.0


def build(
    settings: Settings, environ: Mapping[str, str] | None = None, *,
    probe: Probe | None = None, transport: Transport | None = None,
) -> HubGate:
    """The ASGI app: one route at `settings.path`, behind `HubGate`.

    **The route is a bare path, never `/mcp`** (035 § Paths, not prefixes stripped).
    `redirect_slashes = False` so `/paprika/` is a 404 rather than a redirect to a URL
    that Caddy never forwarded. DNS-rebinding protection allows only `MCP_HOST`: this
    server never had it (no `security_settings` at all), so any `Host` was accepted.

    **The login is the `Paprika` item in the vault copy** (039 order 4): one `Credentials` object is
    built here and `install`ed as the one the tools read, its watcher and the spoke client's
    reporter run on threads for as long as the app does, and `poll=False` because this server
    reports and asks for nothing. `probe` and `transport` are the tests' seams.
    """
    env = os.environ if environ is None else environ
    credentials, spoke = open_vault(env, probe=probe, transport=transport)
    install(credentials)
    # `members_only` replaces a handler, so it goes on a copy with its own handler table:
    # stdio mode (and the in-memory session tests) use `server.app` and have no caller to
    # ask. It is installed after `@app.call_tool()` registered in server.py (importing that
    # module did), which `members_only` checks.
    guarded = copy.copy(mcp_server)
    guarded.request_handlers = dict(mcp_server.request_handlers)
    members_only(guarded)

    manager = StreamableHTTPSessionManager(
        app=guarded,
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[settings.host],
            allowed_origins=[],
        ),
    )

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # Threads, started and stopped with the app and not in a boot step: the watcher's login is
        # synchronous `requests`, and on the event loop it would freeze every request. A task a
        # boot step could end would stop reporting an item that goes, and the page would read `ok`
        # for a login that was deleted to cut it off.
        stopping = threading.Event()
        if spoke is not None:
            threading.Thread(target=spoke.serve, args=(stopping, SPOKE_STEP), name="paprika-spoke",
                             daemon=True).start()
        threading.Thread(target=credentials.watch, args=(stopping,), name="paprika-watch", daemon=True).start()
        try:
            async with manager.run():
                yield
        finally:
            stopping.set()

    inner = Starlette(
        routes=[Route(settings.path, endpoint=Endpoint(), methods=["GET", "POST", "DELETE"])],
        lifespan=lifespan,
    )
    inner.router.redirect_slashes = False
    # No member list: there is one Paprika account and any member Q Branch names may use it,
    # so `members=None` takes the id Caddy names and a list here could only drift from the
    # household manifest. `MCP_HOST` and `MCP_PATH` are refused by `Settings`.
    return settings.build(inner, None)


class _LiveStderr(logging.StreamHandler):  # type: ignore[type-arg]
    """A stderr handler that looks `sys.stderr` up at each emit (as `control.logs`' own does)."""

    def __init__(self) -> None:
        super().__init__()

    @property  # type: ignore[override]
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


def attach_paprika_logging(level: int = logging.INFO) -> None:
    """Put a logfmt handler on the `paprika_mcp` logger itself, as kroger-mcp does for its own.

    This server configured no logging: `control` attaches a handler to its own logger only, so a
    `paprika_mcp` event (the watcher's, a refused call's) reached the root logger, which has none and
    is at WARNING. `control.logs.event` puts every field in `extra` with an empty message, so a line
    that did get out printed blank. `propagate = False` so the root's handler never prints a line a
    second time. Idempotent.
    """
    log = logging.getLogger("paprika_mcp")
    if not any(isinstance(h, _LiveStderr) for h in log.handlers):
        handler = _LiveStderr()
        handler.setFormatter(LogfmtFormatter())
        log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False


def run():
    """Entry point for `paprika-mcp http`."""
    attach_paprika_logging()
    try:
        settings = Settings.from_environ(os.environ)
    except StartRefused as refused:
        raise SystemExit(str(refused)) from None
    host = os.environ.get("PAPRIKA_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("PAPRIKA_MCP_PORT", "3018"))
    # No access log: Caddy writes a line per request already, and a future query string would
    # land in Loki.
    uvicorn.run(build(settings), host=host, port=port, access_log=False)
