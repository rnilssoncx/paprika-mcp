"""The limit guard's tests (068): no new hard-coded limit, and the lists say what is left.

**Everything outside the marked block is identical in every repository.** The block holds what
differs; the detector, `limitguard.py`, is byte-identical and takes it as a `Config`.

- `test_no_new_limit`, `test_list_matches` and `test_tags_valid_and_registered` read this
  repository. The list (`limitguard-todo.txt`) is the limits not yet converted; it can only shrink,
  and a hand-found value stays on it as an anchor until it is converted.
- Everything below them builds a small git repository of its own and shows each shape the detector
  must catch and each way out the guard must refuse. The ten `test_mutation_*` tests are the
  proof (068 § Proof that it works): each is the wrong change and the guard failing on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

try:
    import limitguard as lg
except ModuleNotFoundError:  # `tests/` is a package here
    from tests import limitguard as lg

# ---- per repository: the only lines that differ ---------------------------------------------
HERE = Path(__file__).resolve().parent
CONFIG = lg.Config(
    root=HERE.parent,
    guard_dir=HERE,
    skip=(),
    text_roots=(),
    reply_bound=frozenset(),
)
# ---- end ------------------------------------------------------------------------------------


def fail_text(problems: list[str], how_out: str = "") -> str:
    return "\n" + "\n".join(problems) + ("\n" + how_out if how_out else "")


def test_no_new_limit() -> None:
    assert not (problems := lg.check_new(CONFIG)), fail_text(problems, lg.HOW_OUT)


def test_list_matches() -> None:
    assert not (problems := lg.check_list(CONFIG)), fail_text(problems)


def test_tags_valid_and_registered() -> None:
    assert not (problems := lg.check_tags(CONFIG)), fail_text(problems)


# -- what the detector sees ----------------------------------------------------------------------

FOUND = [
    ("NEW_TIMEOUT = 30", ["30"]),
    ("QUOTE_MIN, QUOTE_MAX = 20, 200", ["20", "200"]),
    ("SIZE = 64 << 20", ["64<<20"]),
    ("CODES = {404, 410}", ["{404,410}"]),
    ("ONE_DAY = 90 * UNIT", ["90*UNIT"]),
    ("deadline = time.monotonic() + 30", ["time.monotonic()+30"]),
    ("backoff = min(backoff * 2, 60.0)", ["min(backoff*2,60.0)"]),
    ("limit = max(1, min(limit, 200))", ["max(1,min(limit,200))"]),
    ("c = Client(timeout=httpx.Timeout(20.0))", ["httpx.Timeout(20.0)"]),
    ("c.get(url, timeout=(5, 30))", ["(5,30)"]),
    ("def f(page_size=20): ...", ["20"]),
    ("def f(*, retries: int = 3): ...", ["3"]),
    ("x = args.get('page_size', 25)", ["25"]),
    ("page_size = args.get('page_size', 25)", ["args.get('page_size',25)"]),
    ("if attempt < 3: pass", ["3"]),
    ("if self.depth >= 32: pass", ["32"]),
    ("if len(x) > 200: pass", ["200"]),
    ("if len(x) > LIMIT_FIELD * 4: pass", ["LIMIT_FIELD*4"]),
    ("async def f():\n    async with asyncio.timeout(30): pass", ["30"]),
    ("def f():\n    with anyio.fail_after(30): pass", ["30"]),
    ("resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))", ["(64,64)"]),
    ("time.sleep(0.5)", ["0.5"]),
    ("sock.recv(65536)", ["65536"]),
    ("for _ in range(5): pass", ["5"]),
    ("x = text[:200]", ["200"]),
    ("x = items[-20:]", ["-20"]),
    ("class C:\n    MAX_ROWS = 10", ["10"]),
]
NOT_FOUND = [
    "x = 0",
    "n = 5",
    "port = 8080",
    "def f(port=8080): ...",
    "def f(max_workers=1): ...",
    "if n < 3: pass",
    "y = items[3]",
    "z = text[:5]",
    "REFUSAL = refusal('x', 404)",
    "NAME = 'a string'",
    "def f():\n    LOCAL = 5",
    "MARKED = _limit('marked', 'Messages marked', 'member', 'hour', 10, 200)",
    "MARKED = Limit('n', 'w', 'count', 'member', 'hour', {'-': 10}, {'-': 200})",
    "x = f'{timeout}'",
]


def values(source: str) -> list[str]:
    hits, _, problems = lg.python_hits("m.py", source)
    assert not problems
    return sorted(h.value for h in hits)


@pytest.mark.parametrize(("source", "expected"), FOUND)
def test_detector_sees(source: str, expected: list[str]) -> None:
    assert values(source) == sorted(expected)


@pytest.mark.parametrize("source", NOT_FOUND)
def test_detector_leaves_alone(source: str) -> None:
    assert values(source) == []


def test_a_file_that_does_not_parse_is_a_problem_not_a_skip() -> None:
    _, _, problems = lg.python_hits("m.py", "def (:\n")
    assert problems
    assert "does not parse" in problems[0]
    assert "not skipped" in problems[0]


def test_text_detector_sees_the_javascript_and_configuration_shapes() -> None:
    js = "const MAX_BYTES = 90 * 1024 * 1024;\nfetch(u, { timeout: 15000 });\nx.slice(0, 200);\n"
    hits, problems = lg.text_hits("a.js", js, js=True)
    assert not problems
    assert sorted(h.value for h in hits) == ["15000", "200", "90*1024*1024;"]
    conf = "mem_limit: 384m\nsleep 30\nsize=8m\nurl: http://x/#frag # 40\nretries: 1\n"
    hits, _ = lg.text_hits("c.yml", conf, js=False)
    assert sorted(h.value for h in hits) == ["30", "384m", "8m"]
    quoted = "const U = 'http://x//y'; const NAME_MAX = 40; // limit-ok: format — a fine reason\n"
    hits, _ = lg.text_hits("a.js", quoted, js=True)
    assert [h.tag.kind for h in hits if h.tag] == ["format"]


# -- what a tag covers ------------------------------------------------------------


def tagged(source: str) -> list[tuple[str, str | None]]:
    hits, _, problems = lg.python_hits("m.py", source)
    assert not problems
    return [(h.value, h.tag.kind if h.tag else None) for h in hits]


REASON = "limit-ok: format — a fact of the format"


def test_a_trailing_tag_covers_its_own_line_only() -> None:
    assert tagged(f"A = 998  # {REASON}\nB = 30\n") == [("998", "format"), ("30", None)]


def test_a_comment_only_line_above_covers_the_next_line_only() -> None:
    assert tagged(f"# {REASON}\nA = 998\nB = 30\n") == [("998", "format"), ("30", None)]


def test_a_default_argument_is_tagged_on_its_own_line_not_the_function() -> None:
    source = f"def f(\n    a_timeout=5,  # {REASON}\n    b_timeout=6,\n):\n    return 1\n"
    assert tagged(source) == [("5", "format"), ("6", None)]


def test_a_tag_inside_a_string_is_no_tag() -> None:
    assert tagged(f'A = 30\nB = "# {REASON}"\n') == [("30", None)]


def test_a_tag_with_nothing_to_cover_is_a_problem() -> None:
    _, _, problems = lg.python_hits("m.py", f"x = foo()  # {REASON}\n")
    assert "no limit on its line" in problems[0]


# -- a small repository of its own -----------------------------------------------------------------

BASE = f"""\
import asyncio
import time

