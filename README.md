# Paprika MCP Server

A Model Context Protocol (MCP) server for the Paprika Recipe Manager, allowing AI assistants to search, read, and update recipes.

## Features

- **Search Recipes**: Search across recipe titles, ingredients, categories, directions, and notes with context
- **Read Recipes**: Get full recipe data including all metadata, ingredients, and directions
- **Update Recipes**: Safely update recipe fields using find/replace (requires user confirmation)

## Prerequisites

1. Python 3.10 or higher (Python 3.13 recommended)
2. A Paprika account with recipes
3. Node.js (for pre-commit hooks, optional)

## Quick Start

Run the setup script to install everything and configure credentials:

```bash
cd paprika-mcp
./setup.sh
```

This will:
1. Install paprika-mcp with dependencies
2. Set up pre-commit hooks (if npm available)

## Manual Installation

If you prefer manual setup:

### 1. Install paprika-recipes

```bash
cd ../paprika-recipes
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
deactivate
```

### 2. Install paprika-mcp

```bash
cd ../paprika-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Credentials

The login is the `Paprika` item in the household's Services vault (moneypenny plan 039): the email is the item's username and the password its `password` field, copied into a read-only mount at `/vault` (`PAPRIKA_VAULT_DIR`) by the vault's fetcher and read on each call. `PAPRIKA_EMAIL`, `PAPRIKA_PASSWORD` and `~/.paprika-mcp/config.json` are no longer read, and the `paprika-mcp setup` command is gone. Run by hand on a machine with no vault copy (stdio mode), every tool answers that it cannot read its credentials yet.

## User-Agent

If you have Paprika for Mac installed, the fork of the `paprika-recipes` Python package should automatically create a suitable User-Agent string. Otherwise, set the `PAPRIKA_USER_AGENT` environment variable (not a secret).

## Usage

### As an MCP Server

Add to your MCP client configuration (e.g., Claude Desktop's `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "paprika": {
      "command": "/Users/yourusername/Developer/paprika-mcp/.venv/bin/paprika-mcp"
    }
  }
}
```

### Available Tools

#### format_fraction

Format a fraction string to unicode fraction characters. **This tool is local-only** and doesn't require Paprika server connectivity - useful for testing.

**Parameters:**
- `fraction` (required): Fraction in the form "numerator/denominator" (e.g., "1/4", " 31 / 200 "), or already formatted unicode

**Features:**
- Handles already-formatted unicode fractions (returns them as-is)
- Strips whitespace from input
- Converts common fractions to dedicated unicode characters
- Composes complex fractions using superscript/subscript digits

**Examples:**
```json
{
  "fraction": "1/4"
}
```
Returns: `¼`

```json
{
  "fraction": " 31 / 200 "
}
```
Returns: `³¹⁄₂₀₀` (whitespace stripped)

```json
{
  "fraction": "¼"
}
```
Returns: `¼` (already formatted, returned as-is)

Common fractions (1/4, 1/2, 3/4, 1/3, 2/3, etc.) use dedicated Unicode characters. Complex fractions are composed using superscript numerator + fraction slash (⁄) + subscript denominator.

#### search_recipes

Search for recipes by text across multiple fields.

**Parameters:**
- `query` (required): Text to search for
- `fields` (optional): Array of fields to search in: `["name", "ingredients", "categories", "directions", "notes"]`
- `context_lines` (optional): Number of context lines around matches (default: 2)

**Example:**
```json
{
  "query": "chicken",
  "fields": ["name", "ingredients"],
  "context_lines": 2
}
```

#### read_recipe

Read full recipe data by ID or title.

**Parameters:**
- `id` or `title` (one required): Recipe UUID or exact recipe name

**Note:** Title matching uses Unicode normalization (NFD), so it works correctly with accented characters regardless of their unicode representation (e.g., "café" will match "café"). Recipes in the trash are skipped, and if two live recipes share the title the tool lists their ids instead of picking one.

**Example:**
```json
{
  "id": "RECIPE-UUID-HERE"
}
```

or

```json
{
  "title": "Chocolate Chip Cookies"
}
```

### User Preferences (Prompts)

You can provide context to the AI about how you want it to work with your recipes by creating a `~/.paprika-mcp/prompt.md` file. This will be automatically loaded as a prompt when the MCP server starts.

**Example prompt file:**
```markdown
# Recipe Management Preferences

- Always preserve source URLs and attribution
- Prefer metric measurements
- I'm cooking for 2 people typically
- I avoid peanuts (allergy)
- Categorize using: Breakfast, Lunch, Dinner, Desserts, Snacks
```

See [`prompt.example.md`](prompt.example.md) for a complete template.

#### update_recipe

Update a recipe field using find/replace.

**⚠️ DANGEROUS**: This tool modifies recipe data. User confirmation is recommended before execution.

**Parameters:**
- `id` (required): Recipe UUID
- `field` (required): Field to update (name, ingredients, directions, notes, etc.)
- `find` (required): Text to find
- `replace` (required): Text to replace with
- `regex` (optional): Treat find pattern as regex (default: false)

An empty `find`, or a regex that can match empty text, is refused on a field that already has text (it would insert the replacement between every character); on an empty field, `find: ""` sets it. To replace a whole field, use `regex: true` with `find: "(?s).+"`. For `rating`, `find` is ignored and `replace` is the new rating, 0 to 5. After a successful edit the apps are notified, as the meal planner tools do.

**Example:**
```json
{
  "id": "RECIPE-UUID-HERE",
  "field": "ingredients",
  "find": "1 cup sugar",
  "replace": "3/4 cup sugar"
}
```

### Code Changes and Rebuilding

The package is installed in **editable mode** (`pip install -e .`), so:

- **✓ No rebuild needed**: Changes to `.py` files are immediately available
- **⚠️ Restart required**: MCP clients cache the stdio process - restart VS Code or your MCP client to pick up changes
- **↻ Reinstall needed**: Only for `pyproject.toml` or entry point changes

Force reinstall if needed:
```bash
.venv/bin/pip install -e . --force-reinstall --no-deps
```

### Pre-commit Hooks

Pre-commit hooks run automatically via Husky when you commit. They:
1. Only run on staged Python files
2. Run isort, black, and ruff
3. Auto-fix issues and re-stage files

To install hooks manually:
```bash
npm install
```

## Security Notes

- The server holds no credential in its environment or in a file of its own: the login is read from the vault copy on each call

## License

MIT

## Credits

Built on top of [paprika-recipes](https://github.com/briantkatch/paprika-recipes) originally by Adam Coddington.
