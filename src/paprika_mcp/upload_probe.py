"""THROWAWAY: moneypenny plans/053 spike S3. Removed after the run.

The question: does a photo attached in Duet reach this server whole, when Duet's
code-execution sandbox `curl`s it to a link this server hands out? (Bytes copied
into a tool argument by the model are reported corrupted on claude.ai; 053 § Why a
link and not the bytes in the call.)

- `photo_probe_link` is an ordinary tool, behind the member check like every other.
  It answers a link `https://{MCP_HOST}/upload/{token}` and the `curl` line to use.
- `PUT /upload/{token}` is NOT behind the member check: Caddy proxies it without
  `verified`, and Cloudflare Access bypasses that one path, because the sandbox holds
  no member sign-in. The token is the only lock: an HMAC over an expiry five minutes
  out and a nonce, keyed by a secret made when the process starts (a restart voids
  every link), good for one upload.
- **It keeps nothing.** The body is hashed as it streams and dropped; the answer is
  its length, SHA-256 and first four bytes, for comparing with the sandbox's own.

Nothing here touches Paprika.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

from mcp.types import TextContent
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

TTL = 300  # seconds a link lives
CAP = 25 * 1024 * 1024  # Caddy's request_body max_size is the same; S1's photo was 1.19 MB
_KEY = secrets.token_bytes(32)
_USED: set[str] = set()
_LOCK = threading.Lock()


def _mac(body: str) -> str:
    return hmac.new(_KEY, body.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(now: float) -> str:
    body = f"{int(now) + TTL}.{secrets.token_hex(8)}"
    return f"{body}.{_mac(body)}"


def take(token: str, now: float) -> str | None:
    """Why `token` may not be used, or None after marking it used."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit():
        return "bad"
    body = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(_mac(body), parts[2]):
        return "bad"
    if now > int(parts[0]):
        return "expired"
    with _LOCK:
        if token in _USED:
            return "used"
        _USED.add(token)
    return None


def link_tool(arguments: dict) -> list[TextContent]:
    host = os.environ.get("MCP_HOST", "")
    url = f"https://{host}/upload/{make_token(time.time())}"
    return [TextContent(type="text", text=(
        "Upload link (works once, for 5 minutes):\n"
        f"{url}\n\n"
        "From code execution, send the attached file with:\n"
        f"curl -sS -T '<path of the attached file>' '{url}'\n\n"
        "Paste the answer exactly: it is the server's byte count and SHA-256 of what arrived."
    ))]


TOOL_DEFINITION = {
    "name": "photo_probe_link",
    "description": (
        "TEST ONLY (temporary): get a one-time link to upload an attached photo from code "
        "execution, to check it arrives intact. Nothing is saved anywhere."
    ),
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}


async def upload(request: Request) -> Response:
    why = take(request.path_params["token"], time.time())
    if why:
        logger.info("upload_probe refused why=%s", why)
        return Response(json.dumps({"refused": why}), status_code=403, media_type="application/json")
    digest, size, first = hashlib.sha256(), 0, b""
    async for chunk in request.stream():
        size += len(chunk)
        if size > CAP:
            logger.info("upload_probe refused why=too-large")
            return Response(json.dumps({"refused": "too-large"}), status_code=413, media_type="application/json")
        if len(first) < 4:
            first += chunk[: 4 - len(first)]
        digest.update(chunk)
    answer = {"bytes": size, "sha256": digest.hexdigest(), "first": first.hex(),
              "content_type": request.headers.get("content-type", "")}
    logger.info("upload_probe received bytes=%d sha256=%s", size, answer["sha256"])
    return Response(json.dumps(answer), media_type="application/json")
