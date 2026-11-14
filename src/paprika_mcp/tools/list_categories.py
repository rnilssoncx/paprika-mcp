"""List categories tool - retrieves all recipe categories."""

from typing import Any

from mcp.types import TextContent

from ..utils import CategoriesUnavailable, get_categories, get_remote, normalize_string


def list_categories_tool(args: dict[str, Any]) -> list[TextContent]:
    """List all recipe categories with their names and hierarchical structure."""
    remote = get_remote()

    # A failed fetch is an error, not "No categories found": the second is a
    # statement about the account, and the request said nothing about it.
    try:
        categories_data = get_categories(remote)
    except CategoriesUnavailable as e:
        return [TextContent(type="text", text=f"Error: {e}.")]
    categories = categories_data["all"]

    if not categories:
        return [TextContent(type="text", text="No categories found")]

    # Sort categories by name
    categories.sort(key=lambda c: c.get("name", "").lower())

    # Build hierarchical structure
    root_categories: list[dict[str, Any]] = []
    child_categories: dict[str, list[dict[str, Any]]] = {}

    for cat in categories:
        parent_uid = cat.get("parent_uid")
        if parent_uid:
            if parent_uid not in child_categories:
                child_categories[parent_uid] = []
            child_categories[parent_uid].append(cat)
        else:
            root_categories.append(cat)

    # Format output
    output_lines = [f"Found {len(categories)} categories:\n"]

    def format_category(cat, indent=0):
        """Recursively format a category and its children."""
        lines = []
        prefix = "  " * indent
        name = cat.get("name", "Unnamed")
        uid = cat["uid"]
        lines.append(f"{prefix}- {name}")

        # Add children
        if uid in child_categories:
            for child in sorted(
                child_categories[uid], key=lambda c: c.get("name", "").lower()
            ):
                lines.extend(format_category(child, indent + 1))

        return lines

    # Format root categories and their children
    output_lines.append("## Hierarchical View:")
    for cat in root_categories:
        output_lines.extend(format_category(cat))

    # Also add a flat list at the end for easy reference
    output_lines.append("\n## Flat List (for reference):")
    categories_by_uid = {c["uid"]: c for c in categories}
    # A name two categories share cannot be given to any tool, which refuses it
    # and asks for an id — so the id is shown beside exactly those names, here
    # and not in the tree. Compared as the lookup compares (case and accents
    # ignored). Unique names keep their old line.
    folded = [normalize_string(c.get("name") or "") for c in categories]
    for cat, key in zip(categories, folded, strict=True):
        name = cat.get("name", "Unnamed")
        shared = bool(key) and folded.count(key) > 1
        parent_uid = cat.get("parent_uid")
        if parent_uid:
            parent_name = categories_by_uid.get(parent_uid, {}).get("name", "Unknown")
            detail = f"Parent: {parent_name}, id {cat['uid']}" if shared else f"Parent: {parent_name}"
        else:
            detail = f"id {cat['uid']}" if shared else ""
        output_lines.append(f"- {name} ({detail})" if detail else f"- {name}")

    return [TextContent(type="text", text="\n".join(output_lines))]


# Tool definition
TOOL_DEFINITION = {
    "name": "list_categories",
    "description": (
        "List all recipe categories with their text names and hierarchical structure. "
        "Categories are organized in a parent-child hierarchy. "
        "Returns both a hierarchical view and a flat list with category names. "
        "Use this to find category names for searching or to understand the category organization."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}
