"""update_recipe against a fake remote: what it uploads, and what it refuses.

Every refusal is asserted by its exact text — the message is part of the
contract, and a test that only checks "something was refused" cannot tell the
guard under test from another one refusing the same input for its own reason.
"""

import pytest
from fakes import RecipeRemote, category
from paprika_recipes.remote import RemoteRecipe

from paprika_mcp.tools import TOOLS
from paprika_mcp.tools import update_recipe as ur

DINNER = category("C-DIN", "Dinner")
SOUPS = category("C-SOU", "Soups")


def roast(**fields):
    base = {"uid": "A1-0A", "name": "Roast", "notes": "Salt well", "ingredients": "1 chicken\n2 lemons", "directions": "", "rating": 3, "categories": ["C-DIN"]}
    return RemoteRecipe(**{**base, **fields})


def update(remote, monkeypatch, **args):
    monkeypatch.setattr(ur, "get_remote", lambda: remote)
    return TOOLS["update_recipe"]["handler"]({"id": "A1-0A", **args})[0].text


def empty_match(find, field="notes"):
    return ur.EMPTY_MATCH.format(find=find, field=field, name="Roast")


# ---- the empty match ----


def test_the_empty_match_refusal_says_how_to_append_or_set():
    assert empty_match("") == (
        "Error: find '' matches empty text in field 'notes' of recipe 'Roast', which would insert "
        "the replacement at every empty match (between every character, for an empty find). No changes were saved. "
        "To append, find the field's last words and replace them with those words plus the addition. "
        "To set the whole field, use regex=true with find '(?s).+'."
    )


@pytest.mark.parametrize("find, regex", [("", False), ("x*", True), ("", True), ("(well)?", True)])
def test_a_find_matching_empty_text_in_a_filled_field_is_refused(monkeypatch, find, regex):
    remote = RecipeRemote([roast()], [DINNER])
    assert update(remote, monkeypatch, field="notes", find=find, replace="!", regex=regex) == empty_match(find)
    assert remote.uploaded == []


def test_an_empty_find_fills_an_empty_field(monkeypatch):
    remote = RecipeRemote([roast(notes="")], [DINNER])
    text = update(remote, monkeypatch, field="notes", find="", replace="Contains nuts")
    assert [u["notes"] for u in remote.uploaded] == ["Contains nuts"]
    assert text.startswith("Successfully updated field 'notes' in recipe 'Roast' (ID: A1-0A)")


def test_an_empty_find_fills_a_field_paprika_sent_as_null(monkeypatch):
    remote = RecipeRemote([roast(notes=None)], [DINNER])
    update(remote, monkeypatch, field="notes", find="", replace="Contains nuts")
    assert [u["notes"] for u in remote.uploaded] == ["Contains nuts"]


def test_the_suggested_whole_field_pattern_sets_the_field_once(monkeypatch):
    remote = RecipeRemote([roast(notes="Salt well\nRest 10 min\n")], [DINNER])
    update(remote, monkeypatch, field="notes", find="(?s).+", replace="Brine overnight", regex=True)
    assert [u["notes"] for u in remote.uploaded] == ["Brine overnight"]


def test_an_empty_find_on_categories_that_has_some_is_refused(monkeypatch):
    remote = RecipeRemote([roast()], [DINNER, SOUPS])
    assert update(remote, monkeypatch, field="categories", find="", replace="Soups") == empty_match("", "categories")
    assert remote.uploaded == []


def test_an_empty_find_adds_a_category_to_a_recipe_with_none(monkeypatch):
    remote = RecipeRemote([roast(categories=[])], [DINNER, SOUPS])
    update(remote, monkeypatch, field="categories", find="", replace="Soups")
    assert [u["categories"] for u in remote.uploaded] == [["C-SOU"]]


# ---- ordinary replaces ----


def test_a_replace_uploads_the_field_with_only_the_match_changed_and_notifies(monkeypatch):
    remote = RecipeRemote([roast()], [DINNER])
    text = update(remote, monkeypatch, field="ingredients", find="2 lemons", replace="3 lemons")
    [uploaded] = remote.uploaded
    assert uploaded["ingredients"] == "1 chicken\n3 lemons"
    assert (uploaded["uid"], uploaded["notes"]) == ("A1-0A", "Salt well")
    assert remote.notified == 1
    assert text == (
        "Successfully updated field 'ingredients' in recipe 'Roast' (ID: A1-0A)\n\n"
        "Old value:\n1 chicken\n2 lemons\n\nNew value:\n1 chicken\n3 lemons"
    )


