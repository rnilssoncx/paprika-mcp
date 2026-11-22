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


def test_limits_read() -> None:
    """080: every declared limit is read, and read where it is used (`lg.read_problems`)."""
    assert not (problems := lg.read_problems(CONFIG)), fail_text(problems)


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
    ("page_size = args.get('page_size', 25)", ['args.get("…",25)']),
    ("c.post(u, json={'max_results': 100}, params={'limit': 50})", ["100", "50"]),
    ("opts = {'net': {'timeout': 30}}", ["30"]),
    ("opts = {'retry': {'timeout': 30}}", ['{"…":30}']),
    ("def f():\n    return {'ttl': 3 * 60}", ["3*60"]),
    ("token_ttl = issue('alice@home.example', 30)", ['issue("…",30)']),
    ("x = {'page_size': g(f'{who} 20', b'raw', 20)}", ['g("…","…",20)']),
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
    "x = {'max_results': 1}",
    "x = {'name': 30}",
    "x = {'timeout': '30s'}",
    "x = {'timeout': None, 'limit': n}",
]

# every call in `lg.CALL_ARGS` and every position of it, written out so that deleting an entry or
# an index from the detector fails here rather than being read back from the same table
CALLS = {
    "sleep": (0,), "wait_for": (1,), "timeout": (0,), "fail_after": (0,), "move_on_after": (0,),
    "Semaphore": (0,), "BoundedSemaphore": (0,), "ThreadPoolExecutor": (0,),
    "ProcessPoolExecutor": (0,), "islice": (1, 2), "timedelta": (0, 1, 2, 3, 4, 5, 6),
    "Timeout": (0, 1, 2, 3), "setrlimit": (1,), "alarm": (0,), "settimeout": (0,),
    "lru_cache": (0,), "deque": (1,), "Queue": (0,), "read": (0,), "read1": (0,), "recv": (0,),
    "readline": (0,), "fetchmany": (0,), "join": (0,), "wait": (0,), "call_later": (0,),
    "acquire": (0, 1), "result": (0,), "Timer": (0,), "communicate": (1,),
}  # fmt: skip


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


def test_every_call_and_position_is_read() -> None:
    assert {k: tuple(v) for k, v in lg.CALL_ARGS.items()} == CALLS
    for name, indexes in CALLS.items():
        for index in indexes:
            args = ", ".join("77" if i == index else "a" for i in range(index + 1))
            assert values(f"x.{name}({args})") == ["77"], (name, index)
            assert values(f"{name}({args})") == ["77"], (name, index)


def test_a_non_limit_argument_of_those_calls_is_left_alone() -> None:
    assert values("x.wait(timeout_s)\nx.acquire(True)\nx.result()\nx.communicate(data)") == []


def test_a_file_that_does_not_parse_is_a_problem_not_a_skip() -> None:
    _, _, problems = lg.python_hits("m.py", "def (:\n")
    assert problems
    assert "does not parse" in problems[0]
    assert "not skipped" in problems[0]


JS_SHAPES = [
    ("const", "const MAX_BYTES = 90 * 1024;", ["90*1024;"]),
    ("timer", "setTimeout(go, 3000);", ["3000"]),
    ("timer", "setInterval(() => tick(), 500)", ["500"]),
    ("prop", "x = { pollMs: 250, name: 9 };", ["250"]),
    ("default", "function f(a, retryCount = 4) {}", ["4"]),
    ("slice", "x.slice(0, 200);", ["200"]),
    ("slice", "x.substring(0, 50);", ["50"]),
    ("length", "if (xs.length > 50) {}", ["50"]),
    ("lname", "let retries = 3;", ["3"]),
    ("quoted", "const U_MAX = 'http://x/y' + 30;", ['"…"+30;']),
]
CONF_SHAPES = [
    ("key", "mem_limit: 384m", ["384m"]),
    ("key", "- RETRY_MAX=5", ["5"]),
    ("opt", "tmpfs: /run:size=8m", ["8m"]),
    ("opt", "tmpfs: /run:nr_inodes=100", ["100"]),
    *[("cmd", f"run {cmd} 77 now", ["77"]) for cmd in (
        "sleep", "timeout", "--max-time", "--connect-timeout", "--interval", "--start-period",
        "--retries", "-m", "--retry", "limit rate", "burst")],
]
NOT_TEXT = [
    (True, "const name = 'a1';\nx = { port: 8080 };\nx.slice(0, 5);\nlet other = 3;"),
    (False, "mode=1777\nport: 8080\nsleeping 30\nretries: 1"),
]


