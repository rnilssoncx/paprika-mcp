"""create_category, rename_category, move_category and delete_category.

The fake records each category POST as sent (strictly decoded), so tests assert
the payload, not the list afterwards. Every refusal is asserted by its exact
text (plans/051 § The words); each test's comment names the wrong implementation
it exists to catch.
"""

import re

import pytest
from fakes import RecipeRemote, Response, category
from paprika_recipes.remote import RemoteRecipe

from paprika_mcp import utils
from paprika_mcp.tools import TOOLS
from paprika_mcp.tools import categories as cat_tools

UUID4 = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}")
NOT_CHANGED = " Nothing was changed."
NOT_DELETED = " Nothing was deleted."

DINNER = category("C-DIN", "Dinner", order_flag=0)
SOUPS = category("C-SOU", "Soups", order_flag=9)
ETHNIC = category("C-ETH", "Ethnic", order_flag=3)
THAI_TOP = category("C-THT", "Thai", order_flag=4)
THAI_UNDER = category("C-THU", "Thai", parent="C-ETH", order_flag=5)
CATEGORIES = [DINNER, SOUPS, ETHNIC, THAI_TOP, THAI_UNDER]


def run(tool, remote, monkeypatch, **args):
    monkeypatch.setattr(cat_tools, "get_remote", lambda: remote)
    return TOOLS[tool]["handler"](args)[0].text


def remote_with(*categories, recipes=()):
    return RecipeRemote(list(recipes), list(categories) or list(CATEGORIES))


def recipe(uid, name, *cats, in_trash=False):
    return RemoteRecipe(uid=uid, name=name, categories=list(cats), in_trash=in_trash)


# ---- the exact object each write sends ----


def test_create_posts_one_new_object_last_in_order_and_notifies(monkeypatch):
    # Catches POSTing the whole list (spike S1's risk), and `last + 1` or `len(categories)`.
    remote = remote_with(category("C-A", "A", order_flag=0), category("C-B", "B", order_flag=9), category("C-C", "C", order_flag=3))
    text = run("create_category", remote, monkeypatch, name="Brunch")
    [[sent]] = remote.category_posts
    assert UUID4.fullmatch(sent["uid"])
    assert sent == {"uid": sent["uid"], "name": "Brunch", "order_flag": 10, "parent_uid": None, "cookbook_uid": None}
    assert text == 'Created category "Brunch" at the top level.'
    assert remote.notified == 1


def test_create_under_a_parent_carries_the_parents_uid(monkeypatch):
    remote = remote_with(*CATEGORIES)
    text = run("create_category", remote, monkeypatch, name="Brunch", parent="ethnic")
    assert remote.category_posts[0][0]["parent_uid"] == "C-ETH"
    assert text == 'Created category "Brunch" under "Ethnic".'


def test_create_in_an_empty_account_starts_at_zero(monkeypatch):
    remote = RecipeRemote([], [])
    run("create_category", remote, monkeypatch, name="Brunch")
    assert remote.category_posts[0][0]["order_flag"] == 0


def test_rename_and_move_post_the_object_as_got_with_only_one_field_changed(monkeypatch):
    # Catches building the object from known keys, which would drop `future`.
    fixture = {**SOUPS, "future": "x"}
    remote = remote_with(fixture, ETHNIC)
    run("rename_category", remote, monkeypatch, category="Soups", new_name="Broths")
    assert remote.category_posts == [[{**fixture, "name": "Broths"}]]
    remote = remote_with(fixture, ETHNIC)
    run("move_category", remote, monkeypatch, category="Soups", parent="Ethnic")
    assert remote.category_posts == [[{**fixture, "parent_uid": "C-ETH"}]]


def test_move_to_the_top_level_sets_parent_to_null(monkeypatch):
    remote = remote_with(*CATEGORIES)
    text = run("move_category", remote, monkeypatch, category="C-THU", parent="")
    assert remote.category_posts == [[{**THAI_UNDER, "parent_uid": None}]]
    assert text == 'Moved category "Thai" to the top level.'


def test_delete_posts_the_whole_object_with_deleted_true(monkeypatch):
    # Catches POSTing {uid, deleted} alone, which spike S5 never proved.
    fixture = {**SOUPS, "future": "x"}
    remote = remote_with(fixture, DINNER)
    text = run("delete_category", remote, monkeypatch, category="Soups")
    assert remote.category_posts == [[{**fixture, "deleted": True}]]
    assert text == 'Deleted category "Soups".'
    assert remote.notified == 1
    assert [c["uid"] for c in remote.categories] == ["C-DIN"]


