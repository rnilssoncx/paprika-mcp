"""Meal planner and menus: the decisions are tested by hand, the handlers against
a fake remote that records what would have been sent to Paprika."""

import asyncio
import gzip
import json
import re
from datetime import date

import pytest
from paprika_recipes.exceptions import RequestError

from paprika_mcp.tools import TOOLS
from paprika_mcp.tools import meal_plan as mp

DINNER = {"uid": "T-DIN", "name": "Dinner", "order_flag": 2, "original_type": 2}
LUNCH = {"uid": "T-LUN", "name": "Lunch", "order_flag": 1, "original_type": 1}
BREAKFAST = {"uid": "T-BRK", "name": "Breakfast", "order_flag": 0, "original_type": 0}
TYPES = [DINNER, LUNCH, BREAKFAST]

FEAST = {"uid": "M-FEAST", "name": "Feast", "notes": "Bring napkins", "order_flag": 0, "days": 2}
OTHER = {"uid": "M-OTHER", "name": "Other", "notes": "", "order_flag": 1, "days": 1}
ITEMS = [
    {"uid": "I1", "menu_uid": "M-FEAST", "name": "Soup", "recipe_uid": "B2-0B", "type_uid": "T-LUN", "day": 1, "order_flag": 0, "scale": None},
    {"uid": "I2", "menu_uid": "M-FEAST", "name": "Roast", "recipe_uid": "A1-0A", "type_uid": "T-DIN", "day": 2, "order_flag": 0, "scale": "2"},
    {"uid": "I3", "menu_uid": "M-OTHER", "name": "Toast", "recipe_uid": "C3-0C", "type_uid": "T-BRK", "day": 1, "order_flag": 0, "scale": None},
]


def meal(uid, day, type_, name, recipe=None):
    return {"uid": uid, "date": f"{day} 00:00:00", "type": type_["original_type"], "type_uid": type_["uid"],
            "name": name, "recipe_uid": recipe, "order_flag": 0, "scale": None, "is_ingredient": False}


# ---- parsing and lookup ----


def test_a_bad_date_is_refused_with_the_format():
    with pytest.raises(mp.Refused, match=r"^Date must be YYYY-MM-DD, got '10/26/2026'$"):
        mp.parse_date("10/26/2026")


def test_meal_type_matches_ignoring_case():
    assert mp.resolve_meal_type(TYPES, " lunch ") is LUNCH


def test_an_unknown_meal_type_names_the_known_ones_in_order():
    with pytest.raises(mp.Refused, match=r'^Unknown meal type "Brunch"\. Known: Breakfast, Lunch, Dinner$'):
        mp.resolve_meal_type(TYPES, "Brunch")


def test_an_unknown_menu_names_the_menus():
    with pytest.raises(mp.Refused, match=r'^No menu named "Picnic"\. Menus: Feast, Other$'):
        mp.find_menu([FEAST, OTHER], "Picnic")


# ---- building meals ----


def test_a_new_meal_carries_the_slot_and_a_fresh_uppercase_uuid():
    m = mp.new_meal(date(2026, 10, 26), LUNCH, "Soup", "B2-0B")
    assert re.fullmatch(r"[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}", m["uid"])
    assert (m["date"], m["type"], m["type_uid"], m["recipe_uid"], m["deleted"]) == ("2026-10-26 00:00:00", 1, "T-LUN", "B2-0B", False)
    assert mp.new_meal(date(2026, 10, 26), LUNCH, "Soup", "B2-0B")["uid"] != m["uid"]


def test_adding_a_dish_already_in_its_slot_is_refused():
    existing = [meal("A", "2026-10-26", DINNER, "Roast", "A1-0A")]
    with pytest.raises(mp.Refused, match=r"^Roast is already planned for Dinner on 2026-10-26; nothing added\.$"):
        mp.decide_add(existing, date(2026, 10, 26), DINNER, "Roast", "A1-0A")


def test_the_same_dish_in_another_slot_or_day_is_allowed():
    existing = [meal("A", "2026-10-26", DINNER, "Roast", "A1-0A")]
    assert mp.decide_add(existing, date(2026, 10, 26), LUNCH, "Roast", "A1-0A")["type_uid"] == "T-LUN"
    assert mp.decide_add(existing, date(2026, 10, 27), DINNER, "Roast", "A1-0A")["date"].startswith("2026-10-27")


