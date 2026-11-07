"""The HTTP mode: who may reach the tools, through the production `build()` (035).

Every request is signed the way Cloudflare Access signs it (a key generated here, an
injected clock) and the Q Branch listener is an `httpx` transport, so nothing here opens
a socket. The server under test is the real `paprika_mcp.server.app` with its real tool
list; only the tool *handlers* are replaced, by a spy, so that "the body never ran" is
something the test can count.
"""

import base64
import inspect
import json
from pathlib import Path

import httpx
import jwt
import pytest
from control import words
from control.identity import Settings, StartRefused
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from mcp.types import TextContent
from starlette.testclient import TestClient

from paprika_mcp import http, server
from paprika_mcp.tools import TOOLS

TEAM = "household.example.com"
AUD = "aud-paprika-0001"
HOST = "mcp.moneypenny.life"
NOW = 1_800_000_000.0
KID = "key-1"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ENV = {
    "ACCESS_TEAM": TEAM,
    "MCP_AUD": AUD,
    "MCP_HOST": HOST,
    "MCP_PATH": "/paprika",
    "CONTROL_SPOKE": "172.31.34.0/24",
    "CONTROL_URL": "http://172.31.34.2:3033",
    "MEMBERS": "alice,bob",
}
# A local address that is not on the spoke: the gate refuses `testserver` as "on the spoke".
CADDY_SIDE = "http://10.0.0.5"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def call(name, arguments=None):
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


class Plane(httpx.MockTransport):
    """A fake of Q Branch's `POST /lookup` and `GET /keys`, with a record of the lookups."""

    def __init__(self):
        super().__init__(self._handle)
        self.answer = {"kind": "member", "member": "alice", "role": "admin"}
        self.down = False
        self.lookups = 0

    def _handle(self, request):
        if self.down:
            raise httpx.ConnectError("down")
        if request.url.path == "/keys":
            jwk = json.loads(RSAAlgorithm.to_jwk(KEY.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": KID}]})
        self.lookups += 1
        return httpx.Response(200, json=self.answer)


def assertion(sub="sub-alice", aud=AUD):
    claims = {"iss": f"https://{TEAM}", "aud": [aud], "sub": sub,
              "iat": NOW - 100, "nbf": NOW - 10, "exp": NOW + 3600}
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})


def production_app(plane, env=None, members=None):
    settings = Settings.from_environ({**ENV, **(env or {})})
    return http.build(settings, members or http.members_from(ENV), clock=lambda: NOW,
                      transport=plane)


def client_for(plane, **kw):
    return TestClient(production_app(plane, **kw), base_url=CADDY_SIDE)


def post(client, body, path="/paprika", host=HOST, token="default", **headers):
    sent = {**HEADERS, "Host": host, **headers}
    if token == "default":
        token = assertion()
    if token is not None:
        sent["Cf-Access-Jwt-Assertion"] = token
    return client.post(path, headers=sent, json=body)


class Spy:
    """Stands in for every tool handler; the server's tool *list* stays the real one."""

    def __init__(self, monkeypatch):
        self.calls = []
        for name, tool in TOOLS.items():
            def handler(arguments, _name=name):
                self.calls.append(_name)
                return [TextContent(type="text", text="served")]

            monkeypatch.setitem(TOOLS, name, {**tool, "handler": handler})


def body_of(response):
    """The JSON-RPC message, whether the transport answered as JSON or as one SSE event."""
    if response.headers["content-type"].startswith("text/event-stream"):
        data = [ln[5:].strip() for ln in response.text.splitlines() if ln.startswith("data:")]
        return json.loads(data[-1])
    return response.json()


def tools(client):
    response = post(client, LIST_TOOLS)
    assert response.status_code == 200
    listed = body_of(response)["result"]["tools"]
    assert {t["name"] for t in listed} == set(TOOLS) and len(listed) > 5
    return listed


def tool_names(client):
    return [t["name"] for t in tools(client)]