@pytest.mark.parametrize(("kind", "source", "expected"), JS_SHAPES)
def test_each_javascript_shape(kind: str, source: str, expected: list[str]) -> None:
    hits, problems = lg.text_hits("a.js", source + "\n", js=True)
    assert not problems
    assert sorted(h.value for h in hits) == expected, kind
    assert hits[0].what.startswith(kind.replace("quoted", "const")), hits[0].what


@pytest.mark.parametrize(("kind", "source", "expected"), CONF_SHAPES)
def test_each_configuration_shape(kind: str, source: str, expected: list[str]) -> None:
    hits, problems = lg.text_hits("c.yml", source + "\n", js=False)
    assert not problems
    assert sorted(h.value for h in hits) == expected, kind
    assert hits[0].what.startswith(kind), hits[0].what


@pytest.mark.parametrize(("js", "source"), NOT_TEXT)
def test_text_shapes_left_alone(js: bool, source: str) -> None:
    hits, _ = lg.text_hits("a.js" if js else "c.yml", source + "\n", js=js)
    assert hits == []


def test_a_text_tag_covers_the_line_below_it_and_no_more() -> None:
    js = f"// {REASON}\nconst A_MAX = 5;\nconst B_MAX = 6;\n"
    hits, problems = lg.text_hits("a.js", js, js=True)
    assert not problems
    assert [(h.value, h.tag.kind if h.tag else None) for h in hits] == [
        ("5;", "format"), ("6;", None)]
    conf = f"# {REASON}\nmem_limit: 384m\nsleep 30\n"
    hits, _ = lg.text_hits("c.yml", conf, js=False)
    assert [(h.value, h.tag.kind if h.tag else None) for h in hits] == [
        ("384m", "format"), ("30", None)]