def test_a_text_only_meal_is_a_duplicate_by_name():
    existing = [meal("A", "2026-10-26", DINNER, "Leftovers")]
    with pytest.raises(mp.Refused, match="already planned"):
        mp.decide_add(existing, date(2026, 10, 26), DINNER, "Leftovers", None)


def test_removal_is_a_soft_delete_of_the_whole_entry():
    target = meal("B", "2026-10-26", DINNER, "Roast", "A1-0A")
    removed = mp.decide_remove([meal("A", "2026-10-25", LUNCH, "Soup"), target], "B")
    assert removed == {**target, "deleted": True}


def test_removing_an_unknown_meal_is_refused():
    with pytest.raises(mp.Refused, match=r'^No planned meal with id "Z"\. Use list_meals to find it\.$'):
        mp.decide_remove([meal("A", "2026-10-25", LUNCH, "Soup")], "Z")


# ---- planning a menu ----


def test_a_menu_lands_day_by_day_from_the_start_date_with_only_its_own_items():
    to_add, skipped = mp.decide_plan_menu(FEAST, ITEMS, date(2026, 12, 24), TYPES, [])
    assert [(m["date"][:10], m["type_uid"], m["name"], m["recipe_uid"], m["scale"]) for m in to_add] == [
        ("2026-12-24", "T-LUN", "Soup", "B2-0B", None),
        ("2026-12-25", "T-DIN", "Roast", "A1-0A", "2"),
    ]
    assert skipped == []


def test_planning_a_menu_twice_adds_nothing_the_second_time():
    first, _ = mp.decide_plan_menu(FEAST, ITEMS, date(2026, 12, 24), TYPES, [])
    again, skipped = mp.decide_plan_menu(FEAST, ITEMS, date(2026, 12, 24), TYPES, first)
    assert again == []
    assert [m["name"] for m in skipped] == ["Soup", "Roast"]


def test_a_menu_item_with_a_vanished_meal_type_is_refused():
    with pytest.raises(mp.Refused, match=r'^Menu "Feast" has an item with a meal type this account no longer has: Soup$'):
        mp.decide_plan_menu(FEAST, ITEMS, date(2026, 12, 24), [DINNER], [])


def test_an_empty_menu_is_refused():
    with pytest.raises(mp.Refused, match=r'^Menu "Feast" has no items to plan\.$'):
        mp.decide_plan_menu(FEAST, [ITEMS[2]], date(2026, 12, 24), TYPES, [])


# ---- rendering ----


def test_meals_are_listed_within_the_range_in_meal_type_order():
    meals = [
        meal("A", "2026-10-26", DINNER, "Roast", "A1-0A"),
        meal("B", "2026-10-26", BREAKFAST, "Toast"),
        meal("C", "2026-10-28", LUNCH, "Out of range"),
        meal("D", "2026-10-25", LUNCH, "Before range"),
    ]
    text = mp.format_meals(meals, TYPES, date(2026, 10, 26), date(2026, 10, 27))
    assert "Out of range" not in text and "Before range" not in text
    assert text.index("Breakfast: Toast") < text.index("Dinner: Roast [recipe A1-0A] (meal id A)")
    assert "Monday 2026-10-26" in text


def test_an_empty_range_says_so():
    assert mp.format_meals([], TYPES, date(2026, 10, 26), date(2026, 11, 1)) == "No meals planned from 2026-10-26 to 2026-11-01."


def test_menus_show_items_by_day_and_their_notes():
    text = mp.format_menus([FEAST, OTHER], ITEMS, TYPES)
    feast = text[text.index("## Feast"):text.index("## Other")]
    assert "Day 1, Lunch: Soup [recipe B2-0B]" in feast and "Day 2, Dinner: Roast" in feast
    assert "Toast" not in feast and "Bring napkins" in feast


# ---- handlers, against a fake remote ----