def test_rename_to_the_same_name_posts_the_same_object(monkeypatch):
    # Catches a no-op shortcut: the design POSTs an idempotent upsert.
    remote = remote_with(*CATEGORIES)
    text = run("rename_category", remote, monkeypatch, category="Soups", new_name="Soups")
    assert remote.category_posts == [[SOUPS]]
    assert text == 'Renamed category "Soups" to "Soups".'


def test_move_to_where_it_already_is_posts_the_same_object(monkeypatch):
    remote = remote_with(*CATEGORIES)
    text = run("move_category", remote, monkeypatch, category="C-THU", parent="Ethnic")
    assert remote.category_posts == [[THAI_UNDER]]
    assert text == 'Moved category "Thai" under "Ethnic".'


# ---- names ----


@pytest.mark.parametrize(
    "name, why",
    [
        ("", "an empty name"),
        ("   ", "an empty name"),
        ("a\nb", "a line break or control character (U+000A)"),
        ("a\rb", "a line break or control character (U+000D)"),
        ("a\x00b", "a line break or control character (U+0000)"),
        ("a\x0bb", "a line break or control character (U+000B)"),
        ("a b", "a line break or control character (U+2028)"),
        ("a b", "a line break or control character (U+2029)"),
        ("a\x85b", "a line break or control character (U+0085)"),
    ],
)
@pytest.mark.parametrize("tool", ["create_category", "rename_category"])
def test_a_bad_name_is_refused_before_any_write(monkeypatch, tool, name, why):
    # Catches a check for \n and \t only.
    remote = remote_with(*CATEGORIES)
    args = {"name": name} if tool == "create_category" else {"category": "Soups", "new_name": name}
    text = run(tool, remote, monkeypatch, **args)
    assert remote.category_posts == []
    assert text == (
        "Error: A category name must be on one line, with no control characters, and not empty; "
        f"got {why}. Nothing was changed."
    )


def test_a_name_is_stored_stripped(monkeypatch):
    remote = remote_with(*CATEGORIES)
    run("create_category", remote, monkeypatch, name="  Brunch \n")
    assert remote.category_posts[0][0]["name"] == "Brunch"


@pytest.mark.parametrize("tool", ["create_category", "rename_category"])
def test_a_taken_name_is_refused_across_levels_case_and_accents(monkeypatch, tool):
    # Catches siblings only, and an exact-string comparison.
    remote = remote_with(category("C-CAF", "Café"), SOUPS, ETHNIC, category("C-X", "Pad", parent="C-ETH"))
    args = {"name": "CAFE"} if tool == "create_category" else {"category": "Soups", "new_name": "CAFE"}
    assert run(tool, remote, monkeypatch, **args) == 'Error: A category named "CAFE" already exists (top level).' + NOT_CHANGED
    args = {"name": "pad"} if tool == "create_category" else {"category": "Soups", "new_name": "pad"}
    assert run(tool, remote, monkeypatch, **args) == 'Error: A category named "pad" already exists (under "Ethnic").' + NOT_CHANGED
    assert remote.category_posts == []


def test_a_name_already_shared_lists_every_place(monkeypatch):
    # Catches a singular-only `where`.
    remote = remote_with(*CATEGORIES)
    text = run("create_category", remote, monkeypatch, name="thai")
    assert text == 'Error: A category named "thai" already exists (top level; under "Ethnic").' + NOT_CHANGED



def test_a_category_may_be_renamed_to_its_own_name_in_another_case(monkeypatch):
    # Catches counting the category itself as the taker.
    remote = remote_with(*CATEGORIES)
    text = run("rename_category", remote, monkeypatch, category="Soups", new_name="soups")
    assert text == 'Renamed category "Soups" to "soups".'
    assert remote.category_posts[0][0]["name"] == "soups"


def test_the_nested_thai_cannot_be_renamed_to_the_name_the_top_level_thai_has(monkeypatch):
    # Catches excluding takers by the folded old name instead of by uid: that
    # would exclude both Thais and allow it.
    remote = remote_with(*CATEGORIES)
    text = run("rename_category", remote, monkeypatch, category="C-THU", new_name="THAI")
    assert text == 'Error: A category named "THAI" already exists (top level).' + NOT_CHANGED
    assert remote.category_posts == []


# ---- nesting ----


