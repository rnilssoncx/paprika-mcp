"""A member's pattern cannot stall the server (moneypenny plans/012 § 4).

Each test runs under pytest-timeout (pyproject), so a regression to `re` fails
rather than hanging the suite: `re` checks for signals while it backtracks,
which is what lets the timeout land. The timed-out words are asserted exactly,
because the absence rule is in their wording — "No recipes found" or "No
changes made" after a match that never finished would be a confident no.
"""

import threading
import time

import pytest
import regex
from fakes import RecipeRemote, category
from paprika_recipes.remote import RemoteRecipe

from paprika_mcp import utils
from paprika_mcp.tools import TOOLS
from paprika_mcp.tools import search_recipes as sr
from paprika_mcp.tools import update_recipe as ur

SLOW = "(a|aa)+$"  # measured: `re` still running at 5 s on SLOW_TEXT
SLOW_TEXT = "a" * 45 + "b"
MEDIUM_TEXT = "a" * 18 + "b"  # measured: ~2 ms a match under `regex`
SOUPS = category("C-SOU", "Soups")

SEARCH_WORDS = (
    "That pattern took too long to search — nothing is wrong with the recipes. "
    "Try a simpler pattern, or search for a plain word."
)


def edit_words(title):
    return f'That pattern took too long to match in "{title}" — nothing was changed. Try a simpler pattern, or a plain word.'


def recipe(uid="A1-0A", name="Slow", **fields):
    base = {"uid": uid, "name": name, "ingredients": "", "directions": "", "notes": "", "categories": ["C-SOU"]}
    return RemoteRecipe(**{**base, **fields})


def search(remote, monkeypatch, **args):
    monkeypatch.setattr(sr, "get_remote", lambda: remote)
    return TOOLS["search_recipes"]["handler"](args)[0].text


def update(remote, monkeypatch, **args):
    monkeypatch.setattr(ur, "get_remote", lambda: remote)
    return TOOLS["update_recipe"]["handler"]({"id": "A1-0A", **args})[0].text


# ---- search ----


def test_a_slow_search_answers_in_the_search_words_within_the_deadline(monkeypatch):
    # At the real deadline, so the default itself is pinned below 3 s.
    remote = RecipeRemote([recipe(notes=SLOW_TEXT)], [SOUPS])
    started = time.monotonic()
    text = search(remote, monkeypatch, query=SLOW, regex=True)
    assert text == SEARCH_WORDS
    assert time.monotonic() - started < 3


def test_matches_found_before_the_deadline_are_not_reported_as_the_answer(monkeypatch):
    # "Aardvark" sorts first and matches at once; the slow field comes after.
    # Reporting what was found so far would be "Found 1 recipes" with the rest
    # never searched — a partial list read as a complete one.
    monkeypatch.setattr(utils, "MATCH_SECONDS", 0.2)
    remote = RecipeRemote([recipe(uid="A0-0A", name="Aardvark", notes="aa"), recipe(notes=SLOW_TEXT)], [SOUPS])
    assert search(remote, monkeypatch, query=SLOW, regex=True) == SEARCH_WORDS


def test_a_plain_search_that_times_out_has_its_own_words(monkeypatch):
    # "Try a simpler pattern" is no advice for a plain word. The deadline is
    # set to nothing so the first match is already late.
    monkeypatch.setattr(utils, "MATCH_SECONDS", 0.0)
    remote = RecipeRemote([recipe(notes="4 tomatoes")], [SOUPS])
    assert search(remote, monkeypatch, query="tomato") == (
        "That search took too long to finish — nothing is wrong with the recipes. Try again in a moment."
    )


def test_an_overlong_plain_search_is_refused_before_the_fetch(monkeypatch):
    monkeypatch.setattr(sr, "get_remote", lambda: pytest.fail("fetched before the cap"))
    assert TOOLS["search_recipes"]["handler"]({"query": "a" * 1001})[0].text == "Error: Search text is 1001 characters; the limit is 1000."