class FakeRemote:
    def __init__(self, meals=(), recipes=None):
        self.data = {"meals": list(meals), "mealtypes": TYPES, "menus": [FEAST, OTHER], "menuitems": ITEMS}
        self.recipes = recipes or {}
        self.posted, self.notified = [], 0
        self.recipe_gets, self.recipe_error = [], None

    def _request(self, method, path, **kwargs):
        if method == "post":
            assert path == "/api/v2/sync/meals/"
            field, body = kwargs["files"]["data"]
            assert field == "data"
            self.posted.append(json.loads(gzip.decompress(body)))
            return Response({"result": True})
        if path.startswith("/api/v2/sync/recipe/"):
            self.recipe_gets.append(path)
            if self.recipe_error:
                raise self.recipe_error
            recipe = self.recipes.get(path.split("/")[-2])
            if recipe is None:
                raise RequestError("Recipe not found.")  # what Paprika answers, checked live 2026-10-25
            return Response({"result": recipe})
        return Response({"result": self.data[path.split("/")[-2]]})

    def notify(self):
        self.notified += 1


class Response:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def run(tool, args, remote, monkeypatch):
    monkeypatch.setattr(mp, "get_remote", lambda: remote)
    return asyncio.run(TOOLS[tool]["handler"](args))[0].text


def test_the_five_tools_are_registered():
    assert {"list_meals", "add_meal", "remove_meal", "list_menus", "plan_menu"} <= set(TOOLS)


def test_add_meal_posts_one_gzipped_meal_named_after_the_recipe_and_notifies(monkeypatch):
    remote = FakeRemote(recipes={"A1-0A": {"name": "Sunday Roast"}})
    text = run("add_meal", {"date": "2026-10-26", "meal_type": "dinner", "recipe_id": "A1-0A"}, remote, monkeypatch)
    [[posted]] = remote.posted
    assert (posted["name"], posted["recipe_uid"], posted["type_uid"], posted["date"]) == ("Sunday Roast", "A1-0A", "T-DIN", "2026-10-26 00:00:00")
    assert remote.notified == 1
    assert text.startswith("Planned Sunday Roast for Dinner on Monday 2026-10-26")


def test_add_meal_with_an_unknown_recipe_posts_nothing(monkeypatch):
    remote = FakeRemote()
    text = run("add_meal", {"date": "2026-10-26", "recipe_id": "D4-0D"}, remote, monkeypatch)
    assert text == 'No recipe with id "D4-0D". Use search_recipes to find it.'
    assert remote.posted == []


def test_a_refused_duplicate_posts_nothing(monkeypatch):
    remote = FakeRemote(meals=[meal("A", "2026-10-26", DINNER, "Leftovers")])
    text = run("add_meal", {"date": "2026-10-26", "name": "Leftovers"}, remote, monkeypatch)
    assert text == "Leftovers is already planned for Dinner on 2026-10-26; nothing added."
    assert remote.posted == []


def test_add_meal_needs_a_recipe_or_a_name(monkeypatch):
    remote = FakeRemote()
    assert run("add_meal", {"date": "2026-10-26"}, remote, monkeypatch) == "Give a recipe_id, or a name for a meal without a recipe."
    assert remote.posted == []


def test_remove_meal_posts_the_entry_marked_deleted(monkeypatch):
    target = meal("B", "2026-10-26", DINNER, "Roast", "A1-0A")
    remote = FakeRemote(meals=[target])
    assert run("remove_meal", {"meal_id": "B"}, remote, monkeypatch) == "Removed Roast from 2026-10-26."
    assert remote.posted == [[{**target, "deleted": True}]]


def test_plan_menu_posts_only_what_is_not_already_planned(monkeypatch):
    remote = FakeRemote(meals=[meal("A", "2026-12-24", LUNCH, "Soup", "B2-0B")])
    text = run("plan_menu", {"menu": "feast", "start_date": "2026-12-24"}, remote, monkeypatch)
    assert [[m["name"] for m in batch] for batch in remote.posted] == [["Roast"]]
    assert "Already planned, skipped: Soup" in text


def test_plan_menu_with_everything_planned_posts_nothing(monkeypatch):
    remote = FakeRemote(meals=[meal("A", "2026-12-24", LUNCH, "Soup", "B2-0B"), meal("B", "2026-12-25", DINNER, "Roast", "A1-0A")])
    run("plan_menu", {"menu": "Feast", "start_date": "2026-12-24"}, remote, monkeypatch)
    assert remote.posted == [] and remote.notified == 0


