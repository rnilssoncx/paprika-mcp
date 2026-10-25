"""Meal planner and menus: read them, add and remove planned meals, and put a
menu on the planner.

Paprika's sync API keeps meals, menus, menu items and meal types as separate
collections, each a plain GET. Writes are documented only for meals, by a
community reference verified against a live account on 2026-06-29: POST a
gzip-compressed JSON array to /api/v2/sync/meals/ as the multipart field
`data`. There is no per-meal endpoint, and removal is a soft delete
(`deleted: true`). Menus' own write format is not known, so menus are read-only
here, and planning a menu writes meals, never the menu.

Everything that decides what to write is a pure function below, so the checks
are tested without the API; the tool handlers only fetch, call them and post.
"""

import gzip
import json
import uuid
from datetime import date, timedelta
from typing import Any

from mcp.types import TextContent

from ..utils import get_remote

MAX_DAYS = 62


class Refused(Exception):
    """A request the tools will not carry out; the message is the answer."""


# ---- pure: parsing and lookup ------------------------------------------------


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise Refused(f"Date must be YYYY-MM-DD, got {value!r}") from None


def meal_date(meal: dict) -> date:
    return date.fromisoformat(meal["date"][:10])


def resolve_meal_type(types: list[dict], name: str) -> dict:
    for t in types:
        if t["name"].lower() == name.strip().lower():
            return t
    known = ", ".join(t["name"] for t in sorted(types, key=lambda t: t["order_flag"]))
    raise Refused(f'Unknown meal type "{name}". Known: {known}')


def find_menu(menus: list[dict], name: str) -> dict:
    for m in menus:
        if m["name"].lower() == name.strip().lower():
            return m
    known = ", ".join(m["name"] for m in menus) or "none"
    raise Refused(f'No menu named "{name}". Menus: {known}')


def find_meal(meals: list[dict], meal_id: str) -> dict:
    for m in meals:
        if m["uid"] == meal_id:
            return m
    raise Refused(f'No planned meal with id "{meal_id}". Use list_meals to find it.')


def same_meal(a: dict, b: dict) -> bool:
    """The same dish in the same slot: date, meal type, and recipe or name."""
    return (
        a["date"][:10] == b["date"][:10]
        and a.get("type_uid") == b.get("type_uid")
        and (a.get("recipe_uid") or a.get("name")) == (b.get("recipe_uid") or b.get("name"))
    )


# ---- pure: building what gets written ------------------------------------------


def new_meal(day: date, meal_type: dict, name: str, recipe_uid: str | None, scale: str | None = None) -> dict:
    # Paprika expects the client to mint ids, as uppercase UUID4s.
    return {
        "uid": str(uuid.uuid4()).upper(),
        "recipe_uid": recipe_uid,
        "date": f"{day.isoformat()} 00:00:00",
        "type": meal_type["original_type"],
        "type_uid": meal_type["uid"],
        "name": name,
        "order_flag": 0,
        "scale": scale,
        "is_ingredient": False,
        "deleted": False,
    }


def decide_add(existing: list[dict], day: date, meal_type: dict, name: str, recipe_uid: str | None) -> dict:
    meal = new_meal(day, meal_type, name, recipe_uid)
    if any(same_meal(meal, m) for m in existing):
        raise Refused(f"{name} is already planned for {meal_type['name']} on {day.isoformat()}; nothing added.")
    return meal


def decide_remove(meals: list[dict], meal_id: str) -> dict:
    return {**find_meal(meals, meal_id), "deleted": True}