def test_the_deadline_is_for_the_whole_call_not_each_match(monkeypatch):
    # Each match must be slow but finish on its own — about 2 ms, under any
    # per-match limit and under a 10 ms floor — or the first one times out and
    # ends the search before either wrong implementation shows. 1,000 fields,
    # two matches each (raw and folded), is ~4 s of matching; only one deadline
    # for the whole call stops it near 0.2 s.
    monkeypatch.setattr(utils, "MATCH_SECONDS", 0.2)
    remote = RecipeRemote([recipe(uid=f"A{i}-0A", name=f"Slow {i}", notes=MEDIUM_TEXT) for i in range(1000)], [SOUPS])
    started = time.monotonic()
    assert search(remote, monkeypatch, query=SLOW, regex=True) == SEARCH_WORDS
    assert time.monotonic() - started < 1.0


def test_the_deadline_starts_at_the_first_match_not_before_the_fetch(monkeypatch):
    # The fetch is slower than the whole deadline. A clock started at compile
    # would be spent before the first match and time out an ordinary search.
    monkeypatch.setattr(utils, "MATCH_SECONDS", 0.2)

    class SlowFetch(RecipeRemote):
        @property
        def recipes(self):
            time.sleep(0.3)
            yield from super().recipes

        def _request(self, method, path, **kwargs):
            # The category list is fetched after the recipes and before the
            # first match; the deadline must not start there either.
            time.sleep(0.3)
            return super()._request(method, path, **kwargs)

    remote = SlowFetch([recipe(notes="4 tomatoes")], [SOUPS])
    assert search(remote, monkeypatch, query="tomato(es)?", regex=True).startswith("Found 1 recipes")


def test_a_slow_match_does_not_hold_the_other_threads(monkeypatch):
    # The server runs tool handlers on worker threads; a match holding the GIL
    # stalls every one of them. Measured: ~36 ticks beside a `regex` match,
    # 1 beside one run with `concurrent=False` (or under `re`).
    monkeypatch.setattr(utils, "MATCH_SECONDS", 1.0)
    pattern = utils.compile_pattern(SLOW)
    ticks = []
    ticking = threading.Event()
    # Fixed here, not in the thread: a thread that only got going once the
    # match let go would tick its whole half second afterwards and pass.
    until = time.monotonic() + 0.5

    def tick():
        while time.monotonic() < until:
            ticks.append(1)
            ticking.set()
            time.sleep(0.01)

    other = threading.Thread(target=tick)
    other.start()
    ticking.wait()
    with pytest.raises(utils.PatternTimeout):
        pattern.search(SLOW_TEXT)
    other.join()
    # 1 when the GIL is held; ~36 when it is not. 10 leaves a slow runner room.
    assert len(ticks) >= 10


@pytest.mark.parametrize(
    "query, regex, text",
    [("gruyere", False, "Gruyère"), ("gruyere", True, "Gruyère"), ("gruyère", False, "Gruyere"), ("gruyère", False, "Gruyère"), ("gruyère", True, "Gruyère")],
)
def test_accent_folding_holds_on_both_paths(monkeypatch, query, regex, text):
    # ("gruyère", plain, "Gruyere") needs the plain query folded: the text has
    # no accent. ("gruyère", regex, "Gruyère") needs the RAW line searched: a
    # pattern is not folded, and the folded line has lost its accent.
    remote = RecipeRemote([recipe(ingredients=f"1 cup {text}")], [SOUPS])
    assert search(remote, monkeypatch, query=query, regex=regex).startswith("Found 1 recipes")


@pytest.mark.parametrize("text, found", [("Wait… then stir", True), ("abc", False)])
def test_a_plain_query_is_folded_before_it_is_escaped(monkeypatch, text, found):
    # NFKD folds "…" to "...". Escaped first, the dots would be folded back in
    # unescaped — three wildcards, matching "abc".
    remote = RecipeRemote([recipe(directions=text)], [SOUPS])
    assert search(remote, monkeypatch, query="…").startswith("Found 1 recipes") is found