def test_list_meals_refuses_an_overlong_range(monkeypatch):
    assert run("list_meals", {"days": 63}, FakeRemote(), monkeypatch) == "days must be between 1 and 62"


def test_a_recipe_in_the_trash_is_refused(monkeypatch):
    remote = FakeRemote(recipes={"E5-0E": {"name": "Old Roast", "in_trash": True}})
    text = run("add_meal", {"date": "2026-10-26", "recipe_id": "E5-0E"}, remote, monkeypatch)
    assert text == 'No recipe with id "E5-0E". Use search_recipes to find it.'
    assert remote.posted == []


def test_a_lookup_failure_is_not_reported_as_a_missing_recipe(monkeypatch):
    remote = FakeRemote(recipes={"A1-0A": {"name": "Sunday Roast"}})
    remote.recipe_error = ConnectionError("timed out")
    text = run("add_meal", {"date": "2026-10-26", "recipe_id": "A1-0A"}, remote, monkeypatch)
    assert text == 'Could not reach Paprika to look up recipe "A1-0A"; nothing added. (ConnectionError)'
    assert remote.posted == []


def test_a_paprika_error_other_than_not_found_is_not_reported_as_a_missing_recipe(monkeypatch):
    remote = FakeRemote()
    remote.recipe_error = RequestError("Unrecognized client.")
    text = run("add_meal", {"date": "2026-10-26", "recipe_id": "A1-0A"}, remote, monkeypatch)
    assert text == 'Could not look up recipe "A1-0A" in Paprika (Unrecognized client.); nothing added.'
    assert remote.posted == []


@pytest.mark.parametrize("bad", ["x/../../account/foo?x=1", "A1-0A/", "ZZ-0A", "A10A"])
def test_a_recipe_id_that_is_not_an_id_never_reaches_the_request_path(monkeypatch, bad):
    remote = FakeRemote()
    text = run("add_meal", {"date": "2026-10-26", "recipe_id": bad}, remote, monkeypatch)
    assert text == f'Not a recipe id: "{bad}". Use search_recipes to find it.'
    assert remote.recipe_gets == [] and remote.posted == []


def test_a_real_paprika_id_is_accepted():
    assert mp.RECIPE_ID.fullmatch("A6A2C004-8C9D-4AF0-8850-7C3FDFB73DA0-341-0000000747EA4CEF")


def test_the_same_recipe_renamed_is_still_the_same_dish():
    existing = [meal("A", "2026-10-26", DINNER, "Roast", "A1-0A")]
    with pytest.raises(mp.Refused, match="already planned"):
        mp.decide_add(existing, date(2026, 10, 26), DINNER, "Sunday Roast", "A1-0A")


def test_two_recipes_with_the_same_name_are_different_dishes():
    existing = [meal("A", "2026-10-26", DINNER, "Soup", "A1-0A")]
    assert mp.decide_add(existing, date(2026, 10, 26), DINNER, "Soup", "B2-0B")["recipe_uid"] == "B2-0B"


def test_list_meals_covers_exactly_start_through_start_plus_days_minus_one(monkeypatch):
    meals = [meal("A", "2026-10-26", DINNER, "First"), meal("B", "2026-10-27", DINNER, "Last"), meal("C", "2026-10-28", DINNER, "After")]
    text = run("list_meals", {"start_date": "2026-10-26", "days": 2}, FakeRemote(meals=meals), monkeypatch)
    assert "First" in text and "Last" in text and "After" not in text
    assert text.startswith("Meals planned from 2026-10-26 to 2026-10-27:")


@pytest.mark.parametrize("days", [0, 63])
def test_list_meals_refuses_a_range_out_of_bounds(monkeypatch, days):
    assert run("list_meals", {"days": days}, FakeRemote(), monkeypatch) == "days must be between 1 and 62"


def test_list_menus_shows_the_menus(monkeypatch):
    text = run("list_menus", {}, FakeRemote(), monkeypatch)
    assert text.startswith("2 menu(s):") and "## Feast (2 days)" in text and "## Other (1 day)" in text
