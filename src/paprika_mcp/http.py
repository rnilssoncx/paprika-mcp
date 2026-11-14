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
import os

import uvicorn
from control.identity import HubGate, Settings, StartRefused, members_only
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route

from .server import app as mcp_server
from .upload_probe import (
    upload as upload_probe,  # THROWAWAY: moneypenny plans/053 spike S3
)


def build(settings: Settings) -> HubGate:
    """The ASGI app: one route at `settings.path`, behind `HubGate`.

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
        routes=[Route(settings.path, endpoint=Endpoint(), methods=["GET", "POST", "DELETE"]),
                # THROWAWAY (053 S3): the token is its only lock; see upload_probe.py.
                Route("/upload/{token}", endpoint=upload_probe, methods=["PUT"])],
        lifespan=lifespan,
    )
    inner.router.redirect_slashes = False
    # No member list: there is one Paprika account and any member Q Branch names may use it,
    # so `members=None` takes the id Caddy names and a list here could only drift from the
    # household manifest. `MCP_HOST` and `MCP_PATH` are refused by `Settings`.
    return settings.build(inner, None)


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
