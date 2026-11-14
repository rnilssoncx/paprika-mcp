"""Create a recipe from fields the caller fills in (moneypenny plans/051).

Duet reads a web page and passes the recipe's fields; the server fetches
nothing and parses nothing, so this opens no trust boundary that `update_recipe`
(which writes any text) does not already open. Spiked 2026-11-14: a
`RemoteRecipe` with the library's fresh uid, through `Remote.upload_recipe`,
creates the recipe — in the list, readable by uid, its category kept.

Split the way meal_plan is: `decide_create` takes the arguments and what was
read from Paprika and returns the recipe to upload or raises `Refused` with the
answer; the handler fetches, calls it, uploads and notifies.
"""

from collections.abc import Callable
from typing import Any

from mcp.types import TextContent
from paprika_recipes.remote import RemoteRecipe

from ..utils import (
    AmbiguousTitle,
    CategoriesUnavailable,
    CategoryRefused,
    all_recipes,
    find_by_title,
    get_categories,
    get_remote,
    notify_apps,
    resolve_category,
)
from .update_recipe import FIELD_LIMIT, FIELDS, TEXT_FIELDS

# What a caller may set: `name`, the other text fields, the categories and the
# rating (update_recipe's FIELDS), then `keep_both`, which is not a recipe field.
ACCEPTED = FIELDS + ("keep_both",)


class Refused(Exception):
    """A create the tool will not carry out; the message is the answer."""


# The words — contract, tests assert them (plans/051 § The words).
CREATED = 'Created recipe "{name}" (ID: {uid}). Categories: {names}.'
CREATED_BARE = 'Created recipe "{name}" (ID: {uid}). No categories.'
NO_NAME = "Error: A recipe needs a name. Nothing was created."
UNKNOWN_ARG = "Error: '{arg}' cannot be set when creating a recipe. Fields: {fields}. Nothing was created."
TOO_LONG = "Error: Field '{field}' is {length} characters; a field may hold at most {limit}. Nothing was created."
BAD_RATING = "Error: A rating is a whole number from 0 to 5; got {rating!r}. Nothing was created."
BAD_KEEP_BOTH = "Error: keep_both is true or false; got {value!r}. Nothing was created."
KEEP_HINT = "To add this one as well, call create_recipe again with keep_both=true."
NAME_TAKEN = 'Error: A recipe titled "{existing}" already exists (ID: {uid}). Nothing was created. ' + KEEP_HINT
NAMES_TAKEN = 'Error: {n} recipes titled "{name}" already exist (IDs: {uids}). Nothing was created. ' + KEEP_HINT
RECIPES_UNREADABLE = (
    'Error: Could not read the recipes to check whether "{name}" already exists ({error}). Nothing was created.'
)
CATEGORIES_UNREADABLE = "Error: {reason}. Nothing was created."
CATEGORY_REFUSED = "{sentence} Nothing was created."
UPLOAD_UNSURE = (
    'Error creating recipe "{name}": {error}. It may or may not have reached Paprika; search for it before trying again.'
)


def check_args(args: dict[str, Any]) -> tuple[str, int, bool]:
    """Everything that can be refused without asking Paprika: (name, rating, keep_both).

    Its own function because the handler runs it before `get_remote()`: a
    request that is wrong on its face fetches nothing, and a failure to reach
    Paprika is never mistaken for one of the refusals below.
    """
    # Refused, not dropped: `uid`, `hash`, `in_trash`, `created` and the photo
    # fields are the server's or Paprika's to set, and a caller that sends one
    # should hear it, not see it vanish.
    for arg in args:
        if arg not in ACCEPTED:
            raise Refused(UNKNOWN_ARG.format(arg=arg, fields=", ".join(ACCEPTED)))

    # Stripped first: the stripped name is what is stored and compared, so
    # " Chili" is checked against a live "Chili".
    raw_name = args.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not name:
        raise Refused(NO_NAME)

    # Bounded though nothing multiplies here, for a different reason than
    # update_recipe's: a scrape that went wrong (a whole page's text in
    # `directions`) would land in the recipe book and in every later read_recipe.
    # A non-string never reaches this: the MCP library's schema check refuses
    # it before the handler.
    for field in TEXT_FIELDS:
        value = name if field == "name" else args.get(field)
        if value is not None and len(value) > FIELD_LIMIT:
            raise Refused(TOO_LONG.format(field=field, length=len(value), limit=FIELD_LIMIT))

    # `bool` is an `int`, so `isinstance(True, int)` alone would take True as 1.
    rating = args.get("rating", 0)
    if not isinstance(rating, int) or isinstance(rating, bool) or not 0 <= rating <= 5:
        raise Refused(BAD_RATING.format(rating=rating))
    keep_both = args.get("keep_both", False)
    if not isinstance(keep_both, bool):
        raise Refused(BAD_KEEP_BOTH.format(value=keep_both))
    return name, rating, keep_both