def valid_arguments(tool):
    """Arguments the SDK's schema check accepts, read from the tool's own schema: the call
    reaches the (spied) handler instead of stopping at validation."""
    schema = tool["inputSchema"]
    sample = {"string": "x", "integer": 1, "number": 1, "boolean": True, "array": [],
              "object": {}}
    props = schema["properties"]
    return {n: props[n]["enum"][0] if "enum" in props[n] else sample[props[n].get("type", "string")]
            for n in schema.get("required", [])}


def text_of(response):
    result = body_of(response)["result"]
    return result["isError"], result["content"][0]["text"]


REFUSALS = [
    ("unknown", {"kind": "unknown"}, False, words.unknown_sign_in),
    ("unlisted", {"kind": "unlisted"}, False, words.unlisted_sign_in),
    ("down", None, True, words.cant_tell),
    # Q Branch names a member this server's MEMBERS does not list: can't tell, not served.
    ("not-in-members", {"kind": "member", "member": "carol", "role": "member"}, False,
     words.cant_tell),
]


@pytest.mark.parametrize(("answer", "down", "expected"), [r[1:] for r in REFUSALS],
                         ids=[r[0] for r in REFUSALS])
def test_every_tool_refuses_each_unservable_caller_with_its_words(monkeypatch, answer, down,
                                                                  expected):
    """The tool list is read from the server. Catches a tool that forgot, one refusal text for
    all, and the refusal happening after the body ran. **Verified by reverting `members_only`'s
    installation in `paprika_mcp/http.py` `build()`.**"""
    spy, plane = Spy(monkeypatch), Plane()
    plane.down = down
    if answer:
        plane.answer = answer
    with client_for(plane) as client:
        plane.down = False
        names = tool_names(client)   # listing is allowed for any signed-in caller
        plane.down = down
        for name in names:
            # A sub of its own per call: a kept answer (60 s) would hide the down listener.
            response = post(client, call(name), token=assertion(sub=f"sub-{name}"))
            assert response.status_code == 200
            assert text_of(response) == (True, expected)
    assert spy.calls == []


def test_a_member_reaches_every_tool(monkeypatch):
    """The control for the test above: a wrapper that refuses everyone passes that one."""
    spy = Spy(monkeypatch)
    with client_for(Plane()) as client:
        for tool in tools(client):
            response = post(client, call(tool["name"], valid_arguments(tool)))
            assert text_of(response) == (False, "served")
    assert sorted(spy.calls) == sorted(TOOLS)


def test_a_member_may_be_any_role(monkeypatch):
    spy, plane = Spy(monkeypatch), Plane()
    plane.answer = {"kind": "member", "member": "bob", "role": "member"}
    with client_for(plane) as client:
        assert text_of(post(client, call("list_categories"))) == (False, "served")
    assert spy.calls == ["list_categories"]


def test_a_tool_that_does_not_exist_is_refused_before_it_is_looked_up(monkeypatch):
    """A stranger must not learn which tool names exist."""
    Spy(monkeypatch)
    plane = Plane()
    plane.answer = {"kind": "unknown"}
    with client_for(plane) as client:
        assert text_of(post(client, call("nope"))) == (True, words.unknown_sign_in)


def test_the_endpoint_is_the_bare_path_from_mcp_path():
    with client_for(Plane()) as client:
        assert post(client, LIST_TOOLS, "/paprika").status_code == 200
        assert post(client, LIST_TOOLS, "/mcp").status_code == 404
        # A trailing slash is a 404, not a redirect to a URL Caddy never forwarded.
        slashed = post(client, LIST_TOOLS, "/paprika/")
        assert slashed.status_code == 404 and "location" not in slashed.headers


def test_the_route_follows_mcp_path():
    with client_for(Plane(), env={"MCP_PATH": "/recipes"}) as client:
        assert post(client, LIST_TOOLS, "/recipes").status_code == 200
        assert post(client, LIST_TOOLS, "/paprika").status_code == 404


def test_only_the_mcp_host_is_accepted():
    """Rebinding protection was never on here. Catches `security_settings` left off, or
    allowing the old hostname alongside the new."""
    with client_for(Plane()) as client:
        assert post(client, LIST_TOOLS, host=HOST).status_code == 200
        assert post(client, LIST_TOOLS, host="paprika-mcp.nilssonstudios.com").status_code == 421
        assert post(client, LIST_TOOLS, host="10.0.0.5").status_code == 421


