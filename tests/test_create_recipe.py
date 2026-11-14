"""create_recipe against a fake remote: what it uploads, and what it refuses.

Every refusal is asserted by its exact text (plans/051 § The words). Each test
names, in its comment, the wrong implementation it exists to catch.
"""

import re

import pytest
from fakes import RecipeRemote, category
from paprika_recipes.remote import RemoteRecipe

from paprika_mcp.tools import TOOLS
from paprika_mcp.tools import create_recipe as cr
from paprika_mcp.tools.update_recipe import FIELD_LIMIT, TEXT_FIELDS

DINNER = category("C-DIN", "Dinner")
SOUPS = category("C-SOU", "Soups")
ETHNIC = category("C-ETH", "Ethnic")
THAI_TOP = category("C-THT", "Thai")
THAI_UNDER = category("C-THU", "Thai", parent="C-ETH")
CATEGORIES = [DINNER, SOUPS, ETHNIC, THAI_TOP, THAI_UNDER]

CHILI = RemoteRecipe(uid="A1-0A", name="Chili", ingredients="beans")
UUID4 = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}")
ACCEPTED = set(cr.ACCEPTED)
NOT_CREATE = " Nothing was created."


def create(remote, monkeypatch, **args):
    monkeypatch.setattr(cr, "get_remote", lambda: remote)
    return TOOLS["create_recipe"]["handler"](args)[0].text


def remote_with(*recipes, categories=CATEGORIES):
    return RecipeRemote(list(recipes), list(categories))


# ---- what is uploaded ----


def test_a_create_with_every_field_uploads_exactly_them(monkeypatch):
    # Catches a handler that drops a field, mints no uid or a fixed one, or skips the notify.
    remote = remote_with(CHILI)
    fields = {f: f"value of {f}" for f in TEXT_FIELDS if f != "name"}
    text = create(remote, monkeypatch, name="Roast", categories=["Dinner", "C-SOU"], rating=4, **fields)
    [up] = remote.uploaded
    assert {f: up[f] for f in fields} == fields
    assert (up["name"], up["rating"], up["categories"], up["in_trash"]) == ("Roast", 4, ["C-DIN", "C-SOU"], False)
    assert UUID4.fullmatch(up["uid"]) and up["uid"] != CHILI.uid
    assert text == f'Created recipe "Roast" (ID: {up["uid"]}). Categories: Dinner, Soups.'
    assert remote.notified == 1


def test_a_create_with_a_name_only_takes_the_defaults(monkeypatch):
    # Catches a handler that fills defaults of its own.
    remote = remote_with()
    text = create(remote, monkeypatch, name="Roast")
    [up] = remote.uploaded
    default = RemoteRecipe().as_dict()
    assert {f: up[f] for f in TEXT_FIELDS if f != "name"} == {f: default[f] for f in TEXT_FIELDS if f != "name"}
    assert (up["categories"], up["rating"], up["in_trash"]) == ([], 0, False)
    assert text == f'Created recipe "Roast" (ID: {up["uid"]}). No categories.'


def test_the_same_name_twice_with_keep_both_gives_two_uids(monkeypatch):
    # Catches a uid derived from the name (uuid5), which would overwrite the first.
    remote = remote_with(CHILI)
    create(remote, monkeypatch, name="Chili", keep_both=True)
    create(remote, monkeypatch, name="Chili", keep_both=True)
    first, second = remote.uploaded
    assert first["uid"] != second["uid"] and CHILI.uid not in (first["uid"], second["uid"])


def test_the_name_is_stored_stripped(monkeypatch):
    remote = remote_with()
    create(remote, monkeypatch, name="  Roast \n")
    assert [u["name"] for u in remote.uploaded] == ["Roast"]


# ---- arguments ----


@pytest.mark.parametrize("arg", sorted({f.name for f in RemoteRecipe.get_all_fields()} - ACCEPTED) + ["foo"])
def test_an_argument_outside_the_accepted_set_is_refused_before_anything_is_fetched(monkeypatch, arg):
    # Catches a denylist of the five obvious fields, or RemoteRecipe(**args).
    monkeypatch.setattr(cr, "get_remote", lambda: pytest.fail("fetched"))
    text = TOOLS["create_recipe"]["handler"]({"name": "Roast", arg: "x"})[0].text
    assert text == (
        f"Error: '{arg}' cannot be set when creating a recipe. Fields: name, ingredients, directions, notes, "
        "description, source, source_url, prep_time, cook_time, total_time, servings, difficulty, "
        "nutritional_info, categories, rating, keep_both. Nothing was created."
    )


