"""Serve the MCP tools over Streamable HTTP, behind a bearer token.

For clients that cannot launch a local process, such as cloud-hosted MCP
connectors. There is one token because there is one Paprika account: whoever
holds the token reaches the whole library, so it is required, it must be long,
and the server binds to localhost unless told otherwise.

    PAPRIKA_MCP_TOKEN=... paprika-mcp http
"""

import contextlib
import hmac
import os

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .server import app as mcp_server

MIN_TOKEN_LENGTH = 32


class _BearerGate:
    """Pass a request to the MCP transport only if it carries the token."""

    def __init__(self, manager: StreamableHTTPSessionManager, token: str):
        self._manager = manager
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        given = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(given, self._expected):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self._manager.handle_request(scope, receive, send)


def build(token: str) -> Starlette:
    """The ASGI app: one route, /mcp, behind the bearer gate."""
    manager = StreamableHTTPSessionManager(app=mcp_server, stateless=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    return Starlette(
        routes=[Route("/mcp", endpoint=_BearerGate(manager, token), methods=["GET", "POST", "DELETE"])],
        lifespan=lifespan,
    )


def run():
    """Entry point for `paprika-mcp http`."""
    token = os.environ.get("PAPRIKA_MCP_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit(
            f"PAPRIKA_MCP_TOKEN must be set to a random value of at least {MIN_TOKEN_LENGTH} characters."
        )
    host = os.environ.get("PAPRIKA_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("PAPRIKA_MCP_PORT", "3018"))
    uvicorn.run(build(token), host=host, port=port)