@pytest.mark.parametrize("token", [None, "not-a-jwt", "wrong-audience"])
def test_a_request_without_a_valid_assertion_is_refused(monkeypatch, token):
    spy, plane = Spy(monkeypatch), Plane()
    token = assertion(aud="aud-someone-else") if token == "wrong-audience" else token
    with client_for(plane) as client:
        response = post(client, call("list_categories"), token=token)
    assert response.status_code == 403 and response.text.startswith("refused: access assertion")
    assert spy.calls == [] and plane.lookups == 0


def test_a_request_that_arrived_on_the_spoke_is_refused(monkeypatch):
    """Catches `IdentityGate` missing from the app **and** a gate that skips the spoke check.
    **Verified by reverting `settings.build(...)` in `paprika_mcp/http.py` `build()` to return the
    bare inner app.**"""
    spy = Spy(monkeypatch)
    with TestClient(production_app(Plane()), base_url="http://172.31.34.7") as client:
        response = post(client, call("list_categories"))
    assert (response.status_code, response.text) == (403, words.not_from_caddy)
    assert spy.calls == []


def test_the_old_bearer_token_is_not_read_anywhere(monkeypatch):
    src = Path(inspect.getfile(http)).parent
    assert not [p for p in src.rglob("*.py") if "PAPRIKA_MCP_TOKEN" in p.read_text()]
    # And a request carrying only what the old gate wanted is just an unsigned request.
    monkeypatch.setenv("PAPRIKA_MCP_TOKEN", "t" * 40)
    with client_for(Plane()) as client:
        response = post(client, LIST_TOOLS, token=None, Authorization="Bearer " + "t" * 40)
    assert response.status_code == 403


def test_the_shared_server_is_left_unwrapped(monkeypatch):
    """Stdio mode and the in-memory session tests use `server.app`, which has no caller to
    ask: wrapping it in place would refuse them all."""
    from mcp import types
    before = server.app.request_handlers[types.CallToolRequest]
    production_app(Plane())
    assert server.app.request_handlers[types.CallToolRequest] is before


def test_the_endpoint_needs_both_a_path_and_a_host():
    with pytest.raises(StartRefused, match="MCP_PATH and MCP_HOST"):
        production_app(Plane(), env={"MCP_HOST": ""})
    with pytest.raises(StartRefused, match="MCP_PATH and MCP_HOST"):
        production_app(Plane(), env={"MCP_PATH": ""})


@pytest.mark.parametrize(("drop", "text"), [
    ("ACCESS_TEAM", words.identity_missing),
    ("MCP_AUD", words.identity_missing),
    ("CONTROL_URL", "refused: CONTROL_URL is required"),
    ("MEMBERS", "refused: MEMBERS is required (a comma list of member ids)"),
])
def test_the_server_will_not_start_without_its_settings(monkeypatch, drop, text):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(drop)
    monkeypatch.setattr(http.uvicorn, "run", lambda *a, **k: pytest.fail("server started"))
    with pytest.raises(SystemExit) as stopped:
        http.run()
    assert str(stopped.value) == text


def test_the_server_starts_and_binds_to_localhost_by_default(monkeypatch):
    seen = {}
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PAPRIKA_MCP_HOST", raising=False)
    monkeypatch.setattr(http.uvicorn, "run", lambda app, host, port: seen.update(host=host))
    http.run()
    assert seen["host"] == "127.0.0.1"


def test_members_are_a_trimmed_comma_list():
    assert http.members_from({"MEMBERS": " alice , bob,,"}) == {"alice", "bob"}


def test_base64_is_not_how_the_assertion_is_read():
    """Identity is the verified claims. A forged payload with no signature is refused."""
    forged = ".".join(base64.urlsafe_b64encode(p).rstrip(b"=").decode()
                      for p in (b'{"alg":"none"}', json.dumps(
                          {"iss": f"https://{TEAM}", "aud": [AUD], "sub": "x"}).encode(), b""))
    with client_for(Plane()) as client:
        assert post(client, LIST_TOOLS, token=forged).status_code == 403