@pytest.mark.parametrize(
    "tool, args",
    [
        ("create_category", {"name": "Brunch", "parent": "C-THU"}),
        ("move_category", {"category": "Soups", "parent": "C-THU"}),
    ],
)
def test_a_parent_that_is_itself_nested_is_refused(monkeypatch, tool, args):
    # Catches a depth check on one tool only.
    remote = remote_with(*CATEGORIES)
    assert run(tool, remote, monkeypatch, **args) == (
        'Error: "Thai" is itself under "Ethnic"; categories nest one level deep.' + NOT_CHANGED
    )
    assert remote.category_posts == []


def test_a_category_with_children_cannot_go_under_another(monkeypatch):
    # Catches forgetting the moved category's own children.
    remote = remote_with(*CATEGORIES)
    text = run("move_category", remote, monkeypatch, category="Ethnic", parent="Soups")
    assert text == (
        'Error: "Ethnic" has 1 category under it, so it cannot go under another; categories nest one level deep.'
        + NOT_CHANGED
    )
    remote = remote_with(*CATEGORIES, category("C-NEW", "Pizza", parent="C-ETH"))
    assert run("move_category", remote, monkeypatch, category="Ethnic", parent="Soups").startswith(
        'Error: "Ethnic" has 2 categories under it,'
    )
    assert remote.category_posts == []


def test_a_childless_top_level_category_cannot_go_under_itself(monkeypatch):
    # Catches no self-check, which the other two rules shadow on every other fixture.
    remote = remote_with(*CATEGORIES)
    text = run("move_category", remote, monkeypatch, category="Soups", parent="c-sou")
    assert text == "Error: A category cannot go under itself." + NOT_CHANGED
    assert remote.category_posts == []


# ---- delete ----


def test_delete_in_use_counts_all_and_names_the_first_five_alphabetically(monkeypatch):
    # Catches no cap, a count taken from the titles shown, and a case-sensitive sort.
    titles = ["f", "E", "d", "C", "b", "A"]
    recipes = [recipe(f"R{i}", t, "C-SOU") for i, t in enumerate(titles)]
    remote = remote_with(*CATEGORIES, recipes=recipes)
    text = run("delete_category", remote, monkeypatch, category="Soups")
    assert text == (
        'Error: 6 recipes are in category "Soups", including "A", "b", "C", "d", "E".' + NOT_DELETED + " "
        "Take them out of it with update_recipe first, or rename the category instead."
    )
    assert remote.category_posts == []


def test_a_recipe_in_the_trash_does_not_block_and_is_counted(monkeypatch):
    # Catches counting the trash as blocking, or ignoring it in the answer.
    # R9 is trashed but in another category: counting every trashed recipe would say 2.
    trashed = [recipe("R1", "Old", "C-SOU", in_trash=True), recipe("R9", "Other", "C-DIN", in_trash=True)]
    remote = remote_with(*CATEGORIES, recipes=trashed)
    assert run("delete_category", remote, monkeypatch, category="Soups") == (
        'Deleted category "Soups". 1 recipe in the trash was in it; if one is restored it will show as in an unknown category.'
    )
    two = [recipe("R1", "Old", "C-SOU", in_trash=True), recipe("R2", "Older", "C-SOU", in_trash=True)]
    remote = remote_with(*CATEGORIES, recipes=two)
    assert run("delete_category", remote, monkeypatch, category="Soups").endswith(
        " 2 recipes in the trash were in it; if one is restored it will show as in an unknown category."
    )


def test_one_live_and_one_trashed_gives_the_singular_wording_and_blocks(monkeypatch):
    recipes = [recipe("R1", "Live", "C-SOU"), recipe("R2", "Old", "C-SOU", in_trash=True)]
    remote = remote_with(*CATEGORIES, recipes=recipes)
    text = run("delete_category", remote, monkeypatch, category="Soups")
    assert text.startswith('Error: 1 recipe is in category "Soups", including "Live".')
    assert remote.category_posts == []


def test_delete_counts_by_uid_not_by_name_and_a_recipe_with_no_categories_does_not_block(monkeypatch):
    # Catches counting by name: the recipe is in the top-level Thai only.
    in_top = recipe("R1", "Pad thai", "C-THT")
    none = RemoteRecipe(uid="R2", name="Plain", categories=None)
    remote = remote_with(*CATEGORIES, recipes=[in_top, none])
    assert run("delete_category", remote, monkeypatch, category="C-THU") == 'Deleted category "Thai".'
    assert remote.category_posts == [[{**THAI_UNDER, "deleted": True}]]