def test_a_text_tag_with_nothing_to_cover_or_malformed_is_a_problem() -> None:
    _, problems = lg.text_hits("a.js", f"foo(); // {REASON}\n", js=True)
    assert "no limit on its line" in problems[0]
    _, problems = lg.text_hits("a.js", "const A_MAX = 5; // limit-ok: nonsense\n", js=True)
    assert "does not read `limit-ok: <kind>" in problems[0]


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
    assert any("`whatever` is not one of maximum, reply-bound, format, not-a-limit, pi-guard" in m
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
    assert any("`not-a-limit` is refused on a hit whose name or description carries `timeout`"
               in m for m in found["tags"])
    assert any("CONNECT_TIMEOUT = 5" in m for m in found["new"])


def test_not_a_limit_is_refused_on_an_unnamed_call_hit_too(repo: Repo) -> None:
    repo.write("pkg/b.py", "import asyncio\n\n\nasync def g(x):\n"
               "    await asyncio.wait_for(x, 30)  # limit-ok: not-a-limit — an animation pause\n"
               "    await asyncio.sleep(2)  # limit-ok: not-a-limit — an animation pause\n")
    found = repo.problems()
    refused = [m for m in found["tags"] if "`not-a-limit` is refused" in m]
    assert len(refused) == 2
    assert any("wait_for(arg 1) 30" in m and "carries `wait`" in m for m in refused)
    assert any("sleep(arg 0) 2" in m and "carries `sleep`" in m for m in refused)
    assert sum("new limit" in m for m in found["new"]) == 2


def test_format_is_not_refused_on_a_strong_word_the_registry_is_the_control(repo: Repo) -> None:
    repo.write("pkg/b.py", "CONNECT_TIMEOUT = 5  # limit-ok: format — the vendor fixes this\n")
    repo.append("tests/limitguard-tags.txt", "pkg/b.py | <module> | CONNECT_TIMEOUT = | 5 | "
                "format | the vendor fixes this\n")
    assert repo.problems() == {"new": [], "list": [], "tags": []}


def test_a_listed_path_that_never_ships_is_stale_and_an_anchor_may_not_name_one(
    repo: Repo,
) -> None:
    repo.write("deploy/keys.py", "A_TIMEOUT = 5\n")
    repo.append("tests/limitguard-todo.txt", "deploy/keys.py | <module> | A_TIMEOUT = | 5\n"
                "plans/x.py | the anchor text\n")
    repo.append("tests/limitguard-tags.txt",
                "plans/x.py | <module> | A = | 5 | format | long enough reason\n")
    found = repo.problems()
    assert any("stale line" in m and "deploy/keys.py" in m for m in found["list"])
    assert any("`plans/x.py | the anchor" in m and "never ships" in m for m in found["list"])
    assert not any("never ships" in m for m in found["tags"])
    assert any("line with no matching tag" in m and "plans/x.py" in m for m in found["tags"])


def test_an_anchor_is_four_characters_and_has_no_separator_in_it(repo: Repo) -> None:
    repo.write("pkg/c.py", "def go(attempt):\n    return attempt | 4\n")
    repo.append("tests/limitguard-todo.txt", "pkg/c.py | att\npkg/c.py | a | b\n")
    found = repo.problems()
    assert sum("is neither `path | scope | what | value` nor `path | anchor`" in m
               for m in found["list"]) == 2
    assert not found["new"]


def test_a_symlinked_python_file_is_read(repo: Repo) -> None:
    (repo.root / "pkg/link.py").symlink_to(repo.root / "pkg/a.py")
    found = " ".join(lg.check_new(repo.cfg))
    assert "pkg/link.py:5 (<module>) LISTED = 30" in found


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


def test_a_skipped_name_is_skipped_from_the_root_only_never_by_folder_name_anywhere(
    repo: Repo,
) -> None:
    for rel in ("pkg/tests/x.py", "pkg/deploy/x.py", "pkg/spikes/x.py", "pkg/plans/x.py",
                "pkg/evals/x.py", "pkg/vendor/x.py"):
        repo.write(rel, "X_TIMEOUT = 5\n")
    cfg = lg.Config(root=repo.root, guard_dir=repo.cfg.guard_dir, skip=("vendor/",))
    found = " ".join(lg.check_new(cfg))
    for rel in ("pkg/tests/x.py", "pkg/deploy/x.py", "pkg/spikes/x.py", "pkg/plans/x.py",
                "pkg/evals/x.py", "pkg/vendor/x.py"):
        assert rel + ":1" in found, rel


def test_a_configured_skip_is_a_prefix_a_pattern_or_one_exact_file(repo: Repo) -> None:
    for rel in ("vendor/v.py", "gen/a_pb.py", "gen/b.py", "walt/loki-test.py",
                "walt/loki-test2.py"):
        repo.write(rel, "X_TIMEOUT = 5\n")
    cfg = lg.Config(root=repo.root, guard_dir=repo.cfg.guard_dir,
                    skip=("vendor/", "gen/*_pb.py", "walt/loki-test.py"))
    found = " ".join(lg.check_new(cfg))
    assert "vendor/" not in found
    assert "gen/a_pb.py" not in found
    assert "walt/loki-test.py" not in found
    assert "gen/b.py:1" in found
    assert "walt/loki-test2.py:1" in found


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
    repo.write("web/page.html", "<script>\n  const REFRESH_MS = 4000;\n  setTimeout(go, 3000);\n"
               "</script>\n")
    repo.write("other/page.html", "<script>\n  const REFRESH_MS = 4000;\n</script>\n")
    found = " ".join(lg.check_new(repo.cfg))
    assert "bin/job:2" in found
    assert "web/app.js:1" in found
    assert "other/app.js" not in found
    assert "web/page.html:2" in found
    assert "web/page.html:3" in found
    assert "other/page.html" not in found


# -- a file that cannot be read, and a tag that cannot count, are said by every check that reads it


def test_a_file_that_does_not_parse_is_said_by_the_new_and_the_tags_checks(repo: Repo) -> None:
    repo.write("pkg/bad.py", "def (:\n")
    found = repo.problems()
    assert any("pkg/bad.py: does not parse" in m for m in found["new"])
    assert any("pkg/bad.py: does not parse" in m for m in found["tags"])
    assert found["list"] == []


def test_a_file_that_is_not_utf8_is_said_by_the_new_and_the_tags_checks(repo: Repo) -> None:
    (repo.root / "pkg").mkdir(exist_ok=True)
    (repo.root / "pkg/latin.py").write_bytes(b"X = 'caf\xe9'\n")
    (repo.root / "web").mkdir()
    (repo.root / "web/latin.js").write_bytes(b"// caf\xe9\n")
    found = repo.problems()
    for check in ("new", "tags"):
        for name in ("pkg/latin.py", "web/latin.js"):
            assert any(f"{name}: is not UTF-8" in m for m in found[check]), (check, name)


def test_a_malformed_or_stray_tag_is_said_by_the_new_and_the_tags_checks(repo: Repo) -> None:
    repo.write("pkg/b.py", "X = foo()  # limit-ok: format — a fine reason here\n"
               "Y_TIMEOUT = 5  # limit-ok nonsense\n")
    found = repo.problems()
    for check in ("new", "tags"):
        assert any("pkg/b.py:1: a `limit-ok` tag with no limit" in m for m in found[check]), check
        assert any("pkg/b.py: line 2: a `limit-ok` comment that does not read" in m
                   for m in found[check]), check


def test_two_identical_tags_need_two_registry_lines(repo: Repo) -> None:
    repo.append("pkg/a.py", f"TAGGED = 998  # {REASON}\n")
    [message] = only(repo.problems(), "tags")
    assert "tag not in limitguard-tags.txt (x1)" in message
    repo.append("tests/limitguard-tags.txt", TAGS)
    assert repo.problems() == {"new": [], "list": [], "tags": []}
    repo.write("tests/limitguard-tags.txt", TAGS + TAGS + TAGS)
    [message] = only(repo.problems(), "tags")
    assert "line with no matching tag (x1)" in message


def test_an_edit_between_two_runs_is_read_not_remembered(repo: Repo) -> None:
    assert lg.check_new(repo.cfg) == []
    repo.replace("pkg/a.py", "LISTED = 30", "LISTED = 31")  # the same size, the same path
    assert [m for m in lg.check_new(repo.cfg) if "LISTED = 31" in m]
    assert any("stale line" in m and "LISTED = | 30" in m for m in lg.check_list(repo.cfg))


def test_no_string_from_source_reaches_a_list_line(repo: Repo) -> None:
    repo.write("pkg/b.py", "token_ttl = issue('alice@home.example', 30)\n"
               "PAGE = {'alice limit': 5, b'x': 9, 'max_results': 7}\n"
               "x = args.get('alice timeout', 25)\n"
               "y = f(f'{who}@home.example', 3, retries=4)\n")
    repo.write("web/b.js", "const NAME_MAX = 'alice@home.example' + 30; const A_MAX = \"x + 5;\n")
    lines = [" | ".join(h.key()) for h in lg.collect(repo.cfg).hits]
    assert len(lines) >= 6
    for line in lines:
        assert not set("\"'`") & set(line.replace(lg.PLACEHOLDER, "")), line
        assert "alice" not in line and "home.example" not in line, line
    assert any(lg.PLACEHOLDER in line for line in lines)


def test_summary_line_counts_the_list_and_the_tags(repo: Repo) -> None:
    repo.append("tests/limitguard-todo.txt", "pkg/a.py | an anchor\n")
    assert lg.summary_line(repo.cfg.guard_dir) == (
        "limits not yet converted: 3 (hits 2, hand-found 1); "
        "tags: format 1, maximum 0, not-a-limit 0, pi-guard 0, reply-bound 0"
    )


def test_no_list_line_holds_a_string_from_source() -> None:
    """The real repository's emitted hits, every todo line (an anchor takes a piece of the line
    that has no quote in it) and each tags line but its free-text reason."""
    keys = [" | ".join(h.key()) for h in lg.collect(CONFIG).hits]
    keys += lg.read_lines(CONFIG.guard_dir / lg.TODO_NAME)
    tags = lg.read_lines(CONFIG.guard_dir / lg.TAGS_NAME)
    keys += [" | ".join(ln.split(" | ")[:4]) for ln in tags]
    for key in keys:
        assert not set("\"'`") & set(key.replace(lg.PLACEHOLDER, "")), key


# -- a cap that protects another machine (080: `pi-guard`) ---------------------------------------

PI_REASON = "limit-ok: pi-guard — a cap that holds when the Pi is broken into"


def pi_guard_repo(repo: Repo, guards: tuple[str, ...]) -> Repo:
    repo.cfg = lg.Config(root=repo.root, guard_dir=repo.root / "tests", text_roots=("web/",),
                         reply_bound=frozenset({"pkg/reader.py"}), pi_guards=guards)
    return repo


def pi_tagged(repo: Repo, path: str) -> list[str]:
    """The tag problems of `TIMEOUT_SECONDS = 30` with a registered `pi-guard` tag at `path`."""
    repo.write(path, f"TIMEOUT_SECONDS = 30  # {PI_REASON}\n")
    repo.append("tests/limitguard-tags.txt", f"{path} | <module> | TIMEOUT_SECONDS = | 30 | "
                "pi-guard | a cap that holds when the Pi is broken into\n")
    return lg.check_tags(repo.cfg)


def test_pi_guard_counts_under_a_configured_path_and_is_registered_like_any_tag(repo: Repo) -> None:
    pi_guard_repo(repo, ("walt/",))
    assert pi_tagged(repo, "walt/server.py") == []
    assert [m for m in lg.check_new(repo.cfg) if "walt/" in m] == []
    assert "pi-guard 1" in lg.summary_line(repo.cfg.guard_dir)


def test_pi_guard_is_refused_outside_the_configured_path_in_words_naming_it(repo: Repo) -> None:
    pi_guard_repo(repo, ("walt/",))
    found = pi_tagged(repo, "pkg/b.py")
    [message] = [m for m in found if "does not count" in m]
    assert any("line with no matching tag" in m for m in found)  # a refused tag is no tag
    assert "the `pi-guard` tag does not count" in message
    assert "`pi-guard` counts only under `walt/` (Config.pi_guards)" in message
    assert "cannot be where it is set" in message
    assert any("pkg/b.py" in m and "TIMEOUT_SECONDS = 30" in m for m in lg.check_new(repo.cfg))


def test_pi_guard_with_no_configured_path_counts_nowhere_not_everywhere(repo: Repo) -> None:
    # Kills: an empty tuple read as "no restriction", which would let every repository tag one.
    pi_guard_repo(repo, ())
    for path in ("pkg/b.py", "walt/server.py"):
        [message] = [m for m in pi_tagged(repo, path) if "does not count" in m]
        assert "counts only under no path in this repository" in message, path
        repo.write(path, "")  # clear for the next, with its registry line removed below
        repo.write("tests/limitguard-tags.txt", "pkg/a.py | <module> | TAGGED = | 998 | format | "
                   "a fact of the format\n")


@pytest.mark.parametrize("path", ["waltz/server.py", "walt/../pi/server.py", "walt.py",
                                  "pi/walt/server.py", "./pkg/b.py"])
def test_pi_guard_is_matched_on_the_normalised_path_whole_not_by_prefix(
        repo: Repo, path: str) -> None:
    # Kills: `startswith("walt")`, a match that ignores `..`, and one that matches a segment
    # anywhere in the path.
    pi_guard_repo(repo, ("walt/",))
    hit = lg.Hit(path, 1, "<module>", "X =", "30", "X", lg.Tag(1, "pi-guard", "three words here"))
    assert "counts only under `walt/`" in (lg.tag_problem(hit, lg.Found(), repo.cfg) or "")


def test_pi_guard_is_matched_after_normalising_a_path_that_goes_down_and_back(repo: Repo) -> None:
    pi_guard_repo(repo, ("walt/",))
    hit = lg.Hit("walt/sub/../server.py", 1, "<module>", "X =", "30", "X",
                 lg.Tag(1, "pi-guard", "three words here"))
    assert lg.tag_problem(hit, lg.Found(), repo.cfg) is None


# -- the read check (080) --------------------------------------------------------------------------

DECLARED = ("from control.limits import Limit\n\n"
            "CAP = Limit('cap_a', 'A cap', 'count', 'household', 'hour', {'-': 5}, {'-': 9})\n"
            "LIMITS = (CAP,)\n")


def reading(repo: Repo, files: dict[str, str], **cfg: object) -> list[str]:
    """The read check over `files` written into the base repository, with `cfg` on the Config."""
    for path, text in files.items():
        repo.write(path, text)
    repo.cfg = lg.Config(root=repo.root, guard_dir=repo.root / "tests", text_roots=("web/",),
                         reply_bound=frozenset({"pkg/reader.py"}), **cfg)  # type: ignore[arg-type]
    return lg.read_problems(repo.cfg)


def test_the_base_repository_declares_nothing_so_nothing_is_unread(repo: Repo) -> None:
    assert reading(repo, {}) == []


def test_a_declared_limit_that_nothing_reads_is_said_with_its_file_line_and_name(
        repo: Repo) -> None:
    # Kills: a check that passes a limit because it is declared and listed in a tuple.
    [message] = reading(repo, {"pkg/limits_here.py": DECLARED})
    assert message.startswith("pkg/limits_here.py:3: the limit `cap_a` is declared and never read")


@pytest.mark.parametrize("read", [
    "limits.value('cap_a')",
    "limits.value('cap_a', 'duet')",
    "self._limits.value(CAP.name)",
    "limits.at_start('cap_a')",
    "value_in_force(CAP, '-', None, now)",
])
def test_a_read_that_names_the_limit_passes_it(repo: Repo, read: str) -> None:
    # Kills: a reader that insists on one spelling of the call.
    use = ("from pkg.limits_here import CAP\n\n\ndef use(limits, now, self=None):\n"
           f"    return {read}\n")
    assert reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": use}) == []


def test_a_name_in_a_call_that_is_not_a_read_does_not_pass_it(repo: Repo) -> None:
    # Kills: any mention of the name counting, a log line or a dict key included.
    use = "def use(log):\n    log.info('cap_a')\n    return {'cap_a': 1}\n"
    [message] = reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": use})
    assert "`cap_a` is declared and never read" in message


def test_a_helper_that_reads_through_a_limit_passes_the_constants_it_is_given(repo: Repo) -> None:
    # Kills: a check that cannot see a read made through `Budget(LIMIT)` and `value(limit.name)`.
    use = ("from pkg.limits_here import CAP\n\n\nclass Budget:\n"
           "    def __init__(self, limit, limits):\n"
           "        self.limit, self.limits = limit, limits\n\n"
           "    def left(self):\n        return self.limits.value(self.limit.name)\n\n\n"
           "def build(limits):\n    return (Budget(CAP, limits), Budget(CAP, limits))\n")
    assert reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": use}) == []


def test_a_constant_only_listed_is_unread_however_many_reads_there_are_elsewhere(
        repo: Repo) -> None:
    # Kills: a generic read anywhere blanket-approving every declared limit.
    declared = DECLARED + ("OTHER = Limit('cap_b', 'B', 'count', 'household', 'hour', {'-': 5}, "
                           "{'-': 9})\nALL = (CAP, OTHER)\n")
    use = ("from pkg.limits_here import CAP\n\n\ndef left(limits, limit):\n"
           "    return limits.value(limit.name)\n\n\ndef go(limits):\n"
           "    return left(limits, CAP)\n")
    [message] = reading(repo, {"pkg/limits_here.py": declared, "pkg/use.py": use})
    assert "`cap_b` is declared and never read" in message


def test_a_constant_used_inside_a_call_inside_a_tuple_is_used(repo: Repo) -> None:
    # Kills: a listing test that treats the whole tuple as a listing (a calendar's two budgets).
    use = ("from pkg.limits_here import CAP\n\n\ndef build(make, limits):\n"
           "    return (make(CAP), limits.value(CAP.name))\n")
    assert reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": use}) == []
    helper = "def make(limit, limits):\n    return limits.value(limit.name)\n"
    only_made = ("from pkg.limits_here import CAP\n\n\ndef build(make):\n"
                 "    return (make(CAP), 2)\n")
    assert reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": only_made,
                          "pkg/helper.py": helper}) == []


