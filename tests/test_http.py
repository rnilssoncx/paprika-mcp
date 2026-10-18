"""The HTTP mode serves the tools only to a request carrying the token."""

import pytest
from starlette.testclient import TestClient

from paprika_mcp import http

TOKEN = "t" * 40
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def post(client, authorization=None):
    headers = dict(HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    return client.post("/mcp", headers=headers, json=LIST_TOOLS)


@pytest.fixture
def client():
    with TestClient(http.build(TOKEN)) as c:
        yield c


def test_no_token_is_refused(client):
    assert post(client).status_code == 401


def test_a_wrong_token_is_refused(client):
    assert post(client, "Bearer " + "x" * 40).status_code == 401


def test_a_prefix_of_the_token_is_refused(client):
    # A comparison that stops at the shorter string would let this through.
    assert post(client, "Bearer " + TOKEN[:10]).status_code == 401


def test_the_token_without_the_bearer_scheme_is_refused(client):
    assert post(client, TOKEN).status_code == 401


def test_the_right_token_reaches_the_tools(client):
    response = post(client, f"Bearer {TOKEN}")
    assert response.status_code == 200
    assert "search_recipes" in response.text


@pytest.mark.parametrize("token", ["", "short"])
def test_the_server_will_not_start_without_a_long_token(monkeypatch, token):
    monkeypatch.setenv("PAPRIKA_MCP_TOKEN", token)
    monkeypatch.setattr(http.uvicorn, "run", lambda *a, **k: pytest.fail("server started"))
    with pytest.raises(SystemExit):
        http.run()


def test_the_server_binds_to_localhost_by_default(monkeypatch):
    seen = {}
    monkeypatch.setenv("PAPRIKA_MCP_TOKEN", TOKEN)
    monkeypatch.delenv("PAPRIKA_MCP_HOST", raising=False)
    monkeypatch.setattr(http.uvicorn, "run", lambda app, host, port: seen.update(host=host))
    http.run()
    assert seen["host"] == "127.0.0.1"