def test_a_category_with_children_cannot_be_deleted(monkeypatch):
    # Catches orphaning the children.
    remote = remote_with(*CATEGORIES)
    text = run("delete_category", remote, monkeypatch, category="Ethnic")
    assert text == 'Error: "Ethnic" has 1 category under it: "Thai".' + NOT_DELETED + " Move or delete them first."
    assert remote.category_posts == []


def test_a_recipe_walk_that_fails_partway_refuses_the_delete(monkeypatch):
    # Catches counting from what was read before the failure: the one recipe
    # read is not in the category, so a partial count would say "none".
    class Failing(RecipeRemote):
        def get_recipe_by_id(self, uid, hash):
            if uid == "R2":
                raise ConnectionError("timed out")
            return super().get_recipe_by_id(uid, hash)

    remote = Failing(
        [RemoteRecipe(uid="R1", name="Elsewhere", categories=["C-DIN"]), RemoteRecipe(uid="R2", name="Soup", categories=["C-SOU"])],
        list(CATEGORIES),
    )
    text = run("delete_category", remote, monkeypatch, category="Soups")
    assert text == (
        'Error: Could not read the recipes to check whether any is in "Soups" (ConnectionError: timed out).'
        + NOT_DELETED
    )
    assert remote.category_posts == []


# ---- the lookup, for every tool and argument ----

UNKNOWN = 'Error: No category is named "Desserts". Use list_categories to see them.'
SHARED = (
    'Error: "Thai" names 2 categories: "Thai" (id C-THT, top level), "Thai" (id C-THU, under "Ethnic"). '
    "Name one by its id."
)


@pytest.mark.parametrize("given, sentence", [("Desserts", UNKNOWN), ("Thai", SHARED)])
@pytest.mark.parametrize(
    "tool, field, others",
    [
        ("create_category", "parent", {"name": "Brunch"}),
        ("rename_category", "category", {"new_name": "Brunch"}),
        ("move_category", "category", {"parent": ""}),
        ("move_category", "parent", {"category": "Soups"}),
        ("delete_category", "category", {}),
    ],
)
def test_each_lookup_refusal_reaches_every_tool_and_argument_in_the_category_form(monkeypatch, tool, field, others, given, sentence):
    # Catches a tool or argument that resolves names its own way.
    remote = remote_with(*CATEGORIES)
    assert run(tool, remote, monkeypatch, **{field: given}, **others) == sentence
    assert remote.category_posts == []


# ---- the POST and the notify ----


@pytest.mark.parametrize("answer", [False, "true", 1])
def test_a_post_answered_without_result_true_is_cant_tell_and_not_notified(monkeypatch, answer):
    # Catches reading any 200 as success (and truthiness: "true" and 1 are not True).
    remote = remote_with(*CATEGORIES)
    remote.category_post_result = answer
    text = run("rename_category", remote, monkeypatch, category="Soups", new_name="Broths")
    assert text == (
        f'Error writing category "Broths": Paprika answered {{\'result\': {answer!r}}}. '
        "The change may or may not have reached Paprika; check list_categories before trying again."
    )
    assert remote.notified == 0


def test_a_raising_post_is_cant_tell_and_not_notified(monkeypatch):
    remote = remote_with(*CATEGORIES)
    remote.category_post_error = ConnectionError("reset")
    text = run("delete_category", remote, monkeypatch, category="Soups")
    assert text == (
        'Error writing category "Soups": reset. '
        "The change may or may not have reached Paprika; check list_categories before trying again."
    )
    assert remote.notified == 0


NOTIFY = (
    "\n\nThe change is saved in Paprika, but the apps could not be told to refresh "
    "(ConnectionError: reset); they will show it at their next sync."
)


@pytest.mark.parametrize(
    "tool, args, success",
    [
        ("create_category", {"name": "Brunch"}, 'Created category "Brunch" at the top level.'),
        ("rename_category", {"category": "Soups", "new_name": "Broths"}, 'Renamed category "Soups" to "Broths".'),
        ("move_category", {"category": "Soups", "parent": "Ethnic"}, 'Moved category "Soups" under "Ethnic".'),
        ("delete_category", {"category": "Soups"}, 'Deleted category "Soups".'),
    ],
)
def test_a_failed_notify_is_success_plus_the_notify_sentence(monkeypatch, tool, args, success):
    # Catches a notify failure reported as a failed write.
    remote = remote_with(*CATEGORIES)
    remote.notify_error = ConnectionError("reset")
    assert run(tool, remote, monkeypatch, **args) == success + NOTIFY
    assert len(remote.category_posts) == 1