def test_the_schema_advertises_exactly_the_accepted_arguments():
    # Catches a field accepted but not advertised, or the reverse.
    assert set(cr.TOOL_DEFINITION["inputSchema"]["properties"]) == ACCEPTED
    assert cr.TOOL_DEFINITION["inputSchema"]["required"] == ["name"]


def test_the_definition_is_annotated_as_a_non_destructive_write():
    assert cr.TOOL_DEFINITION["annotations"] == {"readOnlyHint": False, "destructiveHint": False}


@pytest.mark.parametrize("args", [{}, {"name": ""}, {"name": "   \n"}, {"name": None}])
def test_a_missing_empty_or_null_name_is_refused(monkeypatch, args):
    remote = remote_with()
    assert create(remote, monkeypatch, **args) == "Error: A recipe needs a name." + NOT_CREATE
    assert remote.uploaded == []


@pytest.mark.parametrize("rating", [6, -1, True, "3", 2.5])
def test_a_rating_that_is_not_a_whole_number_0_to_5_is_refused(monkeypatch, rating):
    # Catches `isinstance(rating, int)` alone, which takes True.
    remote = remote_with()
    text = create(remote, monkeypatch, name="Roast", rating=rating)
    assert text == f"Error: A rating is a whole number from 0 to 5; got {rating!r}." + NOT_CREATE
    assert remote.uploaded == []


@pytest.mark.parametrize("rating", [0, 5])
def test_the_ends_of_the_rating_range_pass(monkeypatch, rating):
    remote = remote_with()
    create(remote, monkeypatch, name="Roast", rating=rating)
    assert [u["rating"] for u in remote.uploaded] == [rating]


def test_keep_both_must_be_a_bool(monkeypatch):
    # Catches a truthiness read, which would take the string "false" as yes.
    remote = remote_with(CHILI)
    text = create(remote, monkeypatch, name="Chili", keep_both="false")
    assert text == "Error: keep_both is true or false; got 'false'." + NOT_CREATE
    assert remote.uploaded == []


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_a_field_at_the_limit_passes_and_one_over_is_refused(monkeypatch, field):
    # Catches an off-by-one, or a bound on some fields only.
    remote = remote_with()
    args = {"name": "Roast"}
    args[field] = "x" * FIELD_LIMIT
    create(remote, monkeypatch, **args)
    assert len(remote.uploaded) == 1
    args[field] = "x" * (FIELD_LIMIT + 1)
    text = create(remote, monkeypatch, **args)
    assert text == f"Error: Field '{field}' is {FIELD_LIMIT + 1} characters; a field may hold at most {FIELD_LIMIT}." + NOT_CREATE
    assert len(remote.uploaded) == 1


# ---- a name that is already taken ----


def test_a_name_a_live_recipe_has_is_refused_with_its_id_whatever_the_case_or_accents(monkeypatch):
    # Catches an exact-string comparison.
    remote = remote_with(RemoteRecipe(uid="A1-0A", name="Crème Brûlée"))
    text = create(remote, monkeypatch, name="CREME brulee")
    assert text == (
        'Error: A recipe titled "Crème Brûlée" already exists (ID: A1-0A). Nothing was created. '
        "To add this one as well, call create_recipe again with keep_both=true."
    )
    assert remote.uploaded == []


def test_two_live_recipes_with_the_name_give_the_plural_refusal(monkeypatch):
    # Catches swallowing AmbiguousTitle and creating a third.
    remote = remote_with(CHILI, RemoteRecipe(uid="B2-0B", name="chili"))
    text = create(remote, monkeypatch, name="Chili")
    assert text == (
        'Error: 2 recipes titled "Chili" already exist (IDs: A1-0A, B2-0B). Nothing was created. '
        "To add this one as well, call create_recipe again with keep_both=true."
    )
    assert remote.uploaded == []


def test_keep_both_true_creates_despite_the_name(monkeypatch):
    remote = remote_with(CHILI)
    assert create(remote, monkeypatch, name="Chili", keep_both=True).startswith('Created recipe "Chili"')
    assert len(remote.uploaded) == 1


def test_a_name_only_a_trashed_recipe_has_creates(monkeypatch):
    # Catches a duplicate check that counts the trash.
    remote = remote_with(RemoteRecipe(uid="A1-0A", name="Chili", in_trash=True))
    assert create(remote, monkeypatch, name="Chili").startswith('Created recipe "Chili"')


def test_the_name_is_stripped_before_the_duplicate_check(monkeypatch):
    # Catches a check made on the name as given.
    remote = remote_with(CHILI)
    text = create(remote, monkeypatch, name=" Chili")
    assert text.startswith('Error: A recipe titled "Chili" already exists (ID: A1-0A).')
    assert remote.uploaded == []


