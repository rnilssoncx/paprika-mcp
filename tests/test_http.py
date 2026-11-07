"""The HTTP mode: who may reach the tools, through the production `build()` (037).

The server never sees an Access assertion: Caddy verified it at Q Branch's `/verify` and
forwarded four `X-Moneypenny-*` headers, which `HubGate` reads. A "caller" here is those
headers (`idfix.hub()`'s shape). The server under test is the real `paprika_mcp.server.app`
with its real tool list; only the tool *handlers* are replaced, by a spy, so that "the body
never ran" is something the test can count.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from control import words
from control.identity import Settings
from mcp.types import TextContent
from starlette.testclient import TestClient

from paprika_mcp import http, server
from paprika_mcp.tools import TOOLS

HOST = "mcp.moneypenny.life"
ENV = {"MCP_HOST": HOST, "MCP_PATH": "/paprika"}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def hub(kind="member", sub="sub-alice", member="alice", aud="mcp"):
    """The headers Caddy copies from `/verify`'s answer (the library's tests/idfix.py shape)."""
    return {"x-moneypenny-kind": kind, "x-moneypenny-sub": sub,
            "x-moneypenny-member": member if kind == "member" and member is not None else "",
            "x-moneypenny-aud": aud}


def call(name, arguments=None):
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


def production_app(env=None):
    return http.build(Settings.from_environ({**ENV, **(env or {})}))


def client_for(**kw):
    return TestClient(production_app(**kw), base_url="http://10.0.0.5")


def post(client, body, path="/paprika", host=HOST, who="default", **headers):
    sent = {**HEADERS, "Host": host, **headers}
    sent.update(hub() if who == "default" else (who or {}))
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
    ("unknown", hub(kind="unknown"), words.unknown_sign_in),
    ("unlisted", hub(kind="unlisted"), words.unlisted_sign_in),
    ("missing kind", {k: v for k, v in hub().items() if k != "x-moneypenny-kind"}, words.cant_tell),
    ("empty kind", hub(kind=""), words.cant_tell),
    ("member with empty member", hub(member=""), words.cant_tell),
    ("placeholder member", hub(member="{http.reverse_proxy.header.X-Moneypenny-Member}"),
     words.cant_tell),
]


@pytest.mark.parametrize(("who", "expected"), [r[1:] for r in REFUSALS],
                         ids=[r[0] for r in REFUSALS])
def test_every_tool_refuses_each_unservable_caller_with_its_words(monkeypatch, who, expected):
    """The tool list is read from the server. Catches a tool that forgot, one refusal text for
    all, and the refusal happening after the body ran. **Verified by reverting `members_only`'s
    installation in `paprika_mcp/http.py` `build()`.**"""
    spy = Spy(monkeypatch)
    with client_for() as client:
        names = tool_names(client)   # listing is allowed for any caller
        for name in names:
            response = post(client, call(name), who=who)
            assert response.status_code == 200
            assert text_of(response) == (True, expected)
    assert spy.calls == []


@pytest.mark.parametrize(("who", "expected"), [r[1:] for r in REFUSALS],
                         ids=[r[0] for r in REFUSALS])
def test_every_prompt_is_refused_in_the_same_words_for_a_caller_who_is_not_a_member(
    monkeypatch, who, expected,
):
    """`members_only` guards `prompts/get` as well as `tools/call` (the prompt handler reads a
    file from the container's home). Catches a guard on tools only. The refusal is a JSON-RPC
    error whose message is the words. **Verified by reverting `members_only`'s installation in
    `paprika_mcp/http.py` `build()`.**"""
    from paprika_mcp.prompts import PROMPTS
    spied = []

    async def handler(arguments):
        spied.append(arguments)
        return "served"

    for name, prompt in PROMPTS.items():
        monkeypatch.setitem(PROMPTS, name, {**prompt, "handler": handler})
    with client_for() as client:
        listed = post(client, {"jsonrpc": "2.0", "id": 1, "method": "prompts/list", "params": {}},
                      who=who)
        names = [p["name"] for p in body_of(listed)["result"]["prompts"]]
        assert set(names) == set(PROMPTS) and names
        for name in names:
            response = post(client, {"jsonrpc": "2.0", "id": 2, "method": "prompts/get",
                                     "params": {"name": name}}, who=who)
            assert body_of(response)["error"]["message"] == expected, name
    assert spied == []


def test_a_caller_with_no_headers_at_all_is_cant_tell(monkeypatch):
    """Catches a gate that treats absent headers as a member or as a transport error."""
    spy = Spy(monkeypatch)
    with client_for() as client:
        assert text_of(post(client, call("list_categories"), who=None)) == (True, words.cant_tell)
    assert spy.calls == []


def test_an_underscore_member_header_is_never_read_as_the_member(monkeypatch):
    """`X-Moneypenny_Member` is what a CGI-style lookup would fold onto the real name. Catches a
    lookup that normalises underscores: the caller sends kind `member` with the real member
    header empty and the underscore one filled."""
    spy = Spy(monkeypatch)
    who = {**hub(member=""), "x-moneypenny_member": "alice"}
    with client_for() as client:
        assert text_of(post(client, call("list_categories"), who=who)) == (True, words.cant_tell)
    assert spy.calls == []


def test_a_member_reaches_every_tool(monkeypatch):
    """The control for the tests above: a wrapper that refuses everyone passes those."""
    spy = Spy(monkeypatch)
    with client_for() as client:
        for tool in tools(client):
            response = post(client, call(tool["name"], valid_arguments(tool)))
            assert text_of(response) == (False, "served")
    assert sorted(spy.calls) == sorted(TOOLS)


def test_any_linked_member_is_served_with_no_list_here(monkeypatch):
    """`members=None`: the id Caddy names is taken as it is. Catches a hardcoded list."""
    spy = Spy(monkeypatch)
    with client_for() as client:
        who = hub(sub="sub-zed", member="zed")
        assert text_of(post(client, call("list_categories"), who=who)) == (False, "served")
    assert spy.calls == ["list_categories"]


def test_the_audience_must_be_mcp(monkeypatch):
    """Settings.build defaults `aud` to `mcp`: a request verified for the web audience is
    *can't tell* here. Catches a gate built with `aud=None`."""
    spy = Spy(monkeypatch)
    with client_for() as client:
        assert text_of(post(client, call("list_categories"), who=hub(aud="web"))) == (
            True, words.cant_tell)
    assert spy.calls == []


def test_the_server_is_stateless_so_a_session_id_is_never_a_way_into_another_sign_in(monkeypatch):
    """037 asks for a session-binding test (a second `sub` using the first's session gets 404).
    This server runs `stateless=True`: no session exists to take over, and a session id sent
    by a second sign-in is not honoured, so the same call is served or refused on that
    sign-in's own headers. Catches a switch to stateful sessions without the binding."""
    spy = Spy(monkeypatch)
    with client_for() as client:
        first = post(client, LIST_TOOLS, who=hub(sub="sub-1"))
        assert "mcp-session-id" not in first.headers
        second = post(client, call("list_categories"), who=hub(sub="sub-2"),
                      **{"Mcp-Session-Id": "borrowed"})
        assert text_of(second) == (False, "served")
    assert spy.calls == ["list_categories"]


def test_the_endpoint_is_the_bare_path_from_mcp_path():
    with client_for() as client:
        assert post(client, LIST_TOOLS, "/paprika").status_code == 200
        assert post(client, LIST_TOOLS, "/mcp").status_code == 404
        # A trailing slash is a 404, not a redirect to a URL Caddy never forwarded.
        slashed = post(client, LIST_TOOLS, "/paprika/")
        assert slashed.status_code == 404 and "location" not in slashed.headers


def test_the_route_follows_mcp_path():
    with client_for(env={"MCP_PATH": "/recipes"}) as client:
        assert post(client, LIST_TOOLS, "/recipes").status_code == 200
        assert post(client, LIST_TOOLS, "/paprika").status_code == 404


def test_only_the_mcp_host_is_accepted():
    """Catches `security_settings` left off, or allowing the old hostname alongside the new."""
    with client_for() as client:
        assert post(client, LIST_TOOLS, host=HOST).status_code == 200
        assert post(client, LIST_TOOLS, host="paprika-mcp.nilssonstudios.com").status_code == 421
        assert post(client, LIST_TOOLS, host="10.0.0.5").status_code == 421


def test_nothing_about_access_is_read_from_the_source():
    """037: a server holds no team, audience, keys or Q Branch address. Catches a leftover read."""
    src = Path(http.__file__).parent
    gone = ("ACCESS_TEAM", "MCP_AUD", "CONTROL_URL", "CONTROL_SPOKE", "IdentityGate",
            "Cf-Access", "PAPRIKA_MCP_TOKEN")
    assert not [(p.name, w) for p in src.rglob("*.py") for w in gone if w in p.read_text()]


def test_the_shared_server_is_left_unwrapped():
    """Stdio mode and the in-memory session tests use `server.app`, which has no caller to
    ask: wrapping it in place would refuse them all."""
    from mcp import types
    before = server.app.request_handlers[types.CallToolRequest]
    production_app()
    assert server.app.request_handlers[types.CallToolRequest] is before


@pytest.mark.parametrize(("drop", "text"), [
    ("MCP_HOST", words.mcp_host_missing),
    ("MCP_PATH", words.mcp_path_missing),
])
def test_the_server_will_not_start_without_its_settings(monkeypatch, drop, text):
    """The library's words for the endpoint: this server holds no refusal of its own."""
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
    monkeypatch.setattr(http.uvicorn, "run", lambda app, host, port, access_log: seen.update(host=host, log=access_log))
    http.run()
    # Caddy writes the access line already: the server's own is off.
    assert seen == {"host": "127.0.0.1", "log": False}


def test_a_refusal_writes_one_control_refused_line_to_stderr_of_the_real_entry_point(tmp_path):
    """The server is started as `paprika-mcp http`, the console script, in a subprocess, so the
    logging the library attaches is the only logging there is. Six servers wrote no line under
    035. Catches a server whose refusal is silent in production. **Verified by reverting
    `HubGate`/`members_only` in `paprika_mcp/http.py` `build()`.**"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    exe = Path(sys.executable).parent / "paprika-mcp"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), **ENV,
           "PAPRIKA_MCP_PORT": str(port)}
    proc = subprocess.Popen([str(exe), "http"], env=env, stderr=subprocess.PIPE, text=True)
    try:
        url = f"http://127.0.0.1:{port}/paprika"
        sent = {**HEADERS, "Host": HOST, **hub(kind="unknown")}
        deadline, reply = time.time() + 8, None
        while reply is None and time.time() < deadline:
            try:
                reply = httpx.post(url, headers=sent, json=call("list_categories"))
            except httpx.TransportError:
                time.sleep(0.2)
        assert reply is not None and text_of(reply) == (True, words.unknown_sign_in)
    finally:
        proc.terminate()
        err = proc.communicate(timeout=8)[1]
    lines = [ln for ln in err.splitlines() if "control.refused" in ln]
    assert len(lines) == 1 and "kind=unknown" in lines[0] and "reason=not_a_member" in lines[0], err
