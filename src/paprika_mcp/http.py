"""Serve the MCP tools over Streamable HTTP, behind the household's sign-in (035).

For clients that cannot launch a local process, such as cloud-hosted MCP
connectors. There is one Paprika account and any household member may use it, so
the gate is not a shared secret: Caddy forwards only requests Cloudflare Access
signed, `control.identity.IdentityGate` verifies the assertion and asks Q Branch
who it is, and `members_only` refuses every tool call from anyone who is not a
listed member. The old static bearer token is gone and is read nowhere.

    ACCESS_TEAM=... MCP_AUD=... MCP_HOST=... MCP_PATH=/paprika \\
    CONTROL_SPOKE=... CONTROL_URL=... paprika-mcp http
"""

import contextlib
import copy
import os
from collections.abc import Callable
from typing import Any

import uvicorn
from control.identity import IdentityGate, Settings, StartRefused, members_only
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route

from .server import app as mcp_server


def build(
    settings: Settings,
    *,
    clock: Callable[[], float] | None = None,
    transport: Any = None,
) -> IdentityGate:
    """The ASGI app: one route at `settings.path`, behind `IdentityGate`.

    **The route is a bare path, never `/mcp`** (035 § Paths, not prefixes stripped).
    `redirect_slashes = False` so `/paprika/` is a 404 rather than a redirect to a URL
    that Caddy never forwarded. DNS-rebinding protection allows only `MCP_HOST`: this
    server never had it (no `security_settings` at all), so any `Host` was accepted.
    """
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
        async with manager.run():
            yield

    inner = Starlette(
        routes=[Route(settings.path, endpoint=Endpoint(), methods=["GET", "POST", "DELETE"])],
        lifespan=lifespan,
    )
    inner.router.redirect_slashes = False
    kwargs: dict[str, Any] = {"transport": transport}
    if clock is not None:
        kwargs["clock"] = clock
    # No member list: there is one Paprika account and any member Q Branch names may use it,
    # so `Directory` takes the id Q Branch names (`members=None`) and a list here could only
    # drift from Q Branch's own. `MCP_HOST` and `MCP_PATH` are refused by `Settings`.
    return settings.build(inner, None, **kwargs)


def run():
    """Entry point for `paprika-mcp http`."""
    try:
        settings = Settings.from_environ(os.environ)
    except StartRefused as refused:
        raise SystemExit(str(refused)) from None
    host = os.environ.get("PAPRIKA_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("PAPRIKA_MCP_PORT", "3018"))
    # No access log: Caddy writes a line per request already, and a future query string would
    # land in Loki.
    uvicorn.run(build(settings), host=host, port=port, access_log=False)