def test_a_failed_recipe_walk_refuses_the_create(monkeypatch):
    # Catches treating an unreadable list as "no duplicates".
    class Failing(RecipeRemote):
        def get_recipe_by_id(self, uid, hash):
            raise ConnectionError("timed out")

    remote = Failing([RemoteRecipe(uid="A1-0A", name="Stew")], CATEGORIES)
    text = create(remote, monkeypatch, name="Roast")
    assert text == (
        'Error: Could not read the recipes to check whether "Roast" already exists (ConnectionError: timed out).'
        + NOT_CREATE
    )
    assert remote.uploaded == []


# ---- categories ----


def test_categories_are_de_duplicated_in_order(monkeypatch):
    # Catches no de-duplication: Dinner, dinner and Dinner's id are one.
    remote = remote_with()
    text = create(remote, monkeypatch, name="Roast", categories=["Dinner", "dinner", "C-DIN", "soups"])
    assert [u["categories"] for u in remote.uploaded] == [["C-DIN", "C-SOU"]]
    assert text.endswith("Categories: Dinner, Soups.")


def test_an_unknown_category_is_refused_in_the_create_form(monkeypatch):
    # Catches skipping unknown names.
    remote = remote_with()
    text = create(remote, monkeypatch, name="Roast", categories=["Dinner", "Desert"])
    assert text == (
        'Error: No category is named "Desert". Use list_categories to see them, or create_category to add it.'
        + NOT_CREATE
    )
    assert remote.uploaded == []


def test_a_shared_category_name_is_refused_with_both_ids(monkeypatch):
    # Catches picking the first Thai.
    remote = remote_with()
    text = create(remote, monkeypatch, name="Pad thai", categories=["thai"])
    assert text == (
        'Error: "thai" names 2 categories: "Thai" (id C-THT, top level), "Thai" (id C-THU, under "Ethnic"). '
        "Name one by its id." + NOT_CREATE
    )
    assert remote.uploaded == []


@pytest.mark.parametrize("given", ["C-THU", "c-thu"])
def test_a_category_given_by_id_in_either_case_is_accepted(monkeypatch, given):
    # Catches a lookup by name only, or a case-sensitive id.
    remote = remote_with()
    create(remote, monkeypatch, name="Pad thai", categories=[given])
    assert [u["categories"] for u in remote.uploaded] == [["C-THU"]]


def test_a_failed_category_fetch_is_not_no_such_category(monkeypatch):
    # Catches treating the failure as an empty list and answering "No category is named".
    remote = remote_with()
    remote.category_error = ConnectionError("timed out")
    text = create(remote, monkeypatch, name="Roast", categories=["Dinner"])
    assert text == "Error: Could not fetch categories from Paprika (ConnectionError: timed out)." + NOT_CREATE
    assert remote.uploaded == []


def test_no_categories_asked_means_none_are_fetched(monkeypatch):
    remote = remote_with()
    create(remote, monkeypatch, name="Roast")
    assert remote.category_gets == 0


# ---- the upload and the notify ----


def test_a_failed_upload_says_it_cannot_tell(monkeypatch):
    # Catches an answer of "failed" when the POST may have landed.
    remote = remote_with()

    def fail(recipe):
        raise ConnectionError("reset")

    remote.upload_recipe = fail
    text = create(remote, monkeypatch, name="Roast")
    assert text == (
        'Error creating recipe "Roast": reset. It may or may not have reached Paprika; '
        "search for it before trying again."
    )
    assert remote.notified == 0


def test_a_failed_notify_is_success_plus_the_notify_sentence(monkeypatch):
    remote = remote_with()
    remote.notify_error = ConnectionError("reset")
    text = create(remote, monkeypatch, name="Roast")
    uid = remote.uploaded[0]["uid"]
    assert text == (
        f'Created recipe "Roast" (ID: {uid}). No categories.\n\n'
        "The change is saved in Paprika, but the apps could not be told to refresh "
        "(ConnectionError: reset); they will show it at their next sync."
    )


def test_a_recipe_list_answered_without_result_refuses_the_create(monkeypatch):
    # Catches the library's `.get("result", [])`, which reads such an answer as no recipes.
    remote = RecipeRemote([], CATEGORIES)
    remote.recipe_list_body = {}
    text = create(remote, monkeypatch, name="Roast")
    assert text == (
        'Error: Could not read the recipes to check whether "Roast" already exists (RequestError: Paprika answered {}).'
        + NOT_CREATE
    )
    assert remote.uploaded == []
