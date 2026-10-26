"""search_recipes, read_recipe and list_categories against a fake remote.

The theme is the absence rule: "no recipes found" must only ever mean that the
search ran and found nothing — never that the pattern was invalid, the category
name unknown, or the category list could not be fetched.
"""

import pytest
from fakes import RecipeRemote, category
from paprika_recipes.remote import RemoteRecipe

from paprika_mcp import utils
from paprika_mcp.tools import TOOLS
from paprika_mcp.tools import list_categories as lc
from paprika_mcp.tools import read_recipe as rr
from paprika_mcp.tools import search_recipes as sr

DINNER = category("C-DIN", "Dinner")
SOUPS = category("C-SOU", "Soups")

SOUP = RemoteRecipe(uid="A1-0A", name="Tomato soup", ingredients="4 tomatoes\n1 onion", directions="Simmer.", notes="", categories=["C-SOU"])
ROAST = RemoteRecipe(uid="B2-0B", name="Roast", ingredients="1 chicken", directions="Roast.", notes="", categories=["C-DIN"])


def call(tool, module, remote, monkeypatch, **args):
    monkeypatch.setattr(module, "get_remote", lambda: remote)
    return TOOLS[tool]["handler"](args)[0].text


def search(remote, monkeypatch, **args):
    return call("search_recipes", sr, remote, monkeypatch, **args)


# ---- search: refusals that are not "no recipes found" ----


def test_an_invalid_regex_is_reported_as_invalid(monkeypatch):
    text = search(RecipeRemote([SOUP], [SOUPS]), monkeypatch, query="1/2 cup (", regex=True)
    assert text == "Error: Invalid regex pattern '1/2 cup (': missing ), unterminated subpattern at position 8"


def test_an_overlong_regex_is_refused(monkeypatch):
    pattern = "a" * 201
    text = search(RecipeRemote([SOUP], [SOUPS]), monkeypatch, query=pattern, regex=True)
    assert text == "Error: Regex pattern is 201 characters; the limit is 200."


def test_a_regex_at_the_length_limit_runs(monkeypatch):
    pattern = "(tomato|" + "x" * 191 + ")"
    assert len(pattern) == 200
    assert search(RecipeRemote([SOUP], [SOUPS]), monkeypatch, query=pattern, regex=True).startswith("Found 1 recipes")


def test_the_same_long_text_is_fine_as_a_plain_search(monkeypatch):
    # The cap is on patterns, which cost backtracking, not on plain text.
    text = search(RecipeRemote([SOUP], [SOUPS]), monkeypatch, query="a" * 201)
    assert text == f"No recipes found matching '{'a' * 201}'"


@pytest.mark.parametrize("page", [0, -1])
def test_a_page_below_1_is_refused(monkeypatch, page):
    text = search(RecipeRemote([SOUP], [SOUPS]), monkeypatch, query="", page=page)
    assert text == f"Error: page must be a whole number of 1 or more, got {page}."


@pytest.mark.parametrize("page_size", [0, -5])
def test_a_page_size_below_1_is_refused(monkeypatch, page_size):
    text = search(RecipeRemote([SOUP], [SOUPS]), monkeypatch, query="", page_size=page_size)
    assert text == f"Error: page_size must be a whole number of 1 or more, got {page_size}."


def test_an_unknown_category_is_not_an_empty_one(monkeypatch):
    text = search(RecipeRemote([SOUP], [SOUPS]), monkeypatch, query="", category="Desserts")
    assert text == "Error: No category named 'Desserts'. Use list_categories to see the categories."


def test_a_known_category_with_no_matches_is_no_recipes(monkeypatch):
    text = search(RecipeRemote([SOUP, ROAST], [SOUPS, DINNER]), monkeypatch, query="chicken", category="Soups")
    assert text == "No recipes found matching 'chicken' in category 'Soups'"


def test_a_failed_category_fetch_is_not_no_recipes(monkeypatch):
    remote = RecipeRemote([SOUP], [SOUPS])
    remote.category_error = ConnectionError("timed out")
    text = search(remote, monkeypatch, query="tomato", category="Soups")
    assert text == "Error: Could not fetch categories from Paprika (ConnectionError: timed out), so the search was not run."


# ---- search: categories are fetched per call ----