def test_a_plain_search_cannot_be_read_as_a_pattern(monkeypatch):
    # Escaped: "1/2 cup (" is text, not an unterminated group.
    remote = RecipeRemote([recipe(ingredients="1/2 cup (packed) basil")], [SOUPS])
    assert search(remote, monkeypatch, query="1/2 cup (").startswith("Found 1 recipes")


# ---- update_recipe ----


@pytest.mark.parametrize("field", ["notes", "categories"])
def test_a_slow_edit_answers_in_the_edit_words_and_uploads_nothing(monkeypatch, field):
    monkeypatch.setattr(utils, "MATCH_SECONDS", 0.2)
    # Only the field under test is slow, so a branch matching the wrong text
    # answers quickly instead of timing out for the right reason.
    slow_category = category("C-SLO", SLOW_TEXT)
    notes = SLOW_TEXT if field == "notes" else "Salt well"
    remote = RecipeRemote([recipe(notes=notes, categories=["C-SLO" if field == "categories" else "C-SOU"])], [slow_category, SOUPS])
    text = update(remote, monkeypatch, field=field, find=SLOW, replace="x", regex=True)
    assert text == edit_words("Slow")
    assert remote.uploaded == [] and remote.notified == 0


def test_neither_timed_out_answer_reads_as_nothing_found():
    for words in (utils.SEARCH_TIMEOUT, utils.EDIT_TIMEOUT.format(title="Slow")):
        assert "No recipes found" not in words and "No changes made" not in words
    assert utils.SEARCH_TIMEOUT == SEARCH_WORDS
    assert utils.EDIT_TIMEOUT.format(title="Slow") == edit_words("Slow")


def test_an_overlong_find_is_refused_on_an_edit_too(monkeypatch):
    monkeypatch.setattr(ur, "get_remote", lambda: pytest.fail("fetched before the cap"))
    text = TOOLS["update_recipe"]["handler"]({"id": "A1-0A", "field": "notes", "find": "a" * 201, "replace": "x", "regex": True})[0].text
    assert text == "Error: Regex pattern is 201 characters; the limit is 200."


def test_the_same_long_find_is_fine_without_regex(monkeypatch):
    # Plain find/replace is str.replace, which cannot backtrack.
    remote = RecipeRemote([recipe(notes="Salt well")], [SOUPS])
    text = update(remote, monkeypatch, field="notes", find="a" * 201, replace="x")
    assert text == f"No changes made - pattern '{'a' * 201}' not found in field 'notes' of recipe 'Slow'"


def test_an_edit_still_matches_case_sensitively(monkeypatch):
    # Search ignores case; an edit replaces what it finds, so it never did.
    remote = RecipeRemote([recipe(notes="Salt well")], [SOUPS])
    text = update(remote, monkeypatch, field="notes", find="salt", replace="Pepper", regex=True)
    assert text == "No changes made - pattern 'salt' not found in field 'notes' of recipe 'Slow'"
    assert remote.uploaded == []


def test_an_invalid_find_on_an_edit_is_reported_as_invalid(monkeypatch):
    remote = RecipeRemote([recipe(notes="Salt well")], [SOUPS])
    text = update(remote, monkeypatch, field="notes", find="(", replace="x", regex=True)
    assert text == "Error: Invalid regex pattern '(': missing ) at position 1"


def test_a_plain_edit_still_matches_case_sensitively(monkeypatch):
    remote = RecipeRemote([recipe(notes="Salt well")], [SOUPS])
    text = update(remote, monkeypatch, field="notes", find="salt", replace="Pepper")
    assert text == "No changes made - pattern 'salt' not found in field 'notes' of recipe 'Slow'"
    assert remote.uploaded == []