def test_an_inline_declaration_is_read_through_a_generic_read(repo: Repo) -> None:
    # Kills: a limit built inline in a dict (no constant) always reported unread, and one reported
    # read with no read at all.
    declared = ("TABLE = {'a': Limit('cap_a', 'A', 'count', 'household', 'hour', {'-': 5}, "
                "{'-': 9})}\n")
    use = "def left(limits, kind):\n    return limits._value(TABLE[kind].name)\n"
    assert reading(repo, {"pkg/limits_here.py": declared, "pkg/use.py": use}) == []
    [message] = reading(repo, {"pkg/use.py": "def left():\n    return 3\n"})
    assert "`cap_a` is declared and never read" in message


def test_a_value_helper_with_no_limit_in_its_arguments_is_not_a_read(repo: Repo) -> None:
    # Kills: every `_value(...)` call counted as a limit read (an ordinary helper's name).
    use = "def show(part):\n    return _value(part) or ''\n"
    assert reading(repo, {"pkg/use.py": use}) == []


def test_a_read_at_module_level_class_level_or_in_a_default_argument_is_said(repo: Repo) -> None:
    # Kills: a read frozen at the default, because no answer from Q Branch exists at import.
    use = ("from pkg.limits_here import CAP\n\nSIZE = limits.value(CAP.name)\n\n\n"
           "class C:\n    HELD = limits.value('cap_a')\n\n\n"
           "def f(n=limits.value('cap_a')):\n    return n\n\n\n"
           "def ok(limits):\n    return limits.value('cap_a')\n")
    found = reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": use})
    assert [m.split(" a read of")[0] for m in found] == [
        "pkg/use.py:3:", "pkg/use.py:7:", "pkg/use.py:10:"]
    assert "the limit `cap_a`" in found[0] and "run once before Q Branch has answered" in found[0]


