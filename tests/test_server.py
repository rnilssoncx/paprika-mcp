"""The MCP server itself, over a real (in-memory) MCP session.

Handler tests call the handler functions directly, which proves nothing about
the server: a tool advertised under a name with no handler, a dispatcher that
never calls the handler, or one that blocks the event loop all pass them. These
go through `tools/list` and `tools/call` as a connector does.
"""

import threading

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from paprika_mcp.server import app
from paprika_mcp.tools import (
    TOOLS,
    list_categories,
    meal_plan,
    read_recipe,
    search_recipes,
    update_recipe,
)

OFFLINE = "offline: no Paprika in tests"

# One valid call per advertised tool. Kept as an explicit table, not derived
# from TOOLS, so that a tool added without a handler — or advertised under a
# different name — changes the advertised set and fails the first test below.
CALLS = {
    "search_recipes": {"query": ""},
    "read_recipe": {"id": "A1-0A"},
    "update_recipe": {"id": "A1-0A", "field": "notes", "find": "a", "replace": "b"},
    "list_categories": {},
    "format_fraction": {"fraction": "1/4"},
    "list_meals": {},
    "add_meal": {"date": "2026-10-26", "name": "Takeout"},
    "remove_meal": {"meal_id": "X"},
    "list_menus": {},
    "plan_menu": {"menu": "Feast", "start_date": "2026-10-26"},
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def offline(monkeypatch):
    """Every handler that would reach Paprika gets this error instead."""

    def refuse():
        raise RuntimeError(OFFLINE)

    for module in (search_recipes, read_recipe, update_recipe, list_categories, meal_plan):
        monkeypatch.setattr(module, "get_remote", refuse)


@pytest.mark.anyio
async def test_every_advertised_tool_reaches_its_handler(offline):
    async with create_connected_server_and_client_session(app) as client:
        advertised = {tool.name for tool in (await client.list_tools()).tools}
        assert advertised == set(CALLS)
        for name, arguments in CALLS.items():
            result = await client.call_tool(name, arguments)
            text = result.content[0].text
            # Reaching Paprika is where each handler first does real work, so
            # the offline error proves the call got that far — not swallowed by
            # the dispatcher, refused as unknown, or rejected by the schema.
            expected = "Formatted fraction: ¼" if name == "format_fraction" else f"Error: {OFFLINE}"
            assert text.startswith(expected), (name, text)


@pytest.mark.anyio
async def test_a_tool_name_with_no_handler_is_an_error(offline):
    async with create_connected_server_and_client_session(app) as client:
        result = await client.call_tool("delete_recipe", {})
        assert result.isError and result.content[0].text == "Unknown tool: delete_recipe"


@pytest.mark.anyio
async def test_a_slow_tool_call_does_not_stall_other_requests(monkeypatch):
    # The Paprika client is synchronous `requests`; a call can block for a
    # minute or more. This handler blocks the same way, until released. If it
    # ran on the event loop, list_tools could not be answered until it gave up
    # — so by then it would already have finished.
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow(arguments):
        started.set()
        release.wait(timeout=3)
        finished.set()
        return []

    definition = {"name": "slow", "description": "blocks", "inputSchema": {"type": "object", "properties": {}}}
    monkeypatch.setitem(TOOLS, "slow", {"definition": definition, "handler": slow})

    async with create_connected_server_and_client_session(app) as client:
        async with anyio.create_task_group() as tg:
            tg.start_soon(client.call_tool, "slow", {})
            assert await anyio.to_thread.run_sync(started.wait, 3)
            with anyio.fail_after(2):
                tools = await client.list_tools()
            still_running = not finished.is_set()
            release.set()
    assert "slow" in {t.name for t in tools.tools}
    assert still_running, "list_tools was answered only after the slow call finished"