def decide_create(
    args: dict[str, Any],
    categories: Callable[[], list[dict[str, Any]]],
    recipes: Callable[[], list[Any]],
) -> tuple[RemoteRecipe, list[str]]:
    """The recipe to upload and the names of its categories as resolved, or Refused.

    `categories` and `recipes` are read only when needed: a create that names no
    category never fetches them, and `keep_both=true` never reads the recipes,
    because nothing is being checked. Each raises as the fetch failed, and each
    failure is a refusal — a duplicate check that could not run is *can't tell*,
    not "no duplicate" (the absence rule).
    """
    name, rating, keep_both = check_args(args)

    uids: list[str] = []
    names: list[str] = []
    given = args.get("categories") or []
    if given:
        try:
            known = categories()
        except CategoriesUnavailable as e:
            raise Refused(CATEGORIES_UNREADABLE.format(reason=e)) from None
        for each in given:
            try:
                found = resolve_category(known, each, create_hint=True)
            except CategoryRefused as r:
                raise Refused(CATEGORY_REFUSED.format(sentence=r)) from None
            # In order, once each: Dinner, dinner and Dinner's id are one.
            if found["uid"] not in uids:
                uids.append(found["uid"])
                names.append(found["name"])

    if not keep_both:
        try:
            live = recipes()
        except Exception as e:
            raise Refused(RECIPES_UNREADABLE.format(name=name, error=f"{type(e).__name__}: {e}")) from None
        try:
            existing = find_by_title(live, name)
        except AmbiguousTitle as many:
            raise Refused(
                NAMES_TAKEN.format(n=len(many.matches), name=name, uids=", ".join(m.uid for m in many.matches))
            ) from None
        if existing is not None:
            raise Refused(NAME_TAKEN.format(existing=existing.name, uid=existing.uid))

    # Built by iterating the accepted field tuple, never from the arguments, as
    # apply_restore iterates FIELDS: `keep_both` and anything unknown cannot
    # reach RemoteRecipe. The uid, created and hash are the library's defaults.
    values: dict[str, Any] = {}
    for field in TEXT_FIELDS:
        value = name if field == "name" else args.get(field)
        if value is not None:
            values[field] = value
    return RemoteRecipe(**values, categories=uids, rating=rating), names


def create_recipe_tool(args: dict[str, Any]) -> list[TextContent]:
    """Create a recipe from the given fields."""
    try:
        check_args(args)
    except Refused as r:
        return [TextContent(type="text", text=str(r))]
    remote = get_remote()
    try:
        recipe, names = decide_create(
            args,
            lambda: list(get_categories(remote)["all"]),
            lambda: all_recipes(remote),
        )
    except Refused as r:
        return [TextContent(type="text", text=str(r))]
    try:
        remote.upload_recipe(recipe)
    except Exception as e:
        # The library's upload is a POST followed by a GET of the result, so an
        # error here does not say the POST failed — only that we cannot tell. A
        # retry mints a new uid, and the name check catches it.
        return [TextContent(type="text", text=UPLOAD_UNSURE.format(name=recipe.name, error=e))]
    if names:
        answer = CREATED.format(name=recipe.name, uid=recipe.uid, names=", ".join(names))
    else:
        answer = CREATED_BARE.format(name=recipe.name, uid=recipe.uid)
    return [TextContent(type="text", text=answer + notify_apps(remote))]


# ---- definition -------------------------------------------------------------------------

_TEXT = {
    "ingredients": "Ingredients, one per line",
    "directions": "Directions",
    "notes": "Notes",
    "description": "A short description",
    "source": "Where the recipe came from, e.g. a site's name",
    "source_url": "The web address it came from",
    "prep_time": "Prep time, as text",
    "cook_time": "Cook time, as text",
    "total_time": "Total time, as text",
    "servings": "Servings, as text",
    "difficulty": "Difficulty",
    "nutritional_info": "Nutritional information",
}

TOOL_DEFINITION = {
    "name": "create_recipe",
    "description": (
        "Create a new recipe in Paprika from fields you fill in, for example from a recipe on a web page you have read; "
        "the server fetches nothing. Only a name is required. Categories must already exist (create_category adds one). "
        "A name that a live recipe already has is refused, giving that recipe's id, unless keep_both is true. "
        "No photo is added; the app does that."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The recipe's title"},
            **{f: {"type": "string", "description": d} for f, d in _TEXT.items()},
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Existing categories, by name or id",
            },
            "rating": {"type": "integer", "minimum": 0, "maximum": 5, "description": "0 to 5"},
            "keep_both": {
                "type": "boolean",
                "description": "Create it even though a live recipe has the same title (default: false)",
                "default": False,
            },
        },
        "required": ["name"],
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}
