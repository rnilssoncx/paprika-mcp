"""Category tools: create, rename, move and delete (moneypenny plans/051).

Four tools, one per thing a member asks for, so each has one shape of answer.
Spiked against the real account on 2026-11-14: POST /api/v2/sync/categories/
with a gzip-compressed JSON array as the multipart field `data` **upserts by
uid** — a new uid creates, an existing one is replaced whole, and an object
with `deleted: true` is gone from the next GET. Paprika does not take a deleted
category out of the recipes that were in it (they keep the dangling uid, shown
as `Unknown-xxxxxxxx`), which is why delete refuses while a live recipe is in
the category.

Split the way meal_plan and restore_recipe are: the `decide_*` functions are
pure — they take the category list (and, for delete, a way to read the recipes)
and return the one object to POST or raise CategoryRefused with the answer — so
every guard is tested by handing one adversarial input, not through a scripted
upload. The handlers only fetch, call them, POST and notify.

Every write POSTs the category's whole object as the GET returned it with only
the one field changed, never one built from the keys this module knows: a field
it does not know (`cookbook_uid` today, anything Paprika adds later) must
survive a rename. A new category is the five keys the GET returns.
"""

import uuid
from collections.abc import Callable
from typing import Any

from mcp.types import TextContent

from ..utils import (
    CategoriesUnavailable,
    CategoryRefused,
    all_recipes,
    check_category_name,
    get_categories,
    get_remote,
    normalize_string,
    notify_apps,
    post_categories,
    resolve_category,
)

# The words — contract, tests assert them (plans/051 § The words). A count reads
# "1 recipe … was" for one and "{n} recipes … were" otherwise, throughout; the
# same rule is applied to categories, so a parent with one child does not read
# "1 categories".
CREATED_TOP = 'Created category "{name}" at the top level.'
CREATED_UNDER = 'Created category "{name}" under "{parent}".'
RENAMED = 'Renamed category "{old}" to "{new}".'
MOVED_UNDER = 'Moved category "{name}" under "{parent}".'
MOVED_TOP = 'Moved category "{name}" to the top level.'
DELETED = 'Deleted category "{name}".'
TRASHED_NOTE = (
    " {n} {recipes} in the trash {were} in it; if one is restored it will show as in an unknown category."
)
NAME_TAKEN = 'Error: A category named "{name}" already exists ({where}). Nothing was changed.'
PARENT_NESTED = 'Error: "{parent}" is itself under "{grandparent}"; categories nest one level deep. Nothing was changed.'
HAS_CHILDREN_MOVE = (
    'Error: "{name}" has {n} {categories} under it, so it cannot go under another; '
    "categories nest one level deep. Nothing was changed."
)
UNDER_ITSELF = "Error: A category cannot go under itself. Nothing was changed."
IN_USE = (
    'Error: {n} {recipes} {are} in category "{name}", including {titles}. Nothing was deleted. '
    "Take them out of it with update_recipe first, or rename the category instead."
)
HAS_CHILDREN_DELETE = (
    'Error: "{name}" has {n} {categories} under it: {names}. Nothing was deleted. Move or delete them first.'
)
RECIPES_UNREADABLE = (
    'Error: Could not read the recipes to check whether any is in "{name}" ({error}). Nothing was deleted.'
)
CATEGORIES_UNREADABLE = "Error: {reason}. Nothing was changed."
POST_UNSURE = (
    'Error writing category "{name}": {error}. '
    "The change may or may not have reached Paprika; check list_categories before trying again."
)

# How many titles an in-use refusal names. Enough to recognise the recipes,
# few enough to read; the count is taken from the recipes, never the titles.
TITLES_SHOWN = 5


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _where(cat: dict[str, Any], categories: list[dict[str, Any]]) -> str:
    if not cat.get("parent_uid"):
        return "top level"
    return f'under "{_name_of(cat["parent_uid"], categories)}"'


def _name_of(uid: str, categories: list[dict[str, Any]]) -> str:
    for c in categories:
        if c.get("uid") == uid:
            return str(c.get("name") or "")
    return f"Unknown-{str(uid)[:8]}"