def test_a_lambda_at_module_level_is_a_deferred_read(repo: Repo) -> None:
    # Kills: a module-level lambda body counted as an import-time read.
    use = "READ = lambda limits: limits.value('cap_a')\n"
    assert reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": use}) == []


def test_the_declarations_own_figures_are_not_read_outside_the_library(repo: Repo) -> None:
    # Kills: `.defaults[...]` used as the value in force, which no row of Q Branch can change.
    use = ("def use(limits):\n    limits.value('cap_a')\n    return CAP.defaults['-'], "
           "CAP.minima['-'], CAP.maxima['-']\n")
    found = reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": use})
    assert [m.split(": ")[0] for m in found] == ["pkg/use.py:3"] * 3
    assert sorted(m.split("`")[1] for m in found) == [".defaults[...]", ".maxima[...]",
                                                       ".minima[...]"]
    assert all("outside `limits.py`" in m for m in found)
    # the library itself may read them, and so may a file the repository names
    assert reading(repo, {"pkg/use.py": "", "pkg/limits.py": use}) == []
    assert reading(repo, {"pkg/limits.py": "", "pkg/use.py": use},
                   table_readers=("pkg/use.py",)) == []


def test_a_live_limit_read_in_init_is_said_and_a_restart_one_read_with_at_start_is_not(
        repo: Repo) -> None:
    # Kills: the page saying nothing about a restart that is in fact needed.
    live = ("class B:\n    def __init__(self, limits):\n"
            "        self.cap = limits.value('cap_a')\n")
    [message] = reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": live})
    assert message.startswith("pkg/use.py:3: `cap_a` is read in an `__init__` but is `live`")
    [by_constant] = reading(repo, {"pkg/use.py": live.replace("'cap_a'", "CAP.name")})
    assert "`cap_a` is read in an `__init__`" in by_constant
    restart = DECLARED.replace("{'-': 9})", "{'-': 9}, applies='restart')")
    started = live.replace("limits.value", "limits.at_start")
    assert reading(repo, {"pkg/limits_here.py": restart, "pkg/use.py": started}) == []
    # a restart limit read with `value` in `__init__` is not the live case
    assert reading(repo, {"pkg/limits_here.py": restart, "pkg/use.py": live}) == []