TAGGED = 998  # {REASON}
LISTED = 30


def fetch(client):
    return client.get("u", timeout=30)
"""
TODO = "pkg/a.py | <module> | LISTED = | 30\npkg/a.py | fetch | get(timeout=) | 30\n"
TAGS = "pkg/a.py | <module> | TAGGED = | 998 | format | a fact of the format\n"


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        lg.run_git(root, "init", "-q")
        self.cfg = lg.Config(root=root, guard_dir=root / "tests", text_roots=("web/",),
                             reply_bound=frozenset({"pkg/reader.py"}))
        self.write("pkg/a.py", BASE)
        self.write("tests/limitguard-todo.txt", TODO)
        self.write("tests/limitguard-tags.txt", TAGS)

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def append(self, rel: str, text: str) -> None:
        self.write(rel, (self.root / rel).read_text(encoding="utf-8") + text)

    def replace(self, rel: str, old: str, new: str) -> None:
        text = (self.root / rel).read_text(encoding="utf-8")
        assert old in text
        self.write(rel, text.replace(old, new))

    def problems(self) -> dict[str, list[str]]:
        return {"new": lg.check_new(self.cfg), "list": lg.check_list(self.cfg),
                "tags": lg.check_tags(self.cfg)}


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def only(found: dict[str, list[str]], check: str) -> list[str]:
    """What one check says, after proving that the other two are quiet."""
    for name, problems in found.items():
        assert bool(problems) == (name == check), f"{name}: {problems}"
    return found[check]


def test_the_base_repository_is_quiet(repo: Repo) -> None:
    assert repo.problems() == {"new": [], "list": [], "tags": []}


def test_mutation_01_a_new_constant(repo: Repo) -> None:
    repo.append("pkg/a.py", "NEW_TIMEOUT = 30\n")
    [message] = only(repo.problems(), "new")
    assert "pkg/a.py:" in message
    assert "NEW_TIMEOUT = 30" in message
    assert "Declare it as a Limit" in lg.HOW_OUT
    assert "limitguard-tags.txt" in lg.HOW_OUT


def test_mutation_02_an_untagged_constant_directly_under_a_tagged_one(repo: Repo) -> None:
    tagged_line = f"TAGGED = 998  # {REASON}\n"
    repo.replace("pkg/a.py", tagged_line, tagged_line + "UNDER = 45\n")
    [message] = only(repo.problems(), "new")
    assert "UNDER = 45" in message


def test_mutation_03_a_second_identical_value_in_a_listed_function(repo: Repo) -> None:
    repo.replace("pkg/a.py", '    return client.get("u", timeout=30)\n',
                 '    client.get("v", timeout=30)\n    return client.get("u", timeout=30)\n')
    [message] = only(repo.problems(), "new")
    assert "get(timeout=) 30" in message


def test_mutation_04_a_new_untracked_file(repo: Repo) -> None:
    repo.write("pkg/b.py", "class B:\n    READ_TIMEOUT = 5\n")
    [message] = only(repo.problems(), "new")
    assert "pkg/b.py:2 (B) READ_TIMEOUT = 5" in message


def test_mutation_05_a_tag_with_a_one_word_reason(repo: Repo) -> None:
    repo.replace("pkg/a.py", "a fact of the format", "fact")
    found = repo.problems()
    assert any("under three words" in m for m in found["tags"])
    assert any("TAGGED = 998" in m for m in found["new"])  # an invalid tag exempts nothing


def test_mutation_06_a_tag_of_an_unknown_kind(repo: Repo) -> None:
    repo.replace("pkg/a.py", "limit-ok: format", "limit-ok: whatever")
    found = repo.problems()
    assert any("`whatever` is not one of maximum, reply-bound, format, not-a-limit" in m
               for m in found["tags"])


def test_mutation_07_a_tag_missing_from_the_tags_file(repo: Repo) -> None:
    repo.write("tests/limitguard-tags.txt", "")
    [message] = only(repo.problems(), "tags")
    assert "tag not in limitguard-tags.txt" in message
    assert "pkg/a.py | <module> | TAGGED = | 998 | format" in message


def test_mutation_08_asyncio_timeout_with_a_literal(repo: Repo) -> None:
    repo.append("pkg/a.py", "\n\nasync def g():\n    async with asyncio.timeout(30):\n"
                "        pass\n")
    [message] = only(repo.problems(), "new")
    assert "timeout(arg 0) 30" in message


def test_mutation_09_a_deadline_computed_from_a_literal(repo: Repo) -> None:
    repo.append("pkg/a.py", "\n\ndef h():\n    deadline = time.monotonic() + 30\n"
                "    return deadline\n")
    [message] = only(repo.problems(), "new")
    assert "deadline = time.monotonic()+30" in message


def test_mutation_10_a_listed_value_that_was_converted(repo: Repo) -> None:
    repo.replace("pkg/a.py", "LISTED = 30\n", "")
    [message] = only(repo.problems(), "list")
    assert "stale line" in message
    assert "pkg/a.py | <module> | LISTED = | 30" in message
    assert "delete the line" in message


# -- the escapes are bounded -----------------------------------------------------------------------


def test_maximum_counts_only_on_a_name_a_limit_takes_its_maxima_from(repo: Repo) -> None:
    repo.write("pkg/b.py", "DAY_MAX = 2000  # limit-ok: maximum — the day pages' maximum\n"
                           "OTHER_MAX = 5  # limit-ok: maximum — another fine reason\n"
                           "L = _limit('n', 'w', 'member', 'hour', 10, DAY_MAX)\n")
    repo.append("tests/limitguard-tags.txt", "pkg/b.py | <module> | DAY_MAX = | 2000 | maximum | "
                "the day pages' maximum\n")
    found = repo.problems()
    assert any("OTHER_MAX = 5" in m and "`Limit(...)`'s maxima" in m for m in found["tags"])
    assert not any("DAY_MAX" in m for m in found["tags"])


def test_reply_bound_counts_only_in_the_modules_that_read_q_branch(repo: Repo) -> None:
    line = "ANSWER_LIMIT = 64 * 1024  # limit-ok: reply-bound — Q Branch's answer, read first\n"
    repo.write("pkg/reader.py", line)
    repo.write("pkg/other.py", line)
    repo.append("tests/limitguard-tags.txt",
                "pkg/reader.py | <module> | ANSWER_LIMIT = | 64*1024 | reply-bound | "
                "Q Branch's answer, read first\n")
    found = repo.problems()
    assert any("pkg/other.py" in m and "reads Q Branch" in m for m in found["tags"])
    assert not any("pkg/reader.py" in m for m in found["tags"] + found["new"])


def test_not_a_limit_is_refused_on_a_strong_limit_word(repo: Repo) -> None:
    repo.write("pkg/b.py", "CONNECT_TIMEOUT = 5  # limit-ok: not-a-limit — it is only a label\n")
    found = repo.problems()
    assert any("`not-a-limit` is refused on a name carrying `timeout`" in m for m in found["tags"])
    assert any("CONNECT_TIMEOUT = 5" in m for m in found["new"])


def test_a_list_or_tags_line_may_not_name_a_path_that_never_ships(repo: Repo) -> None:
    repo.append("tests/limitguard-todo.txt", "deploy/keys/keys.py | <module> | A = | 5\n")
    repo.append("tests/limitguard-tags.txt",
                "plans/x.py | <module> | A = | 5 | format | long enough reason\n")
    found = repo.problems()
    assert any("deploy/keys/keys.py" in m and "never ships" in m for m in found["list"])
    assert any("plans/x.py" in m and "never ships" in m for m in found["tags"])


def test_a_hand_found_anchor_must_stay_in_its_file(repo: Repo) -> None:
    repo.write("pkg/c.py", "def go(attempt):\n    return attempt\n")
    repo.append("tests/limitguard-todo.txt", "pkg/c.py | return attempt\n")
    assert repo.problems() == {"new": [], "list": [], "tags": []}
    repo.write("pkg/c.py", "def go():\n    return 1\n")
    [message] = only(repo.problems(), "list")
    assert "stale anchor" in message
    assert "pkg/c.py | return attempt" in message


def test_the_skipped_paths_are_not_read(repo: Repo) -> None:
    for rel in ("tests/t.py", "spikes/s.py", "evals/e.py", "plans/p.py", "deploy/d.py"):
        repo.write(rel, "X_TIMEOUT = 5\n")
    assert repo.problems() == {"new": [], "list": [], "tags": []}


def test_a_gitignored_file_is_not_read_and_an_untracked_one_is(repo: Repo) -> None:
    repo.write(".gitignore", "ignored.py\n")
    repo.write("ignored.py", "X_TIMEOUT = 5\n")
    assert repo.problems() == {"new": [], "list": [], "tags": []}
    repo.write("fresh.py", "X_TIMEOUT = 5\n")
    [message] = only(repo.problems(), "new")
    assert "fresh.py" in message


def test_an_extensionless_script_is_read_and_other_files_only_under_text_roots(repo: Repo) -> None:
    repo.write("bin/job", "#!/usr/bin/env python3\nRUN_SECONDS = 9\n")
    repo.write("web/app.js", "const POLL_MS = 500;\n")
    repo.write("other/app.js", "const POLL_MS = 500;\n")
    found = " ".join(lg.check_new(repo.cfg))
    assert "bin/job:2" in found
    assert "web/app.js:1" in found
    assert "other/app.js" not in found


def test_summary_line_counts_the_list_and_the_tags(repo: Repo) -> None:
    repo.append("tests/limitguard-todo.txt", "pkg/a.py | an anchor\n")
    assert lg.summary_line(repo.cfg.guard_dir) == (
        "limits not yet converted: 3 (hits 2, hand-found 1); "
        "tags: format 1, maximum 0, not-a-limit 0, reply-bound 0"
    )


if __name__ == "__main__":
    sys.exit(lg.cli(CONFIG, sys.argv[1:]))