def children_of(cat: dict[str, Any], categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every category directly under `cat`, in list order, empty names included."""
    return [c for c in categories if c.get("parent_uid") == cat["uid"]]


# ---- pure: the checks ----------------------------------------------------------------


def check_name_free(name: str, categories: list[dict[str, Any]], own_uid: str | None = None) -> None:
    """Refuse a name any other category has, at any level.

    Compared with normalize_string (case and accents ignored), so *soup* cannot
    sit beside *Soups*-with-an-accent, and across levels, so the account's three
    shared names do not grow: each is an ambiguity at every later lookup. The
    category being renamed is excluded **by uid**, not by its old name — a
    rename of *Ethnic › Thai* to "THAI" must still find the top-level *Thai*
    as the taker, and *Soups* → *soups* must not find itself.

    A name equal to another category's id is not refused (051 code review: no
    one in the threat model types a uuid as a name, and the lookup tries ids
    first, so that category is still reached by its id).
    """
    wanted = normalize_string(name)
    takers = [
        c for c in categories if c.get("uid") != own_uid and normalize_string(c.get("name") or "") == wanted
    ]
    if takers:
        where = "; ".join(_where(c, categories) for c in takers)
        raise CategoryRefused(NAME_TAKEN.format(name=name, where=where))


def check_parent(parent: dict[str, Any], categories: list[dict[str, Any]]) -> None:
    """A parent must be a top-level category: nesting is one level deep.

    Every nested category in the account is one level deep, the spike did not
    try two, and what the apps show for two is unknown. A refusal here costs a
    member one more step; a second level the apps mishandle could hide recipes.
    """
    if parent.get("parent_uid"):
        raise CategoryRefused(
            PARENT_NESTED.format(parent=parent.get("name"), grandparent=_name_of(parent["parent_uid"], categories))
        )


# ---- pure: deciding -------------------------------------------------------------------


def decide_create(categories: list[dict[str, Any]], name: Any, parent_given: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The new category's object and its parent (None for the top level)."""
    name = check_category_name(name)
    parent = None
    if parent_given and parent_given.strip():
        parent = resolve_category(categories, parent_given, create_hint=False)
        check_parent(parent, categories)
    check_name_free(name, categories)
    # One past the highest, not len(): the account's flags have gaps (0 to 57
    # over 51 categories), and a new one lands last.
    flags = [c["order_flag"] for c in categories if isinstance(c.get("order_flag"), int)]
    return (
        {
            "uid": str(uuid.uuid4()).upper(),
            "name": name,
            "order_flag": max(flags, default=-1) + 1,
            "parent_uid": parent["uid"] if parent else None,
            "cookbook_uid": None,
        },
        parent,
    )