@pytest.mark.parametrize(
    "replace, error",
    [(r"\2 limes", "invalid group reference"), (r"\g<qty> limes", "unknown group")],
)
def test_a_bad_replacement_is_reported_as_the_replacement(monkeypatch, replace, error):
    # The second is an IndexError in `regex`, not its error class.
    remote = RecipeRemote([recipe(notes="2 lemons")], [SOUPS])
    text = update(remote, monkeypatch, field="notes", find=r"(\d) lemons", replace=replace, regex=True)
    assert text == f"Error: Invalid replacement '{replace}': {error}. No changes were saved."
    assert remote.uploaded == []


def test_an_edit_that_would_balloon_a_field_is_refused(monkeypatch):
    # 4,000 characters times a replacement of 20 copies of the whole field.
    remote = RecipeRemote([recipe(directions="x" * 4000)], [SOUPS])
    text = update(remote, monkeypatch, field="directions", find="(?s)(.+)", replace=r"\1" * 20, regex=True)
    assert text == (
        "Error: That edit would make field 'directions' of recipe 'Slow' 80000 characters long; "
        "an edit may write at most 64000. No changes were saved."
    )
    assert remote.uploaded == []


def test_a_field_already_over_the_limit_can_still_be_edited(monkeypatch):
    remote = RecipeRemote([recipe(directions="Stir. " + "x" * 70000)], [SOUPS])
    update(remote, monkeypatch, field="directions", find="Stir.", replace="Mix.")
    assert [u["directions"][:4] for u in remote.uploaded] == ["Mix."]


def test_a_category_edit_that_would_balloon_is_refused(monkeypatch):
    # Every name is a real category, so only the size check refuses it: the
    # unknown-category refusal downstream cannot stand in for it.
    remote = RecipeRemote([recipe(categories=["C-SOU"])], [SOUPS])
    text = update(remote, monkeypatch, field="categories", find="(?s).+", replace="Soups\n" * 11000, regex=True)
    assert text.startswith("Error: That edit would make field 'categories' of recipe 'Slow' 66000 characters long")
    assert remote.uploaded == []


@pytest.mark.parametrize(
    "find, error",
    [("(?:x|(?R))+$", "unknown extension ?R at position 6"), ("((?:a|(?1))*)+$", "unknown extension ?1 at position 7"), ("(?P<q>a)(?&q)", "unknown extension ?& at position 9")],
)
def test_a_group_call_is_refused_before_it_runs(monkeypatch, find, error):
    # Measured: these grow memory, not time — 289 MB and 420 MB inside the
    # deadline. `re` refuses them, and it is the syntax gate.
    monkeypatch.setattr(sr, "get_remote", lambda: pytest.fail("fetched before the gate"))
    text = TOOLS["search_recipes"]["handler"]({"query": find, "regex": True})[0].text
    assert text == f"Error: Invalid regex pattern '{find}': {error}"


@pytest.mark.parametrize("query", ["cherr(y|ies)", "bananas?", "(?s).+", r"\bsalt\b", "(beef|chicken|pork)", "salt{e<=1}"])
def test_household_patterns_pass_the_gate(query):
    utils.compile_pattern(query)


# ---- UserPattern itself: every operation is timed ----


@pytest.mark.parametrize("operation", ["search", "finditer", "sub"])
def test_each_operation_runs_under_the_deadline(operation):
    # Called directly, because through update_recipe `finditer` always runs
    # (and times out) first, so a `sub` that skipped the timeout would pass.
    pattern = utils.UserPattern(regex.compile(SLOW), 0.2)
    run = {"search": lambda: pattern.search(SLOW_TEXT), "finditer": lambda: pattern.finditer(SLOW_TEXT), "sub": lambda: pattern.sub("x", SLOW_TEXT)}
    started = time.monotonic()
    with pytest.raises(utils.PatternTimeout):
        run[operation]()
    assert time.monotonic() - started < 1.0


def test_an_operation_after_the_deadline_does_not_run():
    class Refuses:
        def search(self, *args, **kwargs):
            raise AssertionError("matched after the deadline")

    pattern = utils.UserPattern(Refuses(), 0.2)  # type: ignore[arg-type]
    pattern._deadline = time.monotonic() - 1
    with pytest.raises(utils.PatternTimeout):
        pattern.search("a")