def test_a_category_added_in_the_app_is_found_on_the_next_call(monkeypatch):
    remote = RecipeRemote([SOUP], [DINNER])
    assert search(remote, monkeypatch, query="", category="Soups") == "Error: No category named 'Soups'. Use list_categories to see the categories."
    remote.categories.append(SOUPS)  # a member adds it in the app; the server keeps running
    assert search(remote, monkeypatch, query="", category="Soups").startswith("Found 1 recipes")


def test_a_search_fetches_the_categories_once_not_once_per_recipe(monkeypatch):
    remote = RecipeRemote([SOUP, ROAST], [SOUPS, DINNER])
    search(remote, monkeypatch, query="Soups", fields=["categories"])
    assert remote.category_gets == 1


def test_category_names_are_searchable(monkeypatch):
    text = search(RecipeRemote([SOUP, ROAST], [SOUPS, DINNER]), monkeypatch, query="soups", fields=["categories"])
    assert text.startswith("Found 1 recipes with 1 total matches for 'soups'")
    assert "Tomato soup (ID: A1-0A)" in text and "Roast" not in text


def test_a_regex_search_matches_case_insensitively(monkeypatch):
    text = search(RecipeRemote([SOUP, ROAST], [SOUPS, DINNER]), monkeypatch, query="TOMATO(es)?", regex=True, fields=["ingredients"])
    assert text.startswith("Found 1 recipes")


# ---- read_recipe by title ----

LIVE = RemoteRecipe(uid="L1-0A", name="Soup", ingredients="new", in_trash=False)
TRASHED = RemoteRecipe(uid="T1-0A", name="Soup", ingredients="old", in_trash=True)
TWIN = RemoteRecipe(uid="L2-0B", name="soup", ingredients="other", in_trash=False)


def read(remote, monkeypatch, **args):
    return call("read_recipe", rr, remote, monkeypatch, **args)


def test_a_trashed_recipe_does_not_shadow_the_live_one(monkeypatch):
    text = read(RecipeRemote([TRASHED, LIVE]), monkeypatch, title="Soup", fields=["ingredients"])
    assert text == "# Soup\n\n**UID:** L1-0A\n\n## Ingredients\nnew"


def test_a_title_held_only_by_a_trashed_recipe_is_not_found(monkeypatch):
    assert read(RecipeRemote([TRASHED]), monkeypatch, title="Soup") == "Error: No recipe found with title 'Soup'"


def test_two_live_recipes_with_one_title_are_refused_with_their_ids(monkeypatch):
    text = read(RecipeRemote([LIVE, TRASHED, TWIN]), monkeypatch, title="Soup")
    assert text == "Error: 2 recipes are titled 'Soup': L1-0A, L2-0B. Read one by id."


def test_read_shows_category_names_fetched_for_the_call(monkeypatch):
    text = read(RecipeRemote([SOUP], [SOUPS]), monkeypatch, id="A1-0A", fields=["categories"])
    assert text == "# Tomato soup\n\n**UID:** A1-0A\n\n## Categories\nSoups"


def test_read_says_when_category_names_could_not_be_fetched(monkeypatch):
    remote = RecipeRemote([SOUP], [SOUPS])
    remote.category_error = ConnectionError("timed out")
    text = read(remote, monkeypatch, id="A1-0A", fields=["categories"])
    assert text == "# Tomato soup\n\n**UID:** A1-0A\n\n## Categories\n(Could not fetch categories from Paprika (ConnectionError: timed out).)"


def test_read_without_categories_does_not_fetch_them(monkeypatch):
    remote = RecipeRemote([SOUP], [SOUPS])
    read(remote, monkeypatch, id="A1-0A", fields=["ingredients"])
    assert remote.category_gets == 0


# ---- list_categories ----


def test_list_categories_shows_a_category_added_since_the_last_call(monkeypatch):
    remote = RecipeRemote([], [DINNER])
    assert call("list_categories", lc, remote, monkeypatch).startswith("Found 1 categories:")
    remote.categories.append(SOUPS)
    assert call("list_categories", lc, remote, monkeypatch).startswith("Found 2 categories:")


def test_a_failed_category_fetch_is_not_no_categories(monkeypatch):
    remote = RecipeRemote([], [DINNER])
    remote.category_error = ConnectionError("timed out")
    assert call("list_categories", lc, remote, monkeypatch) == "Error: Could not fetch categories from Paprika (ConnectionError: timed out)."


# ---- the recipe cache, shared by concurrent tool calls ----