def decide_plan_menu(
    menu: dict, items: list[dict], start: date, types: list[dict], existing: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Meals for every item of the menu, day N landing on start + N - 1.

    Returns (to_add, already_planned): an item already in its slot is skipped,
    so planning the same menu twice does not double it.
    """
    by_uid = {t["uid"]: t for t in types}
    to_add, skipped = [], []
    for item in sorted((i for i in items if i["menu_uid"] == menu["uid"]), key=lambda i: (i["day"], i["order_flag"])):
        meal_type = by_uid.get(item["type_uid"])
        if meal_type is None:
            raise Refused(f'Menu "{menu["name"]}" has an item with a meal type this account no longer has: {item["name"]}')
        meal = new_meal(start + timedelta(days=item["day"] - 1), meal_type, item["name"], item.get("recipe_uid"), item.get("scale"))
        (skipped if any(same_meal(meal, m) for m in existing + to_add) else to_add).append(meal)
    if not to_add and not skipped:
        raise Refused(f'Menu "{menu["name"]}" has no items to plan.')
    return to_add, skipped


# ---- pure: rendering -------------------------------------------------------------


def _type_name(meal: dict, types: list[dict]) -> str:
    return next((t["name"] for t in types if t["uid"] == meal.get("type_uid")), "Meal")


def format_meals(meals: list[dict], types: list[dict], start: date, end: date) -> str:
    order = {t["uid"]: t["order_flag"] for t in types}
    chosen = sorted(
        (m for m in meals if start <= meal_date(m) <= end),
        key=lambda m: (m["date"], order.get(m.get("type_uid"), 99), m.get("order_flag", 0)),
    )
    span = f"{start.isoformat()} to {end.isoformat()}"
    if not chosen:
        return f"No meals planned from {span}."
    lines, current = [f"Meals planned from {span}:"], None
    for m in chosen:
        d = meal_date(m)
        if d != current:
            lines.append(f"\n{d.strftime('%A')} {d.isoformat()}")
            current = d
        recipe = f" [recipe {m['recipe_uid']}]" if m.get("recipe_uid") else ""
        lines.append(f"- {_type_name(m, types)}: {m['name']}{recipe} (meal id {m['uid']})")
    return "\n".join(lines)


def format_menus(menus: list[dict], items: list[dict], types: list[dict]) -> str:
    if not menus:
        return "No menus."
    lines = [f"{len(menus)} menu(s):"]
    for menu in sorted(menus, key=lambda m: m.get("order_flag", 0)):
        days = menu.get("days") or 1
        lines.append(f"\n## {menu['name']} ({days} day{'s' if days != 1 else ''})")
        mine = sorted((i for i in items if i["menu_uid"] == menu["uid"]), key=lambda i: (i["day"], i["order_flag"]))
        for i in mine:
            recipe = f" [recipe {i['recipe_uid']}]" if i.get("recipe_uid") else ""
            lines.append(f"- Day {i['day']}, {_type_name(i, types)}: {i['name']}{recipe}")
        if not mine:
            lines.append("- (no items)")
        if menu.get("notes"):
            lines.append("Notes:\n" + menu["notes"].strip())
    return "\n".join(lines)


# ---- I/O ---------------------------------------------------------------------------


def _get(remote, collection: str) -> list[dict]:
    return remote._request("get", f"/api/v2/sync/{collection}/").json().get("result", [])


def _post_meals(remote, meals: list[dict]) -> None:
    body = gzip.compress(json.dumps(meals).encode())
    remote._request("post", "/api/v2/sync/meals/", files={"data": ("data", body)})
    remote.notify()


def _recipe_name(remote, recipe_id: str) -> str:
    try:
        recipe = remote._request("get", f"/api/v2/sync/recipe/{recipe_id}/").json().get("result") or {}
    except Exception:
        recipe = {}
    if not recipe.get("name") or recipe.get("in_trash"):
        raise Refused(f'No recipe with id "{recipe_id}". Use search_recipes to find it.')
    return recipe["name"]


def _text(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=message)]


def _refusable(handler):
    async def run(args: dict[str, Any]) -> list[TextContent]:
        try:
            return await handler(args)
        except Refused as r:
            return _text(str(r))

    return run


@_refusable
async def list_meals_tool(args: dict[str, Any]) -> list[TextContent]:
    start = parse_date(args["start_date"]) if args.get("start_date") else date.today()
    days = int(args.get("days", 7))
    if not 1 <= days <= MAX_DAYS:
        raise Refused(f"days must be between 1 and {MAX_DAYS}")
    remote = get_remote()
    return _text(format_meals(_get(remote, "meals"), _get(remote, "mealtypes"), start, start + timedelta(days=days - 1)))


@_refusable
async def add_meal_tool(args: dict[str, Any]) -> list[TextContent]:
    day = parse_date(args.get("date"))
    recipe_id, name = args.get("recipe_id"), (args.get("name") or "").strip()
    if not recipe_id and not name:
        raise Refused("Give a recipe_id, or a name for a meal without a recipe.")
    remote = get_remote()
    meal_type = resolve_meal_type(_get(remote, "mealtypes"), args.get("meal_type") or "Dinner")
    if recipe_id:
        name = _recipe_name(remote, recipe_id)
    meal = decide_add(_get(remote, "meals"), day, meal_type, name, recipe_id)
    _post_meals(remote, [meal])
    return _text(f"Planned {name} for {meal_type['name']} on {day.strftime('%A')} {day.isoformat()} (meal id {meal['uid']}).")


@_refusable
async def remove_meal_tool(args: dict[str, Any]) -> list[TextContent]:
    remote = get_remote()
    meal = decide_remove(_get(remote, "meals"), args.get("meal_id", ""))
    _post_meals(remote, [meal])
    return _text(f"Removed {meal['name']} from {meal['date'][:10]}.")


@_refusable
async def list_menus_tool(args: dict[str, Any]) -> list[TextContent]:
    remote = get_remote()
    return _text(format_menus(_get(remote, "menus"), _get(remote, "menuitems"), _get(remote, "mealtypes")))


@_refusable
async def plan_menu_tool(args: dict[str, Any]) -> list[TextContent]:
    start = parse_date(args.get("start_date"))
    remote = get_remote()
    menu = find_menu(_get(remote, "menus"), args.get("menu", ""))
    to_add, skipped = decide_plan_menu(menu, _get(remote, "menuitems"), start, _get(remote, "mealtypes"), _get(remote, "meals"))
    if to_add:
        _post_meals(remote, to_add)
    lines = [f'Planned {len(to_add)} meal(s) from "{menu["name"]}" starting {start.isoformat()}:']
    lines += [f"- {m['date'][:10]}: {m['name']} (meal id {m['uid']})" for m in to_add]
    if skipped:
        lines.append(f"Already planned, skipped: {', '.join(m['name'] for m in skipped)}")
    return _text("\n".join(lines))


# ---- definitions ---------------------------------------------------------------------

LIST_MEALS_DEF = {
    "name": "list_meals",
    "description": (
        "List meals planned in Paprika's meal planner over a range of days, grouped by day, "
        "with each meal's type (Breakfast, Lunch, Dinner, Snacks), recipe id and meal id."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "First day, YYYY-MM-DD. Defaults to today."},
            "days": {"type": "integer", "description": f"How many days, 1 to {MAX_DAYS}. Defaults to 7."},
        },
        "required": [],
    },
}

ADD_MEAL_DEF = {
    "name": "add_meal",
    "description": (
        "Plan a meal on a date in Paprika's meal planner. Give recipe_id (from search_recipes) to link a "
        "recipe, or a name for a meal without one. Refuses a dish already planned in that slot."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "meal_type": {"type": "string", "description": "Breakfast, Lunch, Dinner or Snacks. Defaults to Dinner."},
            "recipe_id": {"type": "string", "description": "The recipe's id."},
            "name": {"type": "string", "description": "A meal without a recipe, e.g. 'Leftovers'."},
        },
        "required": ["date"],
    },
}

REMOVE_MEAL_DEF = {
    "name": "remove_meal",
    "description": "Remove a planned meal from Paprika's meal planner, by the meal id list_meals shows.",
    "inputSchema": {
        "type": "object",
        "properties": {"meal_id": {"type": "string"}},
        "required": ["meal_id"],
    },
}

LIST_MENUS_DEF = {
    "name": "list_menus",
    "description": "List Paprika's menus: reusable plans of recipes laid out by day, with their notes.",
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}

PLAN_MENU_DEF = {
    "name": "plan_menu",
    "description": (
        "Put a menu on the meal planner: its day 1 lands on start_date, day 2 the day after, and so on. "
        "Items already planned in their slot are skipped. Menus themselves are not edited."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "menu": {"type": "string", "description": "The menu's name, as list_menus shows it."},
            "start_date": {"type": "string", "description": "YYYY-MM-DD for the menu's day 1."},
        },
        "required": ["menu", "start_date"],
    },
}