def test_a_regex_replace_can_use_groups(monkeypatch):
    remote = RecipeRemote([roast()], [DINNER])
    update(remote, monkeypatch, field="ingredients", find=r"(\d) lemons", replace=r"\1 limes", regex=True)
    assert [u["ingredients"] for u in remote.uploaded] == ["1 chicken\n2 limes"]


def test_a_find_that_is_absent_uploads_nothing(monkeypatch):
    remote = RecipeRemote([roast()], [DINNER])
    text = update(remote, monkeypatch, field="notes", find="pepper", replace="salt")
    assert text == "No changes made - pattern 'pepper' not found in field 'notes' of recipe 'Roast'"
    assert remote.uploaded == [] and remote.notified == 0


def test_a_failed_notify_after_an_upload_is_not_a_failed_edit(monkeypatch):
    remote = RecipeRemote([roast()], [DINNER])
    remote.notify_error = ConnectionError("reset")
    text = update(remote, monkeypatch, field="notes", find="Salt", replace="Brine")
    assert text == (
        "Successfully updated field 'notes' in recipe 'Roast' (ID: A1-0A)\n\nOld value:\nSalt well\n\nNew value:\nBrine well"
        "\n\nThe change is saved in Paprika, but the apps could not be told to refresh "
        "(ConnectionError: reset); they will show it at their next sync."
    )
    assert len(remote.uploaded) == 1


# ---- the field allowlist ----


@pytest.mark.parametrize("field", ["uid", "in_trash", "hash", "photo"])
def test_a_field_outside_the_allowlist_is_refused_before_anything_is_fetched(monkeypatch, field):
    remote = RecipeRemote([roast()], [DINNER])
    monkeypatch.setattr(ur, "get_remote", lambda: pytest.fail("fetched the recipe"))
    text = TOOLS["update_recipe"]["handler"]({"id": "A1-0A", "field": field, "find": "A1", "replace": "ZZ"})[0].text
    assert text == (
        f"Error: Field '{field}' cannot be updated. Updatable fields: name, ingredients, directions, notes, "
        "description, source, source_url, prep_time, cook_time, total_time, servings, difficulty, "
        "nutritional_info, categories, rating."
    )
    assert remote.uploaded == []


def test_the_schema_advertises_exactly_the_allowlist():
    assert ur.TOOL_DEFINITION["inputSchema"]["properties"]["field"]["enum"] == list(ur.FIELDS)


# ---- rating ----


def test_a_rating_is_set_as_an_integer(monkeypatch):
    remote = RecipeRemote([roast()], [DINNER])
    text = update(remote, monkeypatch, field="rating", find="", replace="5")
    assert [u["rating"] for u in remote.uploaded] == [5]
    assert text == "Successfully updated field 'rating' in recipe 'Roast' (ID: A1-0A)\n\nOld value:\n3\n\nNew value:\n5"
    assert remote.notified == 1


@pytest.mark.parametrize("bad", ["6", "-1", "4.5", "five", ""])
def test_a_rating_outside_0_to_5_is_refused(monkeypatch, bad):
    remote = RecipeRemote([roast()], [DINNER])
    text = update(remote, monkeypatch, field="rating", find="3", replace=bad)
    assert text == f"Error: A rating is a whole number from 0 to 5, given as replace; got '{bad}'. No changes were saved."
    assert remote.uploaded == []


# ---- categories ----


def test_categories_are_replaced_by_name_and_uploaded_as_ids(monkeypatch):
    remote = RecipeRemote([roast()], [DINNER, SOUPS])
    text = update(remote, monkeypatch, field="categories", find="Dinner", replace="Soups")
    assert [u["categories"] for u in remote.uploaded] == [["C-SOU"]]
    assert text.startswith("Successfully updated categories in recipe 'Roast' (ID: A1-0A)")


def test_a_category_added_in_the_app_is_usable_on_the_next_call(monkeypatch):
    remote = RecipeRemote([roast()], [DINNER])
    first = update(remote, monkeypatch, field="categories", find="Dinner", replace="Soups")
    assert first == "Error: Category 'Soups' not found in Paprika — cannot convert back to UUID. No changes were saved."
    remote.categories.append(SOUPS)  # a member adds it in the app; the server keeps running
    update(remote, monkeypatch, field="categories", find="Dinner", replace="Soups")
    assert [u["categories"] for u in remote.uploaded] == [["C-SOU"]]


def test_a_failed_category_fetch_saves_nothing(monkeypatch):
    remote = RecipeRemote([roast()], [DINNER])
    remote.category_error = ConnectionError("timed out")
    text = update(remote, monkeypatch, field="categories", find="Dinner", replace="Soups")
    assert text == "Error: Could not fetch categories from Paprika (ConnectionError: timed out). No changes were saved."
    assert remote.uploaded == []