def test_two_caches_saving_over_one_directory_keep_both_entries(tmp_path):
    # Two tool calls on two threads each hold a cache over the same directory.
    # The library's save writes its whole index over the file, so the second
    # save would discard the first call's entries.
    first, second = utils._SharedDirectoryCache(str(tmp_path)), utils._SharedDirectoryCache(str(tmp_path))
    first.store_in_cache("A1-0A", "h1", {"name": "Soup"})
    second.store_in_cache("B2-0B", "h2", {"name": "Roast"})
    first.save()
    second.save()
    fresh = utils._SharedDirectoryCache(str(tmp_path))
    assert fresh.read_from_cache("A1-0A", "h1") == {"name": "Soup"}
    assert fresh.read_from_cache("B2-0B", "h2") == {"name": "Roast"}


def test_the_cache_leaves_no_temporary_files(tmp_path):
    cache = utils._SharedDirectoryCache(str(tmp_path))
    cache.store_in_cache("A1-0A", "h1", {"name": "Soup"})
    cache.save()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["A1-0A.h1.json", "index.json"]


def test_get_remote_uses_the_shared_cache(monkeypatch, tmp_path):
    seen = {}

    class Remote:
        def __init__(self, email, password, cache, user_agent, timeout):
            seen["cache"] = cache

        bearer_token = "tok"

    monkeypatch.setattr(utils, "Remote", Remote)
    monkeypatch.setattr(utils, "get_credentials", lambda: ("e", "p"))
    monkeypatch.setattr(utils, "get_user_agent", lambda: None)
    monkeypatch.setattr(utils.os.path, "expanduser", lambda path: str(tmp_path / "cache"))
    utils.get_remote()
    assert type(seen["cache"]) is utils._SharedDirectoryCache


def test_the_index_never_leads_to_another_version_of_a_recipe(tmp_path):
    # Two calls fetch one recipe either side of an edit. The second to save
    # holds the OLDER hash, so the merged index names the old version — and
    # with one file per recipe that file now holds the new one.
    first, second = utils._SharedDirectoryCache(str(tmp_path)), utils._SharedDirectoryCache(str(tmp_path))
    first.store_in_cache("A1-0A", "h1", {"name": "Old soup"})
    second.store_in_cache("A1-0A", "h2", {"name": "Soup"})
    second.save()
    first.save()
    fresh = utils._SharedDirectoryCache(str(tmp_path))
    version = fresh.index["A1-0A"]
    assert fresh.read_from_cache("A1-0A", version) == {"h1": {"name": "Old soup"}, "h2": {"name": "Soup"}}[version]


def test_a_new_version_replaces_the_old_file(tmp_path):
    cache = utils._SharedDirectoryCache(str(tmp_path))
    (tmp_path / "A1-0A.json").write_text("{}")  # the library's own layout, from before
    cache.store_in_cache("A1-0A", "h1", {"name": "Old soup"})
    cache.store_in_cache("A1-0A", "h2", {"name": "Soup"})
    cache.save()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["A1-0A.h2.json", "index.json"]
    assert cache.read_from_cache("A1-0A", "h2") == {"name": "Soup"}
    assert cache.read_from_cache("A1-0A", "h1") == {}


def test_an_id_that_is_not_an_id_is_not_cached(tmp_path):
    cache = utils._SharedDirectoryCache(str(tmp_path))
    cache.store_in_cache("../escape", "h1", {"name": "x"})
    cache.save()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index.json"]


# ---- accents: nobody types them ----

ONION = RemoteRecipe(uid="C3-0C", name="Crème Brûlée", ingredients="1 cup shredded Gruyère", directions="Bake.", notes="", categories=[])


def test_a_plain_search_without_the_accent_finds_the_accented_word(monkeypatch):
    text = search(RecipeRemote([ONION, SOUP], [SOUPS]), monkeypatch, query="gruyere", fields=["ingredients"])
    assert text.startswith("Found 1 recipes with 1 total matches for 'gruyere'")


def test_a_regex_without_the_accent_finds_the_accented_word(monkeypatch):
    text = search(RecipeRemote([ONION, SOUP], [SOUPS]), monkeypatch, query="gruyere|emmental", regex=True, fields=["ingredients"])
    assert text.startswith("Found 1 recipes")


def test_a_title_typed_without_accents_reads_the_recipe(monkeypatch):
    text = read(RecipeRemote([ONION]), monkeypatch, title="creme brulee", fields=["ingredients"])
    assert text == "# Crème Brûlée\n\n**UID:** C3-0C\n\n## Ingredients\n1 cup shredded Gruyère"