def decide_rename(categories: list[dict[str, Any]], given: str, new_name: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the category as it is, the object to POST). No no-op shortcut: the same
    name POSTs the same object, an idempotent upsert, and answers as usual."""
    cat = resolve_category(categories, given, create_hint=False)
    name = check_category_name(new_name)
    check_name_free(name, categories, own_uid=cat["uid"])
    return cat, {**cat, "name": name}


def decide_move(categories: list[dict[str, Any]], given: str, parent_given: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """(the category, the object to POST, the new parent or None for the top level)."""
    cat = resolve_category(categories, given, create_hint=False)
    if not parent_given or not parent_given.strip():
        return cat, {**cat, "parent_uid": None}, None
    parent = resolve_category(categories, parent_given, create_hint=False)
    # Checked first: a childless top-level category moved under itself passes
    # both rules below and would drop out of the tree.
    if parent["uid"] == cat["uid"]:
        raise CategoryRefused(UNDER_ITSELF)
    check_parent(parent, categories)
    kids = children_of(cat, categories)
    if kids:
        raise CategoryRefused(
            HAS_CHILDREN_MOVE.format(name=cat.get("name"), n=len(kids), categories=_plural(len(kids), "category", "categories"))
        )
    return cat, {**cat, "parent_uid": parent["uid"]}, parent


def decide_delete(
    categories: list[dict[str, Any]], given: str, load_recipes: Callable[[], list[Any]]
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """(the category, the object to POST, how many trashed recipes were in it).

    Refused while any live recipe is in it, by uid: Paprika leaves the uid in
    every recipe that had it, so those recipes would point at nothing. Refused
    while categories sit under it, which would leave each pointing at a parent
    that no longer exists. Recipes in the trash do not block — that would make
    a member empty the trash, in the app, to delete a category — and are counted
    in the answer instead.

    `load_recipes` reads every recipe; a failure anywhere in that walk refuses
    the delete. It is never carried out on a count taken from what was read
    before the failure: *can't tell* is not *none*.
    """
    cat = resolve_category(categories, given, create_hint=False)
    kids = children_of(cat, categories)
    if kids:
        raise CategoryRefused(
            HAS_CHILDREN_DELETE.format(
                name=cat.get("name"),
                n=len(kids),
                categories=_plural(len(kids), "category", "categories"),
                names=", ".join(f'"{k.get("name")}"' for k in kids),
            )
        )
    try:
        recipes = load_recipes()
    except Exception as e:
        raise CategoryRefused(RECIPES_UNREADABLE.format(name=cat.get("name"), error=f"{type(e).__name__}: {e}")) from e
    # `categories` may be None on a recipe Paprika sent that way; it holds none.
    holders = [r for r in recipes if cat["uid"] in (r.categories or [])]
    live = [r for r in holders if not r.in_trash]
    trashed = len(holders) - len(live)
    if live:
        first = sorted((r.name or "" for r in live), key=str.casefold)[:TITLES_SHOWN]
        raise CategoryRefused(
            IN_USE.format(
                n=len(live),
                recipes=_plural(len(live), "recipe", "recipes"),
                are=_plural(len(live), "is", "are"),
                name=cat.get("name"),
                titles=", ".join(f'"{t}"' for t in first),
            )
        )
    return cat, {**cat, "deleted": True}, trashed


# ---- I/O ------------------------------------------------------------------------------


def _text(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=message)]


def _categories(remote: Any) -> list[dict[str, Any]]:
    # Fresh for every call, so a category made in the app a minute ago is known.
    # The `all` list, not `by_uid`, which skips a category with an empty name.
    return list(get_categories(remote)["all"])


def _write(remote: Any, obj: dict[str, Any], success: str) -> list[TextContent]:
    """POST one object, then tell the apps. A failed POST is *can't tell*.

    The POST either answered something other than `result: true` or raised; in
    both the change may or may not have reached Paprika, so the answer sends the
    member to list_categories rather than claiming it failed. A failed notify
    after a good POST is reported after the success, never as a failed write.
    """
    try:
        post_categories(remote, [obj])
    except Exception as e:
        return _text(POST_UNSURE.format(name=obj.get("name"), error=e))
    return _text(success + notify_apps(remote))


def _run(decide: Callable[[Any, list[dict[str, Any]]], tuple[dict[str, Any], str]]) -> list[TextContent]:
    """Shared shape of every handler: fetch, decide, write.

    Names are checked once, inside `decide` — the pure function a test hands
    adversarial input — before any write. A second check before the fetch was
    removed at code review: it saved one categories GET on a bad name.
    """
    remote = get_remote()
    try:
        categories = _categories(remote)
    except CategoriesUnavailable as e:
        return _text(CATEGORIES_UNREADABLE.format(reason=e))
    try:
        obj, success = decide(remote, categories)
    except CategoryRefused as r:
        return _text(str(r))
    return _write(remote, obj, success)


def create_category_tool(args: dict[str, Any]) -> list[TextContent]:
    def decide(remote: Any, categories: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        obj, parent = decide_create(categories, args.get("name"), args.get("parent"))
        if parent is None:
            return obj, CREATED_TOP.format(name=obj["name"])
        return obj, CREATED_UNDER.format(name=obj["name"], parent=parent["name"])

    return _run(decide)


def rename_category_tool(args: dict[str, Any]) -> list[TextContent]:
    def decide(remote: Any, categories: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        cat, obj = decide_rename(categories, args.get("category") or "", args.get("new_name"))
        return obj, RENAMED.format(old=cat["name"], new=obj["name"])

    return _run(decide)


def move_category_tool(args: dict[str, Any]) -> list[TextContent]:
    def decide(remote: Any, categories: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        cat, obj, parent = decide_move(categories, args.get("category") or "", args.get("parent") or "")
        if parent is None:
            return obj, MOVED_TOP.format(name=cat["name"])
        return obj, MOVED_UNDER.format(name=cat["name"], parent=parent["name"])

    return _run(decide)


def delete_category_tool(args: dict[str, Any]) -> list[TextContent]:
    def decide(remote: Any, categories: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        # Every recipe, through the cached walk update_recipe makes to find one.
        cat, obj, trashed = decide_delete(categories, args.get("category") or "", lambda: all_recipes(remote))
        note = ""
        if trashed:
            note = TRASHED_NOTE.format(
                n=trashed, recipes=_plural(trashed, "recipe", "recipes"), were=_plural(trashed, "was", "were")
            )
        return obj, DELETED.format(name=cat["name"]) + note

    return _run(decide)


# ---- definitions -----------------------------------------------------------------------

_DANGER = "This is a DANGEROUS operation that requires user confirmation. "

CREATE_CATEGORY_DEF = {
    "name": "create_category",
    "description": (
        "Create a recipe category, at the top level or under a parent category (categories nest one level deep). "
        "A new category goes last. The name may not be one any other category already has."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the new category, on one line"},
            "parent": {
                "type": "string",
                "description": "Optional: a top-level category, by name or id, to put it under",
            },
        },
        "required": ["name"],
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}

RENAME_CATEGORY_DEF = {
    "name": "rename_category",
    "description": (
        "Rename a recipe category. " + _DANGER + "Recipes keep the category. "
        "Give the category by name, or by id when its name is shared."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "The category, by name or id"},
            "new_name": {"type": "string", "description": "Its new name, on one line"},
        },
        "required": ["category", "new_name"],
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True},
}

MOVE_CATEGORY_DEF = {
    "name": "move_category",
    "description": (
        "Move a recipe category under a top-level parent category, or back to the top level with an empty parent. "
        + _DANGER
        + "Categories nest one level deep; a category with categories under it cannot go under another."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "The category, by name or id"},
            "parent": {
                "type": "string",
                "description": 'The top-level category to put it under, by name or id; "" for the top level',
            },
        },
        "required": ["category", "parent"],
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True},
}

DELETE_CATEGORY_DEF = {
    "name": "delete_category",
    "description": (
        "Delete a recipe category. " + _DANGER + "Refused while a recipe is in it or categories are under it: "
        "take recipes out with update_recipe first, or rename the category instead."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"category": {"type": "string", "description": "The category, by name or id"}},
        "required": ["category"],
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True},
}
