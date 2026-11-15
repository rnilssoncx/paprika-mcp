"""CLI utilities for paprika-mcp."""

import sys


def main():
    """Main CLI entry point."""
    if len(sys.argv) > 1 and sys.argv[1] == "http":
        from paprika_mcp.http import run

        run()
    else:
        # Start the server
        from paprika_mcp.server import run

        run()


if __name__ == "__main__":
    main()
