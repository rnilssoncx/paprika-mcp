"""Update recipe tool - modifies recipe fields using find/replace."""

import re
from typing import Any

import regex as regex_module
from mcp.types import TextContent

from ..utils import (
    EDIT_TIMEOUT,
    CategoriesUnavailable,
    PatternTimeout,
    UserPattern,
    compile_pattern,
    get_categories,
    get_remote,
    notify_apps,
)

# The text fields find/replace works on. Checked in the handler, not only in the
# schema: the MCP library validates arguments against the schema, but that is a
# property of the transport, and a field outside this set reaches `setattr` on
# the recipe — `uid` uploads a copy of the recipe under a new id.
TEXT_FIELDS = (
    "name",
    "ingredients",
    "directions",
    "notes",
    "description",
    "source",
    "source_url",
    "prep_time",
    "cook_time",
    "total_time",
    "servings",
    "difficulty",
    "nutritional_info",
)
FIELDS = TEXT_FIELDS + ("categories", "rating")

# Refusals, part of the tool's contract: tests assert these words.
UNKNOWN_FIELD = "Error: Field '{field}' cannot be updated. Updatable fields: " + ", ".join(FIELDS) + "."
BAD_RATING = "Error: A rating is a whole number from 0 to 5, given as replace; got '{replace}'. No changes were saved."
EMPTY_MATCH = (
    "Error: find '{find}' matches empty text in field '{field}' of recipe '{name}', which would insert "
    "the replacement at every empty match (between every character, for an empty find). No changes were saved. "
    "To append, find the field's last words and replace them with those words plus the addition. "
    "To set the whole field, use regex=true with find '(?s).+'."
)
# The find is compiled before the recipe is fetched, so an error from the
# substitution is the replacement's: `\2` with one group, or `\g<qty>` naming
# none (which `regex`, like `re`, raises as IndexError rather than its error).
BAD_REPLACEMENT = "Error: Invalid replacement '{replace}': {error}. No changes were saved."
# The most an edit may write into one field. A replacement is multiplied by
# every match, so `(?s)(.+)` with `\1` repeated, or `.` with a long literal,
# builds hundreds of megabytes from a request of a few hundred kilobytes —
# within the match deadline, which bounds time, not size — and the whole value
# is then uploaded and echoed back. Far above any real recipe field; a field
# already longer than this can still be edited, as long as it does not grow.
FIELD_LIMIT = 64_000
TOO_LONG = (
    "Error: That edit would make field '{field}' of recipe '{name}' {length} characters long; "
    "an edit may write at most {limit}. No changes were saved."
)


def _text(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=message)]


def matches_empty(text: str, find: str, pattern: UserPattern | None) -> bool:
    """Would replacing `find` in `text` insert at a zero-width match?

    `str.replace("", x)` and `re.sub` with a pattern that can match nothing
    (`x*`, `(a)?`, `^`, `\\b`) both insert the replacement at every empty match:
    "Salt well" with find "" and replace "!" uploads "!S!a!l!t! !w!e!l!l!".
    Only a NON-empty field is refused — on an empty field an empty find is the
    one way to give it a value, and it inserts exactly once.
    """
    if not text:
        return False
    if pattern is None:
        return find == ""
    return any(m.start() == m.end() for m in pattern.finditer(text))


def _replace(text: str, find: str, replace: str, pattern: UserPattern | None) -> str:
    return pattern.sub(replace, text) if pattern is not None else text.replace(find, replace)


