"""MCP server for Paprika recipe manager."""

import asyncio
import logging

import anyio.to_thread
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Prompt, Tool

from .prompts import PROMPTS
from .tools import TOOLS

logger = logging.getLogger(__name__)

# Create server instance
app = Server("paprika")


@app.list_prompts()
async def list_prompts():
    """List available prompts."""
    return [Prompt(**prompt["definition"]) for prompt in PROMPTS.values()]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict = None):
    """Get prompt content."""
    if name in PROMPTS:
        return await PROMPTS[name]["handler"](arguments or {})
    raise ValueError(f"Unknown prompt: {name}")


@app.list_tools()
async def list_tools():
    """List available tools."""
    return [Tool(**tool["definition"]) for tool in TOOLS.values()]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    """Handle tool calls, each on a worker thread.

    The handlers are synchronous because everything they do blocks: the
    Paprika client is `requests`, with a 30 s timeout and three retries with
    backoff (a minute or two before it gives up), and a cold recipe cache is a
    GET per recipe. Called inline on the event loop, one such call froze every
    other request the server was handling — another member's conversation, a
    tools/list, the HTTP transport itself. On a worker thread only the caller
    waits. Concurrent calls share the recipe cache directory, which is why
    `utils._SharedDirectoryCache` writes atomically.
    """
    if name in TOOLS:
        try:
            return await anyio.to_thread.run_sync(TOOLS[name]["handler"], arguments)
        except Exception as e:
            logger.error(f"Error in {name}: {e}", exc_info=True)
            from mcp.types import TextContent

            return [TextContent(type="text", text=f"Error: {str(e)}")]
    raise ValueError(f"Unknown tool: {name}")


async def main():
    """Run the MCP server using stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def run():
    """Entry point for the server."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