@pytest.mark.parametrize("body", [
    "x = limits.value('cap_a')\n    return x or 5",
    "x = limits.value('cap_a')\n    return 5 if not x else x",
    "x = limits.value('cap_a')\n    if x:\n        return 1\n    return 0",
    "return limits.value('cap_a') or 5",
    "return 7 if limits.value('cap_a') else 5",
    "self.n = limits.value('cap_a')\n    return self.n or 5",
])
def test_a_read_used_as_a_truth_value_is_said(repo: Repo, body: str) -> None:
    # Kills: a limit of 0 read as no limit, which is the brake turned into its opposite.
    use = f"def use(limits, self=None):\n    {body}\n"
    [message] = reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": use})
    assert message.startswith("pkg/use.py:") and "used as a condition" in message


@pytest.mark.parametrize("body", [
    "x = limits.value('cap_a')\n    return x > 0",
    "x = limits.value('cap_a')\n    return x is not None and x < 9",
    "x = limits.value('cap_a')\n    return 5 if x == 0 else x",
    "return limits.value('cap_a') + 1",
])
def test_a_read_that_is_compared_is_not_a_truth_value(repo: Repo, body: str) -> None:
    use = f"def use(limits):\n    {body}\n"
    assert reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": use}) == []


def test_a_truth_test_of_something_that_is_not_a_read_is_left_alone(repo: Repo) -> None:
    # Kills: any `or` in a function that also reads a limit.
    use = ("def use(limits, other):\n    n = limits.value('cap_a')\n    m = other or 5\n"
           "    return n + m\n")
    assert reading(repo, {"pkg/limits_here.py": DECLARED, "pkg/use.py": use}) == []