def update_recipe_tool(args: dict[str, Any]) -> list[TextContent]:
    """Update recipe fields using find/replace - DANGEROUS operation."""
    recipe_id = args["id"]
    field = args["field"]
    find = args["find"]
    replace = args["replace"]
    use_regex = args.get("regex", False)

    if field not in FIELDS:
        return _text(UNKNOWN_FIELD.format(field=field))

    new_rating = None
    if field == "rating":
        # A rating is an int, so find/replace on its text cannot work: `find` is
        # ignored and `replace` is the new rating. Checked before any request.
        if replace.strip() not in {"0", "1", "2", "3", "4", "5"}:
            return _text(BAD_RATING.format(replace=replace))
        new_rating = int(replace.strip())

    # Through the same helper as search, for its cap and its deadline: this is
    # the second place a member's pattern meets recipe text. Case-sensitive,
    # as it always was here — an edit replaces exactly what it finds.
    pattern = None
    if use_regex and field != "rating":
        try:
            pattern = compile_pattern(find, ignore_case=False)
        except ValueError as e:
            return _text(str(e))

    # Get the remote
    remote = get_remote()

    # Fetch the recipe
    recipe = None
    for r in remote.recipes:
        if r.uid == recipe_id:
            recipe = r
            break

    if not recipe:
        return _text(f"Error: No recipe found with ID '{recipe_id}'")

    if field == "rating":
        old_rating = recipe.rating or 0
        if new_rating == old_rating:
            return _text(f"No changes made - recipe '{recipe.name}' is already rated {old_rating}")
        recipe.rating = new_rating
        return _upload(remote, recipe, f"Successfully updated field 'rating' in recipe '{recipe.name}' (ID: {recipe_id})\n\nOld value:\n{old_rating}\n\nNew value:\n{new_rating}")

    # Special handling for categories field (stored internally as a UUID list)
    if field == "categories":
        try:
            categories_info = get_categories(remote)
        except CategoriesUnavailable as e:
            return _text(f"Error: {e}. No changes were saved.")
        uid_to_name = categories_info["uid_to_name"]
        name_to_uid = categories_info["name_to_uid"]

        # Convert UUID list to newline-separated names for find/replace
        current_uids = recipe.categories if isinstance(recipe.categories, list) else []
        current_names = "\n".join(
            uid_to_name.get(uid, f"Unknown-{uid[:8]}") for uid in current_uids
        )

        try:
            if matches_empty(current_names, find, pattern):
                return _text(EMPTY_MATCH.format(find=find, field=field, name=recipe.name))
            new_names = _replace(current_names, find, replace, pattern)
        except PatternTimeout:
            return _text(EDIT_TIMEOUT.format(title=recipe.name))
        except (regex_module.error, IndexError) as e:
            return _text(BAD_REPLACEMENT.format(replace=replace, error=e))

        if len(new_names) > FIELD_LIMIT and len(new_names) > len(current_names):
            return _text(TOO_LONG.format(field=field, name=recipe.name, length=len(new_names), limit=FIELD_LIMIT))

        # Collapse any double-newlines left after removal
        new_names = re.sub(r"\n{2,}", "\n", new_names).strip()

        if new_names == current_names.strip():
            return _text(f"No changes made - pattern '{find}' not found in categories of recipe '{recipe.name}'")

        # Convert names back to UUIDs
        new_uid_list = []
        for name in new_names.split("\n"):
            name = name.strip()
            if not name:
                continue
            uid = name_to_uid.get(name.lower())
            if uid:
                new_uid_list.append(uid)
            else:
                return _text(f"Error: Category '{name}' not found in Paprika — cannot convert back to UUID. No changes were saved.")

        recipe.categories = new_uid_list
        return _upload(remote, recipe, f"Successfully updated categories in recipe '{recipe.name}' (ID: {recipe_id})\n\nOld categories:\n{current_names}\n\nNew categories:\n{new_names}")

    # A text field Paprika sent as null is empty, and an empty find fills it.
    field_value = getattr(recipe, field) or ""

    # A timeout uploads nothing: it is caught before `setattr`, and it is not
    # "No changes made", which would claim the pattern is absent from the field.
    try:
        if matches_empty(field_value, find, pattern):
            return _text(EMPTY_MATCH.format(find=find, field=field, name=recipe.name))
        new_value = _replace(field_value, find, replace, pattern)
    except PatternTimeout:
        return _text(EDIT_TIMEOUT.format(title=recipe.name))
    except (regex_module.error, IndexError) as e:
        return _text(BAD_REPLACEMENT.format(replace=replace, error=e))

    # Check if anything changed
    if new_value == field_value:
        return _text(f"No changes made - pattern '{find}' not found in field '{field}' of recipe '{recipe.name}'")

    if len(new_value) > FIELD_LIMIT and len(new_value) > len(field_value):
        return _text(TOO_LONG.format(field=field, name=recipe.name, length=len(new_value), limit=FIELD_LIMIT))

    # Update the field
    setattr(recipe, field, new_value)
    return _upload(remote, recipe, f"Successfully updated field '{field}' in recipe '{recipe.name}' (ID: {recipe_id})\n\nOld value:\n{field_value}\n\nNew value:\n{new_value}")


def _upload(remote, recipe, success: str) -> list[TextContent]:
    """Upload, then ask Paprika to tell the apps — as the meal tools do.

    Without the notify an edit sits in Paprika's cloud until each app happens to
    sync, so the member who asked for it does not see it on the kitchen iPad.
    A failed notify is reported after the success, never as a failed write.
    """
    try:
        remote.upload_recipe(recipe)
    except Exception as e:
        # The library's upload is a POST followed by a GET of the result, so an
        # error here does not say the POST failed — only that we cannot tell.
        return _text(
            f"Error updating recipe '{recipe.name}': {str(e)}. "
            "The edit may or may not have reached Paprika; read the recipe before trying again."
        )
    return _text(success + notify_apps(remote))


# Tool definition
TOOL_DEFINITION = {
    "name": "update_recipe",
    "description": (
        "Update recipe fields using find/replace. "
        "This is a DANGEROUS operation that requires user confirmation. "
        "Can update any text field in a recipe. "
        "An empty find (or a regex that can match nothing) is refused on a field that has text; "
        "on an empty field, find '' sets it. "
        "For rating, find is ignored and replace is the new rating, a whole number 0 to 5."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Recipe UID to update"},
            "field": {
                "type": "string",
                "enum": list(FIELDS),
                "description": "Field to update",
            },
            "find": {"type": "string", "description": "Text to find in the field"},
            "replace": {"type": "string", "description": "Text to replace it with"},
            "regex": {
                "type": "boolean",
                "description": "Whether to treat 'find' as a regex pattern (default: false)",
                "default": False,
            },
        },
        "required": ["id", "field", "find", "replace"],
    },
}