@pytest.mark.parametrize(
    "tool, args",
    [
        ("create_category", {"name": "Brunch"}),
        ("rename_category", {"category": "Soups", "new_name": "Broths"}),
        ("move_category", {"category": "Soups", "parent": ""}),
        ("delete_category", {"category": "Soups"}),
    ],
)
def test_a_failed_category_fetch_answers_its_reason_and_writes_nothing(monkeypatch, tool, args):
    # Catches a timeout reported as "not changed".
    remote = remote_with(*CATEGORIES)
    remote.category_error = ConnectionError("timed out")
    assert run(tool, remote, monkeypatch, **args) == "Error: Could not fetch categories from Paprika (ConnectionError: timed out)." + NOT_CHANGED
    assert remote.category_posts == []


# ---- what is advertised ----


def test_the_tools_are_advertised_with_their_annotations_and_the_danger_wording():
    # Catches annotations dropped in tools/__init__.py.
    expected = {"create_category": False, "rename_category": True, "move_category": True, "delete_category": True}
    for name, destructive in expected.items():
        definition = TOOLS[name]["definition"]
        assert definition["annotations"] == {"readOnlyHint": False, "destructiveHint": destructive}, name
        assert ("DANGEROUS operation that requires user confirmation" in definition["description"]) is destructive, name
    assert TOOLS["create_recipe"]["definition"]["annotations"] == {"readOnlyHint": False, "destructiveHint": False}


# ---- the shared helpers in utils ----


def test_an_id_wins_over_a_name_and_is_case_insensitive():
    cats = [category("c-x", "Other"), category("B", "c-x")]  # a name equal to another's id
    assert utils.resolve_category(cats, "C-X", create_hint=False)["uid"] == "c-x"


def test_a_category_with_an_empty_name_is_never_matched_by_an_empty_given():
    with pytest.raises(utils.CategoryRefused, match='^Error: No category is named "". '):
        utils.resolve_category([category("C-E", "")], "  ", create_hint=False)


def test_a_recipe_list_answered_without_result_refuses_the_delete(monkeypatch):
    # Catches the library's walk reading such an answer as no recipes, which deletes a category in use.
    remote = remote_with(*CATEGORIES, recipes=[recipe("R1", "Soup", "C-SOU")])
    remote.recipe_list_body = {}
    text = run("delete_category", remote, monkeypatch, category="Soups")
    assert text == 'Error: Could not read the recipes to check whether any is in "Soups" (RequestError: Paprika answered {}).' + NOT_DELETED
    assert remote.category_posts == []


def test_a_category_list_answered_without_result_is_unreadable(monkeypatch):
    # Catches reading it as no categories, which let a taken name through on create.
    class Odd(RecipeRemote):
        def _request(self, method, path, **kwargs):
            if (method, path) == ("get", "/api/v2/sync/categories/"):
                return Response({})
            return super()._request(method, path, **kwargs)

    remote = Odd([], list(CATEGORIES))
    text = run("create_category", remote, monkeypatch, name="Soups")
    assert text == "Error: Could not fetch categories from Paprika (ValueError: Paprika answered {}). Nothing was changed."
    assert remote.category_posts == []


def test_a_child_with_an_empty_name_still_blocks_deleting_its_parent(monkeypatch):
    # Catches building the checks on `by_uid`, which skips a category with an empty name.
    remote = remote_with(DINNER, ETHNIC, category("C-E", "", parent="C-ETH"))
    text = run("delete_category", remote, monkeypatch, category="Ethnic")
    assert text == 'Error: "Ethnic" has 1 category under it: "". Nothing was deleted. Move or delete them first.'
    assert remote.category_posts == []


def test_two_new_categories_get_different_uids(monkeypatch):
    # Catches a fixed uid: the API upserts, so the second would replace the first.
    remote = remote_with(*CATEGORIES)
    run("create_category", remote, monkeypatch, name="Brunch")
    run("create_category", remote, monkeypatch, name="Supper")
    first, second = (batch[0]["uid"] for batch in remote.category_posts)
    assert first != second


def test_the_lookup_folds_accents():
    # Catches `.lower()` in place of normalize_string.
    assert utils.resolve_category([category("C-CAF", "Café")], "cafe", create_hint=False)["uid"] == "C-CAF"