def test_helper_names_beside_limit_declare_a_limit_only_when_configured(repo: Repo) -> None:
    # Kills: a helper's first string argument read as a limit's name when the repository never
    # said so, and one the repository did say so that is not found.
    helper = "CAP = _cap('cap_h', 'A cap', 'count', 'day', 5, 9)\n"
    assert reading(repo, {"pkg/helpers.py": helper}) == []
    [message] = reading(repo, {"pkg/helpers.py": helper}, declarers=("_cap",))
    assert "`cap_h` is declared and never read" in message


def test_a_limit_known_unread_is_held_back_and_must_be_dropped_once_read(repo: Repo) -> None:
    # Kills: an escape that stays for ever after the read arrives, or names a limit that is gone.
    assert reading(repo, {"pkg/limits_here.py": DECLARED}, unread=("cap_a",)) == []
    read = "def use(limits):\n    return limits.value('cap_a')\n"
    [message] = reading(repo, {"pkg/use.py": read}, unread=("cap_a",))
    assert "`cap_a` is now read; delete it from Config.unread" in message
    [gone] = reading(repo, {}, unread=("cap_z",))
    assert "Config.unread names `cap_z`, which no `Limit` declares" in gone


def test_tests_and_skipped_paths_are_not_read_by_the_read_check(repo: Repo) -> None:
    # Kills: a read in a test counting as the shipped code reading the limit.
    read = "def t(limits):\n    return limits.value('cap_a')\n"
    [message] = reading(repo, {"pkg/limits_here.py": DECLARED, "tests/test_x.py": read})
    assert "`cap_a` is declared and never read" in message


if __name__ == "__main__":
    sys.exit(lg.cli(CONFIG, sys.argv[1:]))
