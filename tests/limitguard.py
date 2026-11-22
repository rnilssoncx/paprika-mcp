"""The limit guard (068): finds a limit-shaped value that is neither a declared `Limit` nor marked.

**This file is identical in every repository.** Nothing in it names a repository. What differs
(extra skipped paths, where non-Python files are read, the modules that read Q Branch) is a
`Config` that the repository's own `test_limitguard.py` builds and passes in. Edit the copy under
`068-guard/` in the coordinator's notes, never one repository's file alone.

**What it reads.** Every file git lists in the working tree and does not ignore (tracked and
untracked, so a new file is read in the run that gates its commit), minus the skipped paths.
Python is every `*.py` and every extensionless file whose first line names python; it is read with
the `ast` module, and a file that does not parse is a problem, never a skip. Other files (the
household pages' JavaScript and `.html` (its inline script), compose, systemd units, shell,
Dockerfiles, the Caddyfile) are read by pattern, only under `Config.text_roots`.

**What it flags** (a literal other than 0, 1 and -1; literal arithmetic such as `64 << 20` counts
as one literal):

- any upper-case constant at module or class level, tuple targets paired with their values;
- any name, keyword or default argument carrying a limit word (`WORDS`), whatever the expression
  around the literal (`deadline = time.monotonic() + 30`, `timeout=httpx.Timeout(20.0)`);
- a dict literal's key carrying a limit word with a numeric value (`{"max_results": 100}`);
- `.get("<limit word>", N)`, a literal compared with a limit-worded name or with `len(...)`,
  the limit argument of the calls in `CALL_ARGS` (tuples walked), `range(N)`, a slice bound of 16
  or more.

A literal inside a call named exactly `Limit` or `_limit` is a declaration and is never flagged.

**How a value is allowed to stay.** A comment `# limit-ok: <kind> — <reason of three words or
more>` (`//` in JavaScript) on the value's own lines or on a comment-only line directly above it.
The kinds are `KINDS`; `maximum`, `reply-bound` and `not-a-limit` each have a condition (see
`tag_problem`). Every tag is also a line in `limitguard-tags.txt`, so a new escape is an edit to
two files and Robert reads the registry in one place.

**No string from source is ever copied into a list.** A hit's value is the expression with every
string literal replaced by `PLACEHOLDER`, and `what` holds a string only when it is a plain word.

**What is not converted yet** is `limitguard-todo.txt`: a detector hit as `path | scope | what |
value` (no line number, so an edit elsewhere does not move it) or a hand-found miss as `path |
anchor`, an anchor being a piece of the line's text of at least `MIN_ANCHOR` characters, with no
` | ` in it. The list is a multiset.

Run `python tests/test_limitguard.py --strict` (or `python -m tests.test_limitguard --strict`
where `tests/` is a package) to list everything still on the list and exit 1; `--emit` prints
every unmarked hit as a todo line.
"""

from __future__ import annotations

import ast
import copy
import fnmatch
import functools
import hashlib
import io
import posixpath
import re
import shutil
import subprocess
import sys
import tokenize
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NON_SHIPPING = ("deploy/", "plans/", "spikes/")
DEFAULT_SKIP = ("tests/", "evals/", *NON_SHIPPING)
KINDS = ("maximum", "reply-bound", "format", "not-a-limit", "pi-guard")
STRONG = frozenset(  # `sleep` and `wait` are here so an unnamed `wait_for(x, 30)` is refused too
    "timeout timeouts seconds wall deadline max cap caps bytes retries attempts limit sleep wait"
    .split()
)
TODO_NAME = "limitguard-todo.txt"
TAGS_NAME = "limitguard-tags.txt"
EXEMPT_CALLS = frozenset({"Limit", "_limit"})
# Where a Limit's maxima sit: (positional index, keyword) per declaring call.
MAXIMA_ARG = {"Limit": (6, "maxima"), "_limit": (5, "maximum")}

WORDS = frozenset(
    """timeout timeouts seconds secs sec ms millis minutes mins hours days wall deadline interval
    every period ttl age window delay backoff wait sleep retry retries attempt attempts tries max
    maximum min minimum limit limits cap caps ceiling budget budgets size sizes bytes octets kib
    mib gib kb mb gb pages chars length len count batch chunk workers concurrency parallel slots
    hourly daily rate quota depth hits top lines rows items keep cpu rlimit maxsize maxlen stale
    grace poll lease horizon allowance per hour day month week most longest cutoff expiry expires
    lifetime pool""".split()
)
# call name -> positional indexes that are a limit; the argument is walked, so a tuple counts
CALL_ARGS: dict[str, tuple[int, ...]] = {
    "sleep": (0,), "wait_for": (1,), "timeout": (0,), "fail_after": (0,), "move_on_after": (0,),
    "Semaphore": (0,), "BoundedSemaphore": (0,), "ThreadPoolExecutor": (0,),
    "ProcessPoolExecutor": (0,), "islice": (1, 2), "timedelta": (0, 1, 2, 3, 4, 5, 6),
    "Timeout": (0, 1, 2, 3), "setrlimit": (1,), "alarm": (0,), "settimeout": (0,),
    "lru_cache": (0,), "deque": (1,), "Queue": (0,), "read": (0,), "read1": (0,), "recv": (0,),
    "readline": (0,), "fetchmany": (0,), "join": (0,), "wait": (0,), "call_later": (0,),
    "acquire": (0, 1), "result": (0,), "Timer": (0,), "communicate": (1,),
}  # fmt: skip
# the calls whose arguments count inside an upper-case constant (a refusal's HTTP status does not)
TIME_CALLS = frozenset("timedelta Timeout Limits timeout frozenset set tuple dict list".split())

MALFORMED = "a `limit-ok` comment that does not read `limit-ok: <kind> — <reason>`"
STRAY = "a `limit-ok` tag with no limit on its line or on the line below it"
TAG = re.compile(r"limit-ok:\s*(?P<kind>[A-Za-z-]+)\s*(?:—|--|-|:)\s*(?P<reason>.*?)\s*$")
TAG_WORD = re.compile(r"limit-ok\b")


PLACEHOLDER = '"…"'  # stands for every string literal in a value written to a shipped list


class Scrub(ast.NodeTransformer):
    """A copy of an expression with every string literal replaced by `PLACEHOLDER`, so a string
    from source (an address, a name) is never copied into a todo or tags line."""

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, (str, bytes)):
            return ast.copy_location(ast.Name(id=PLACEHOLDER, ctx=ast.Load()), node)
        return node

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        return ast.copy_location(ast.Name(id=PLACEHOLDER, ctx=ast.Load()), node)


def unparsed(node: ast.AST) -> str:
    return ast.unparse(Scrub().visit(copy.deepcopy(node)))


def label(text: str) -> str:
    """A string literal's text for a `what` field: itself when it is a plain identifier-like word,
    else the placeholder."""
    return text if re.fullmatch(r"[\w.\-]{1,40}", text) else PLACEHOLDER


def words(name: str) -> set[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return {w for w in re.split(r"[^a-z0-9]+", name.lower()) if w}


def limitish(name: str | None) -> bool:
    return bool(name) and bool(words(name or "") & WORDS)


@dataclass(frozen=True)
class Config:
    """What differs per repository. `root` is the repository root, `guard_dir` holds the two text
    files. `skip`: path prefixes (ending in `/`) or `fnmatch` patterns from the root, added to
    `DEFAULT_SKIP`. `text_roots`: path prefixes under which non-Python files are read.
    `reply_bound`: the paths of modules that read Q Branch, the only places `reply-bound` counts.
    `pi_guards`: the path prefixes (each ending in `/`) where a `pi-guard` tag counts, a cap that
    protects another machine from a broken-into Pi and so cannot be set from Q Branch (080 § The
    guard learns a fifth kind); **empty means nowhere**, which is every repository but mcp-host.
    `declarers`: the helper functions besides `Limit` and `_limit` whose first string argument is
    a limit's name, for the read check (Quartermaster's `_cap` and `TimeLimit`).
    `table_readers`: the files, besides `limits.py`, that may read a declaration's own `defaults`,
    `minima` or `maxima` (Q Branch's page, which draws them, and a provider that stands in for Q
    Branch where there is none); each is named in the repository's own block with its reason.
    `unread`: names of limits declared and known not to be read yet (another change is mid-way);
    the read check holds a name here only while nothing reads it, so the list can only shrink."""

    root: Path
    guard_dir: Path
    skip: tuple[str, ...] = ()
    text_roots: tuple[str, ...] = ()
    reply_bound: frozenset[str] = frozenset()
    pi_guards: tuple[str, ...] = ()
    declarers: tuple[str, ...] = ()
    table_readers: tuple[str, ...] = ()
    unread: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tag:
    line: int
    kind: str
    reason: str


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    scope: str
    what: str
    value: str
    name: str
    tag: Tag | None = None

    def key(self) -> tuple[str, str, str, str]:
        return (self.path, self.scope, self.what, self.value)

    def where(self) -> str:
        return f"{self.path}:{self.line} ({self.scope}) {self.what} {self.value}"


@dataclass
class Found:
    hits: list[Hit] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    maxima: dict[str, set[str]] = field(default_factory=dict)  # path -> names inside a maxima arg


# --------------------------------------------------------------------------------------------
# Python


def lit(node: ast.AST) -> float | None:
    """The value of a literal numeric expression (`1024 * 1024`, `1 << 20`, `-5`), else None."""
    if isinstance(node, ast.Constant):
        v = node.value
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = lit(node.operand)
        return None if v is None else -v
    if isinstance(node, ast.BinOp):
        a, b = lit(node.left), lit(node.right)
        if a is None or b is None:
            return None
        try:
            return {
                ast.Mult: lambda: a * b, ast.Add: lambda: a + b, ast.Sub: lambda: a - b,
                ast.Pow: lambda: a**b, ast.LShift: lambda: int(a) << int(b),
                ast.Div: lambda: a / b, ast.FloorDiv: lambda: a // b,
            }[type(node.op)]()  # fmt: skip
        except (KeyError, ZeroDivisionError, ValueError, OverflowError, MemoryError):
            return None
    return None


def trivial(node: ast.AST) -> bool:
    v = lit(node)
    return v is None or v in (0, 1, -1)


def callname(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def opname(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else ""


def literals(node: ast.AST) -> list[ast.AST]:
    """Every non-trivial literal in an expression, whatever surrounds it. A literal expression is
    one literal. A subscript's index and a `Limit(...)` declaration are not read."""
    if lit(node) is not None:
        return [] if trivial(node) else [node]
    if isinstance(node, ast.Call) and callname(node) in EXEMPT_CALLS:
        return []
    if isinstance(node, ast.JoinedStr):
        return []
    if isinstance(node, ast.Subscript):
        return literals(node.value)
    out: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        out += literals(child)
    return out


def scaled(node: ast.AST) -> list[ast.AST]:
    """The literals that scale a name (`MAX_FIELD * 4`), not an offset (`position + 8`)."""
    out: list[ast.AST] = []
    for n in ast.walk(node):
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Mult, ast.LShift)):
            out += [x for x in (n.left, n.right) if lit(x) is not None and not trivial(x)]
    return out


def constant_literals(node: ast.AST, inside_call: bool = False) -> list[ast.AST]:
    """The literals of an upper-case constant's value: those of a collection, a `Timeout(...)` or
    a `timedelta(...)`, and a name multiplied by a literal; not the arguments of an ordinary
    call."""
    if (isinstance(node, ast.BinOp) and lit(node) is None
            and any(lit(s) is not None and not trivial(s) for s in (node.left, node.right))):
        return [node]
    if lit(node) is not None:
        return [] if trivial(node) or inside_call else [node]
    if isinstance(node, ast.Call):
        if callname(node) in EXEMPT_CALLS:
            return []
        keep = callname(node) in TIME_CALLS or limitish(callname(node))
        out: list[ast.AST] = []
        for a in node.args:
            out += constant_literals(a, inside_call=not keep)
        for kw in node.keywords:
            out += constant_literals(kw.value, inside_call=not (keep or limitish(kw.arg)))
        return out
    if isinstance(node, (ast.JoinedStr, ast.Subscript)):
        return []
    out = []
    for child in ast.iter_child_nodes(node):
        out += constant_literals(child, inside_call)
    return out


def pair_targets(target: ast.AST, value: ast.AST) -> list[tuple[str, ast.AST]]:
    """(name, value) per assigned name; a tuple target takes the matching element of a tuple."""
    if isinstance(target, (ast.Name, ast.Attribute)):
        return [(opname(target), value)]
    if isinstance(target, (ast.Tuple, ast.List)):
        if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts):
            out: list[tuple[str, ast.AST]] = []
            for t, v in zip(target.elts, value.elts, strict=True):
                out += pair_targets(t, v)
            return out
        out = []
        for t in target.elts:
            out += pair_targets(t, value)
        return out
    return []


def shorten(text: str) -> str:
    """A value over 160 characters (a schema, a table) keeps its start and a digest of all of it."""
    if len(text) <= 160:
        return text
    head = text[:80]
    if head.count('"') % 2:  # the cut fell inside a placeholder: drop its opening half
        head = head[: head.rfind('"')]
    return f"{head}...#{hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:12]}"


def parse_comments(source: str, lines: list[str]) -> tuple[dict[int, Tag], set[int], list[str]]:
    """Tags by line, the lines that hold only a comment, and the tags that do not parse."""
    tags: dict[int, Tag] = {}
    only: set[int] = set()
    bad: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        n, col = tok.start
        if not lines[n - 1][:col].strip():
            only.add(n)
        if TAG_WORD.search(tok.string):
            m = TAG.search(tok.string)
            if m:
                tags[n] = Tag(n, m["kind"], m["reason"])
            else:
                bad.append(f"line {n}: {MALFORMED}")
    return tags, only, bad


class PyScan(ast.NodeVisitor):
    def __init__(self, path: str, tags: dict[int, Tag], only: set[int]) -> None:
        self.path, self.tags, self.only = path, tags, only
        self.scope: list[str] = []
        self.funcs = 0
        self.in_limit = 0
        self.covered: set[int] = set()
        self.used: set[int] = set()
        self.hits: list[Hit] = []
        self.maxima: set[str] = set()

    # a hit ------------------------------------------------------------------------------
    def tag_for(self, first: int, last: int) -> Tag | None:
        for n in range(first, last + 1):
            if n in self.tags:
                self.used.add(n)
                return self.tags[n]
        if first - 1 in self.tags and first - 1 in self.only:
            self.used.add(first - 1)
            return self.tags[first - 1]
        return None

    def add(self, span: Any, lits: Sequence[ast.AST], what: str, value: ast.AST | str, name: str,
            start: Any = None) -> None:
        if self.in_limit:
            return
        lits = [n for n in lits if id(n) not in self.covered]
        if not lits:
            return
        self.covered.update(id(n) for x in lits for n in ast.walk(x))
        first = (start or span).lineno
        last = getattr(span, "end_lineno", None) or span.lineno
        text = shorten(re.sub(r"\s+", "", value if isinstance(value, str) else unparsed(value)))
        self.hits.append(Hit(self.path, span.lineno, ".".join(self.scope) or "<module>", what, text,
                             name, self.tag_for(first, last)))

    # scopes -------------------------------------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.funcs += 1
        a = node.args
        pos = a.posonlyargs + a.args
        pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults, strict=True))
        pairs += [(g, d) for g, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None]
        for arg, default in pairs:
            if limitish(arg.arg):
                what = f"default {arg.arg}="
                self.add(default, literals(default), what, default, arg.arg, start=arg)
        self.generic_visit(node)
        self.funcs -= 1
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # assignments --------------------------------------------------------------------------
    def assigned(self, node: ast.stmt, targets: Sequence[ast.AST], value: ast.AST) -> None:
        for target in targets:
            for name, val in pair_targets(target, value):
                constant = name.isupper() and not self.funcs
                lits = constant_literals(val) if constant else []
                if not lits and limitish(name):
                    lits = literals(val)
                if lits:
                    self.add(node, lits, f"{name} =", val, name)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.assigned(node, node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.assigned(node, [node.target], node.value)
        self.generic_visit(node)

    # calls --------------------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        name = callname(node)
        if name in EXEMPT_CALLS:
            index, keyword = MAXIMA_ARG[name]
            args = [node.args[index]] if len(node.args) > index else []
            args += [kw.value for kw in node.keywords if kw.arg == keyword]
            for arg in args:
                self.maxima.update(opname(n) for n in ast.walk(arg) if opname(n))
            self.in_limit += 1
            self.generic_visit(node)
            self.in_limit -= 1
            return
        for kw in node.keywords:
            if kw.arg and limitish(kw.arg):
                self.add(kw.value, literals(kw.value), f"{name}({kw.arg}=)", kw.value, kw.arg)
        for i in CALL_ARGS.get(name, ()):
            if len(node.args) > i:
                self.add(node.args[i], literals(node.args[i]), f"{name}(arg {i})", node.args[i], "")
        if name == "range" and any(not trivial(a) for a in node.args):
            lits = [a for a in node.args if not trivial(a)]
            self.add(node, lits, "range(N)", ",".join(unparsed(a) for a in node.args), "")
        if (name == "get" and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str) and limitish(node.args[0].value)):
            self.add(node.args[1], literals(node.args[1]), f".get({label(node.args[0].value)})",
                     node.args[1], node.args[0].value)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and limitish(key.value)):
                self.add(value, literals(value), f"dict key {label(key.value)}", value, key.value)
        self.generic_visit(node)

    def visit_Slice(self, node: ast.Slice) -> None:
        for bound in (node.lower, node.upper):
            if bound is not None and abs(lit(bound) or 0) >= 16:
                self.add(bound, [bound], "slice bound", bound, "")
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        sides = [node.left, *node.comparators]
        named = [opname(s) for s in sides if limitish(opname(s))]
        if named or any(isinstance(s, ast.Call) and callname(s) == "len" for s in sides):
            for s in sides:
                if not (isinstance(s, ast.Call) and callname(s) == "len"):
                    lits = [s] if lit(s) is not None and not trivial(s) else scaled(s)
                    self.add(s, lits, f"{named[0] if named else 'len()'} compared with", s,
                             named[0] if named else "")
        self.generic_visit(node)


def python_hits(path: str, source: str) -> tuple[list[Hit], set[str], list[str]]:
    """Hits, the names a `Limit` takes its maxima from, and problems, for one Python file."""
    try:
        tree = ast.parse(source)
        lines = re.split(r"\r\n|\r|\n", source)
        tags, only, bad = parse_comments(source, lines)
    except (SyntaxError, ValueError, tokenize.TokenError) as err:
        reason = "a file that does not parse is not skipped"
        return [], set(), [f"{path}: does not parse ({err}); {reason}"]
    scan = PyScan(path, tags, only)
    scan.visit(tree)
    problems = [f"{path}: {b}" for b in bad]
    for n in sorted(set(tags) - scan.used):
        problems.append(f"{path}:{n}: {STRAY}")
    return scan.hits, scan.maxima, problems


# --------------------------------------------------------------------------------------------
# Other files: JavaScript, compose, systemd, shell, Dockerfile, Caddyfile

NUM = r"-?\d[\d_]*(?:\.\d+)?(?:\s*[*/]\s*\d[\d_]*(?:\.\d+)?)*"
JS_PATTERNS = [
    ("const", re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Z][A-Z0-9_]*)\s*=\s*(.*\d.*)$")),
    ("timer", re.compile(rf"\b(setTimeout|setInterval)\([^;]*,\s*({NUM})\s*\)")),
    ("prop", re.compile(rf"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*({NUM})\b")),
    ("default", re.compile(rf"[(,]\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*({NUM})\s*[,)]")),
    ("slice", re.compile(rf"\.(?:slice|substring|substr)\(\s*\d+\s*,\s*({NUM})\s*\)")),
    ("length", re.compile(rf"\.length\s*[<>]=?\s*({NUM})")),
    ("lname", re.compile(rf"\b(?:const|let|var)\s+([a-z][A-Za-z0-9_]*)\s*=\s*({NUM})\s*;?\s*$")),
]
CONF_PATTERNS = [
    ("key", re.compile(
        rf"^\s*-?\s*([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*[\"']?({NUM}[a-zA-Z]{{0,3}})[\"']?\s*$")),
    ("opt", re.compile(rf"\b(size|nr_inodes|mode)=({NUM}[kmgKMG]?)")),
    ("cmd", re.compile(
        rf"(?<![\w])(sleep|timeout|--max-time|--connect-timeout|--interval|--start-period|--retries"
        rf"|-m|--retry|limit rate|burst)[ =]+({NUM}[a-z/]*)")),
]  # fmt: skip
TEXT_SUFFIXES = frozenset(
    ".js .mjs .cjs .html .yml .yaml .service .timer .socket .sh .conf".split()
)
JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".html"})  # a page's inline script is JavaScript
QUOTED = re.compile(r"""(["'`])(?:\\.|(?!\1).)*\1""")
TRIVIAL_TEXT = frozenset({"0", "1", "-1"})


def split_comment(line: str, js: bool) -> tuple[str, str]:
    """Code and comment of a line; a comment marker inside a string does not start one."""
    quote = ""
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'" or (js and c == "`"):
            quote = c
        elif (js and line.startswith("//", i)) or (
            not js and c == "#" and (i == 0 or line[i - 1].isspace())
        ):
            return line[:i], line[i:]
        i += 1
    return line, ""


def text_hits(path: str, text: str, js: bool) -> tuple[list[Hit], list[str]]:
    lines = text.split("\n")
    tags: dict[int, Tag] = {}
    only: set[int] = set()
    problems: list[str] = []
    split = [split_comment(line, js) for line in lines]
    for n, (code, comment) in enumerate(split, 1):
        if comment and not code.strip():
            only.add(n)
        if TAG_WORD.search(comment):
            m = TAG.search(comment)
            if m:
                tags[n] = Tag(n, m["kind"], m["reason"])
            else:
                problems.append(f"{path}:{n}: {MALFORMED}")
    hits: list[Hit] = []
    used: set[int] = set()
    for n, (code, _) in enumerate(split, 1):
        seen: set[tuple[str, str]] = set()
        for kind, rx in JS_PATTERNS if js else CONF_PATTERNS:
            for m in rx.finditer(code):
                name = m.group(1) if (m.lastindex or 0) >= 2 else ""
                val = m.group(m.lastindex or 1).strip()
                if val in TRIVIAL_TEXT or (name, val) in seen:
                    continue
                if kind in ("prop", "default", "lname", "key", "opt") and not (
                    limitish(name) or name in ("size", "nr_inodes")
                    or (kind == "key" and name.isupper() and not js)
                ):
                    continue
                if kind == "slice" and abs(float(eval_num(val))) < 16:
                    continue
                if kind == "const" and not re.search(r"(?<![\w.])\d", val):
                    continue
                seen.add((name, val))
                tag = tags.get(n) or (tags.get(n - 1) if n - 1 in only else None)
                if tag:
                    used.add(tag.line)
                value = shorten(re.sub(r"\s+", "", scrub_text(val)))
                hits.append(Hit(path, n, "-", f"{kind} {name}".strip(), value, name, tag))
    for n in sorted(set(tags) - used):
        problems.append(f"{path}:{n}: {STRAY}")
    return hits, problems


def scrub_text(text: str) -> str:
    """`text` with every quoted string, and any quote left unclosed, replaced by `PLACEHOLDER`."""
    marked = QUOTED.sub("\0", text)
    marked = re.sub(r"[\"'`].*$", "\0", marked)
    return marked.replace("\0", PLACEHOLDER)


def eval_num(expr: str) -> float:
    """A number or a product or quotient of numbers, as text (`1024 * 1024`)."""
    total = 1.0
    op = "*"
    for part in re.split(r"([*/])", expr.replace("_", "")):
        part = part.strip()
        if part in ("*", "/"):
            op = part
        elif part:
            total = total * float(part) if op == "*" else total / float(part)
    return total


# --------------------------------------------------------------------------------------------
# The files


def skipped(rel: str, cfg: Config) -> bool:
    for entry in (*DEFAULT_SKIP, *cfg.skip):
        if entry.endswith("/"):
            if rel.startswith(entry):
                return True
        elif rel == entry or fnmatch.fnmatchcase(rel, entry):
            return True
    return False


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("limit guard: git is not on PATH; it lists the files to read")
    return subprocess.run([git, "-C", str(root), *args], capture_output=True, check=True)  # noqa: S603, RUF100


def listed(root: Path) -> list[str]:
    """Every file git lists and does not ignore: tracked, and untracked but not ignored."""
    out = run_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout
    return sorted({n for n in out.decode("utf-8", "surrogateescape").split("\0") if n})


def first_line(path: Path) -> str:
    with path.open("rb") as f:
        return f.readline(200).decode("utf-8", "replace")


def collect(cfg: Config) -> Found:
    found = Found()
    for rel in listed(cfg.root):
        p = cfg.root / rel
        if skipped(rel, cfg) or not p.is_file():
            continue
        suffix = p.suffix.lower()
        head = first_line(p) if not suffix else ""
        python = suffix == ".py" or (head.startswith("#!") and "python" in head)
        text = (
            not python
            and any(rel.startswith(r) for r in cfg.text_roots)
            and (suffix in TEXT_SUFFIXES or p.name.startswith(("Dockerfile", "Caddyfile"))
                 or (head.startswith("#!") and ("sh" in head or "bash" in head)))
        )  # fmt: skip
        if not python and not text:
            continue
        try:
            source = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            found.problems.append(f"{rel}: is not UTF-8; a file that cannot be read is not skipped")
            continue
        hits, maxima, problems = scanned(rel, source, python, suffix in JS_SUFFIXES)
        if python:
            found.maxima[rel] = set(maxima)
        found.hits += hits
        found.problems += problems
    return found


@functools.cache
def scanned(
    rel: str, source: str, python: bool, js: bool
) -> tuple[tuple[Hit, ...], frozenset[str], tuple[str, ...]]:
    """One file's hits, maxima names and problems. Cached on the file's text, so the three checks
    of one run parse it once, and an edit between two runs in one process is still read."""
    if python:
        hits, maxima, problems = python_hits(rel, source)
        return tuple(hits), frozenset(maxima), tuple(problems)
    hits, problems = text_hits(rel, source, js)
    return tuple(hits), frozenset(), tuple(problems)


# --------------------------------------------------------------------------------------------
# The two lists


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]  # fmt: skip


MIN_ANCHOR = 4


def todo_lines(
    cfg: Config,
) -> tuple[Counter[tuple[str, ...]], list[tuple[str, str]], list[str]]:
    """The hit lines (a multiset of 4-field keys), the hand-found anchors (path, anchor), and the
    lines that are neither: an anchor may not hold ` | `, which would read as a hit line, and
    must be at least `MIN_ANCHOR` characters."""
    hits: Counter[tuple[str, ...]] = Counter()
    anchors: list[tuple[str, str]] = []
    bad: list[str] = []
    for line in read_lines(cfg.guard_dir / TODO_NAME):
        parts = line.split(" | ")
        if len(parts) == 4:
            hits[tuple(parts)] += 1
        elif len(parts) == 2 and len(parts[1]) >= MIN_ANCHOR:
            anchors.append((parts[0], parts[1]))
        else:
            bad.append(f"{TODO_NAME}: `{line[:60]}` is neither `path | scope | what | value` nor "
                       f"`path | anchor` (an anchor of at least {MIN_ANCHOR} characters, with no "
                       "` | ` in it)")
    return hits, anchors, bad


def tag_lines(cfg: Config) -> Counter[tuple[str, ...]]:
    out: Counter[tuple[str, ...]] = Counter()
    for line in read_lines(cfg.guard_dir / TAGS_NAME):
        out[tuple(line.split(" | ", 5))] += 1
    return out


def on_pi_guard_path(path: str, cfg: Config) -> bool:
    """Whether `path`, normalised, sits under one of `Config.pi_guards`. The prefix is matched whole
    (`walt/` is not `waltz/`) and on the normalised path, so `walt/../pi/x.py` is `pi/x.py`."""
    clean = posixpath.normpath(path)
    return any(clean.startswith(posixpath.normpath(guard) + "/") for guard in cfg.pi_guards)


def tag_problem(hit: Hit, found: Found, cfg: Config) -> str | None:
    """Why a hit's tag does not count, or None when it does."""
    tag = hit.tag
    if tag is None:
        return None
    if tag.kind not in KINDS:
        return f"the kind `{tag.kind}` is not one of {', '.join(KINDS)}"
    if len(tag.reason.split()) < 3:
        return "the reason is under three words; say why this is not a limit anyone could change"
    if tag.kind == "maximum" and hit.name not in found.maxima.get(hit.path, set()):
        return "`maximum` counts only on a name this file passes into a `Limit(...)`'s maxima"
    if tag.kind == "reply-bound" and hit.path not in cfg.reply_bound:
        return "`reply-bound` counts only in a module that reads Q Branch (Config.reply_bound)"
    if tag.kind == "pi-guard" and not on_pi_guard_path(hit.path, cfg):
        where = ", ".join(f"`{g}`" for g in cfg.pi_guards) or "no path in this repository"
        return (f"`pi-guard` counts only under {where} (Config.pi_guards): a cap that protects "
                "another machine from a broken-into Pi, so Q Branch, which runs on the Pi, cannot "
                "be where it is set")
    strong_words = (words(hit.name) | words(hit.what)) & STRONG
    if tag.kind == "not-a-limit" and strong_words:
        strong = sorted(strong_words)[0]
        return f"`not-a-limit` is refused on a hit whose name or description carries `{strong}`"
    return None


def accepted(hit: Hit, found: Found, cfg: Config) -> bool:
    return hit.tag is not None and tag_problem(hit, found, cfg) is None


HOW_OUT = (
    "Declare it as a Limit (Q Branch's Limits page; 059), or, if it is a maximum, a bound on Q "
    "Branch's own reply, a format fact, a cap that protects another machine from a broken-into "
    "Pi (`pi-guard`, only where Config.pi_guards allows) or not a limit at all, mark it in place "
    "with `# limit-ok: <kind> — <why, three words or more>` and add the same line to "
    f"{TAGS_NAME}. Adding it to {TODO_NAME} is not a way out."
)


def check_new(cfg: Config) -> list[str]:
    """Every hit that is neither accepted by a tag nor on the list: a new limit."""
    found = collect(cfg)
    listed_hits, _, _ = todo_lines(cfg)
    left = Counter(listed_hits)
    out = list(found.problems)
    for hit in found.hits:
        if accepted(hit, found, cfg):
            continue
        if left[hit.key()] > 0:
            left[hit.key()] -= 1
        else:
            out.append(f"new limit: {hit.where()}")
    return out


def check_list(cfg: Config) -> list[str]:
    """Every line on the list still matches: a converted value must leave it."""
    found = collect(cfg)
    listed_hits, anchors, out = todo_lines(cfg)
    have: Counter[tuple[str, ...]] = Counter(
        h.key() for h in found.hits if not accepted(h, found, cfg))
    for key, n in sorted((listed_hits - have).items()):
        out.append(f"stale line (x{n}) in {TODO_NAME}: {' | '.join(key)}. The value is no longer a "
                   "hit (converted, tagged or moved): delete the line.")
    for path, anchor in anchors:
        if path.startswith(NON_SHIPPING):
            out.append(f"{TODO_NAME}: `{path} | {anchor[:40]}` names a path that never ships")
            continue
        p = cfg.root / path
        if not p.is_file() or anchor not in p.read_text(encoding="utf-8", errors="replace"):
            out.append(f"stale anchor in {TODO_NAME}: {path} | {anchor}. The text is gone from the "
                       "file (converted?): delete the line.")
    return out


def check_tags(cfg: Config) -> list[str]:
    """Every tag is valid and on the registry, and the registry holds nothing else."""
    found = collect(cfg)
    out = list(found.problems)
    have: Counter[tuple[str, ...]] = Counter()
    for hit in found.hits:
        if hit.tag is None:
            continue
        problem = tag_problem(hit, found, cfg)
        if problem:
            out.append(f"{hit.where()}: the `{hit.tag.kind}` tag does not count: {problem}.")
        else:
            have[(*hit.key(), hit.tag.kind, hit.tag.reason)] += 1
    registry = tag_lines(cfg)
    for key, n in sorted((have - registry).items()):
        out.append(f"tag not in {TAGS_NAME} (x{n}): {' | '.join(key)}. Add this line.")
    for key, n in sorted((registry - have).items()):
        out.append(f"{TAGS_NAME} line with no matching tag (x{n}): {' | '.join(key)}. Delete it, "
                   "or fix the tag so its kind and reason are the line's.")
    return out


# --------------------------------------------------------------------------------------------
# The read check (080 § The guard learns a fifth kind and to check reads)
#
# `check_new` sees only that a literal left, and every test passes at the default (a limit's default
# equals today's value), so a converted value that is declared and never read, or read once at
# import, passes everything. This check reads the code for the ways a conversion goes wrong.

#: A call that asks for a limit's value. `value`, `at_start` and `value_in_force` always do;
#: `_value` is also the name of ordinary helpers, so it counts only when an argument names a limit.
READ_CALLS = frozenset({"value", "_value", "at_start", "value_in_force"})
SURE_READS = frozenset({"value", "at_start", "value_in_force"})
DECLARING = frozenset({"Limit", "_limit"})  # a call whose first string argument names a limit
LIMIT_TABLES = frozenset({"defaults", "minima", "maxima"})  # a declaration's own figures
LIMITS_MODULE = "limits.py"  # the library file whose clamp may read them
NAME_PATTERN = re.compile(r"[a-z0-9_]{1,40}")
OWN_NAME = "name"


@dataclass(frozen=True)
class Declared:
    path: str
    line: int
    name: str  # the limit's name, as the page and the wire hold it
    ident: str  # the constant it is assigned to, or "" when it is built inline
    applies: str  # `live` unless the declaration says another


@dataclass(frozen=True)
class Read:
    """One call that asks for a limit's value, as the check needs to see it."""

    path: str
    line: int
    col: int
    callee: str
    names: frozenset[str]  # string literals among its arguments
    idents: frozenset[str]  # constants among its arguments, `X` of `X.name` included
    generic: bool  # an argument that is `<something>.name` whose something is not a constant
    in_init: bool
    at_import: bool  # module or class level, or a default argument: run once, before any answer

    def key(self) -> tuple[int, int]:
        return (self.line, self.col)


@dataclass
class ReadFacts:
    path: str = ""
    declared: list[Declared] = field(default_factory=list)
    reads: list[Read] = field(default_factory=list)
    tables: list[tuple[int, str]] = field(default_factory=list)  # `.defaults[` and the like
    truthy: list[tuple[int, str, tuple[int, int]]] = field(default_factory=list)
    refs: Counter[str] = field(default_factory=Counter)  # identifier -> uses outside a listing


def first_string(call: ast.Call) -> str | None:
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value if NAME_PATTERN.fullmatch(arg.value) else None
    return None


def applies_of(call: ast.Call) -> str:
    for kw in call.keywords:
        if kw.arg == "applies" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return "live"


def read_key(node: ast.AST) -> tuple[int, int] | None:
    if isinstance(node, ast.Call) and callname(node) in READ_CALLS:
        return (node.lineno, node.col_offset)
    return None


def walk_scope(node: ast.AST) -> list[ast.AST]:
    """Every node under `node` that belongs to its own function, not to one nested in it."""
    out: list[ast.AST] = [node]
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            out += walk_scope(child)
    return out


class ReadScan(ast.NodeVisitor):
    """Walks one module and records what the read check asks of it."""

    def __init__(self, path: str, declarers: frozenset[str]) -> None:
        self.path, self.declarers = path, declarers
        self.facts = ReadFacts(path)
        self.functions: list[str] = []  # enclosing function names, innermost last
        self.deferred = 0  # inside a function or lambda body: runs when called, not at import
        self.target = ""  # the constant the statement being walked assigns to

    # scopes ---------------------------------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for default in [*node.args.defaults, *[d for d in node.args.kw_defaults if d]]:
            self.visit(default)  # a default runs where the function is defined
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.functions.append(node.name)
        self.deferred += 1
        for statement in node.body:
            self.visit(statement)
        self.deferred -= 1
        self.functions.pop()
        self.truthiness(node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in [*node.args.defaults, *[d for d in node.args.kw_defaults if d]]:
            self.visit(default)
        self.deferred += 1
        self.visit(node.body)
        self.deferred -= 1

    def visit_Module(self, node: ast.Module) -> None:
        self.generic_visit(node)
        self.truthiness(node.body)

    # declarations ---------------------------------------------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        previous, self.target = self.target, names[0] if len(names) == 1 else ""
        self.generic_visit(node)
        self.target = previous

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        previous = self.target
        self.target = node.target.id if isinstance(node.target, ast.Name) else ""
        self.generic_visit(node)
        self.target = previous

    # references and reads -------------------------------------------------------------
    def listing(self, node: ast.Tuple | ast.List | ast.Set) -> None:
        """A constant that is a direct element of a collection is listed, not used."""
        for element in node.elts:
            if isinstance(element, (ast.Name, ast.Attribute)):
                continue
            self.visit(element)

    visit_Tuple = visit_List = visit_Set = listing

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.facts.refs[node.id] += 1

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self.facts.refs[node.attr] += 1
        self.generic_visit(node)

    def visit_Starred(self, node: ast.Starred) -> None:
        if not isinstance(node.value, (ast.Name, ast.Attribute)):
            self.visit(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        v = node.value
        if (isinstance(v, ast.Attribute) and v.attr in LIMIT_TABLES
                and posixpath.basename(self.path) != LIMITS_MODULE):
            self.facts.tables.append((node.lineno, v.attr))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = callname(node)
        if name in DECLARING or name in self.declarers:
            limit = first_string(node)
            if limit is not None:
                self.facts.declared.append(Declared(
                    self.path, node.lineno, limit, self.target, applies_of(node)))
            self.target = ""  # a constant is the limit only when the call is the whole value
        if name in READ_CALLS:
            self.record_read(node, name)
        self.generic_visit(node)

    def record_read(self, node: ast.Call, name: str) -> None:
        names: set[str] = set()
        idents: set[str] = set()
        generic = False
        for arg in [*node.args, *[kw.value for kw in node.keywords]]:
            for inner in ast.walk(arg):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    names.add(inner.value)
                elif isinstance(inner, ast.Attribute) and inner.attr == OWN_NAME:
                    owner = opname(inner.value)
                    if owner.isupper():
                        idents.add(owner)
                    else:
                        generic = True
                elif isinstance(inner, (ast.Name, ast.Attribute)) and opname(inner).isupper():
                    idents.add(opname(inner))
        self.facts.reads.append(Read(
            self.path, node.lineno, node.col_offset, name, frozenset(names), frozenset(idents),
            generic, bool(self.functions) and self.functions[-1] == "__init__",
            not self.deferred))

    # a read used as a condition ---------------------------------------------------------
    def truthiness(self, body: list[ast.stmt]) -> None:
        """A limit's value used as a truth value turns a 0 into *no limit*. Within one function
        (or the module's own statements), a name assigned from a read and then used as an `or` or
        `and` operand, under `not`, or as an `if`, `while`, conditional-expression or `assert`
        test, and a read used so directly."""
        nodes = [n for statement in body
                 if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                 for n in walk_scope(statement)]
        tainted: dict[str, tuple[int, int]] = {}
        for n in nodes:
            targets: list[ast.AST] = []
            if isinstance(n, ast.Assign):
                targets, value = list(n.targets), n.value
            elif isinstance(n, ast.AnnAssign) and n.value is not None:
                targets, value = [n.target], n.value
            else:
                continue
            key = read_key(value)
            if key is not None:
                tainted.update({opname(t): key for t in targets if opname(t)})

        def source(x: ast.AST) -> tuple[int, int] | None:
            key = read_key(x)
            if key is not None:
                return key
            if isinstance(x, (ast.Name, ast.Attribute)):
                return tainted.get(opname(x))
            return None

        for n in nodes:
            tested: list[ast.AST] = []
            if isinstance(n, ast.BoolOp):
                tested = list(n.values)
            elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
                tested = [n.operand]
            elif isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert)):
                tested = [n.test]
            for x in tested:
                key = source(x)
                if key is not None:
                    self.facts.truthy.append((getattr(x, "lineno", 0), opname(x) or callname(x),
                                              key))


@functools.cache
def read_facts(rel: str, source: str, declarers: frozenset[str]) -> ReadFacts | None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None  # `check_new` already reports a file that does not parse
    scan = ReadScan(rel, declarers)
    scan.visit(tree)
    return scan.facts


def read_problems(cfg: Config) -> list[str]:
    """Every way a declared limit is not read, or is read wrongly. Each names the file, the line
    and the limit. An empty list is a pass.

    - **a declared limit with no read.** Its name is a string argument of a read call (`value`,
      `at_start`, `value_in_force`, and `_value` where an argument names a limit), or its constant
      is (as `X` or `X.name`). Where a repository reads through one helper that takes the limit
      (`Budget(LIMIT)` and `value(limit.name)` inside it), the limit counts as read when such a
      generic read exists and the constant is used (passed, indexed, returned) anywhere but as an
      element of a listing: a constant that appears only in its declaration and a tuple is
      declared and never read;
    - **a read at import**: at module or class level, or in a default argument, where no answer
      from Q Branch exists yet, so the value is the default for ever;
    - **`.defaults[`, `.minima[` or `.maxima[` outside `limits.py`** (and the paths in
      `Config.table_readers`): a figure from the declaration where the value in force belongs;
    - **a read in an `__init__` for a limit declared `live`**: the page then says nothing about a
      restart that is in fact needed (`restart` limits are read with `at_start`);
    - **a read used as a truth value** (`x or ...`, `if not x`): a 0 would mean *no limit*.
    """
    declarers = frozenset(cfg.declarers)
    facts: list[ReadFacts] = []
    for rel in listed(cfg.root):
        p = cfg.root / rel
        if skipped(rel, cfg) or not p.is_file():
            continue
        head = first_line(p) if not p.suffix else ""
        if p.suffix.lower() != ".py" and not (head.startswith("#!") and "python" in head):
            continue
        try:
            source = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = read_facts(rel, source, declarers)
        if found is not None:
            facts.append(found)
    declared = [d for f in facts for d in f.declared]
    by_name = {d.name: d for d in declared}
    idents = {d.ident for d in declared if d.ident}
    # A call counts as a read when it is one of the sure ones or an argument names a limit.
    reads = {(*r.key(), r.path): r for f in facts for r in f.reads
             if r.callee in SURE_READS or r.generic or (r.names & by_name.keys())
             or (r.idents & idents)}
    refs: Counter[str] = Counter()
    for f in facts:
        refs.update(f.refs)
    generic_read = any(r.generic for r in reads.values())
    out: list[str] = []
    for d in declared:
        literal = any(d.name in r.names for r in reads.values())
        owned = bool(d.ident) and any(d.ident in r.idents for r in reads.values())
        # A constant's own declaration is a store, never a load, so any load is a use.
        wired = generic_read and (refs[d.ident] > 0 if d.ident else True)
        if d.name in cfg.unread:
            if literal or owned or wired:
                out.append(f"{d.path}:{d.line}: the limit `{d.name}` is now read; delete it from "
                           "Config.unread")
        elif not (literal or owned or wired):
            out.append(f"{d.path}:{d.line}: the limit `{d.name}` is declared and never read: no "
                       "read call names it, and no read takes it through a helper")
    for name in cfg.unread:
        if name not in by_name:
            out.append(f"Config.unread names `{name}`, which no `Limit` declares: delete it")
    for r in reads.values():
        named = sorted({n for n in r.names if n in by_name}
                       | {d.name for d in declared if d.ident and d.ident in r.idents})
        if r.at_import:
            which = f" the limit `{named[0]}`" if named else " a limit"
            out.append(f"{r.path}:{r.line}: a read of{which} at module or class level or in a "
                       "default argument, run once before Q Branch has answered, so it is the "
                       "default for ever; read it where it is used")
        if r.in_init and r.callee != "at_start":
            live = [by_name[n] for n in named if by_name[n].applies == "live"]
            if live or r.generic:
                which = live[0].name if live else "a limit"
                out.append(f"{r.path}:{r.line}: `{which}` is read in an `__init__` but is `live`: "
                           "it is then read once, and a change on the page needs a restart that "
                           "the page does not say; read it where it is used, or declare it "
                           "`restart` and read it with `at_start`")
    for f in facts:
        if f.path not in cfg.table_readers:
            for line, table in f.tables:
                out.append(f"{f.path}:{line}: `.{table}[...]` read outside `{LIMITS_MODULE}`: that "
                           "is the figure the limit was declared with, not the value in force; "
                           "read it with `value(...)`")
        for line, what, key in f.truthy:
            if (*key, f.path) in reads:
                out.append(f"{f.path}:{line}: the value of a limit (`{what}`) used as a "
                           "condition: a limit of 0 would mean no limit; compare it (`> 0`, "
                           "`is None`)")
    return sorted(out, key=lambda m: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", m)])


def summary_line(guard_dir: Path) -> str:
    """The one line every test run ends with."""
    todo = read_lines(guard_dir / TODO_NAME)
    misses = sum(1 for ln in todo if len(ln.split(" | ")) != 4)
    kinds = Counter(ln.split(" | ", 5)[4] for ln in read_lines(guard_dir / TAGS_NAME)
                    if len(ln.split(" | ", 5)) == 6)  # fmt: skip
    tags = ", ".join(f"{k} {kinds[k]}" for k in sorted(KINDS))
    hits = len(todo) - misses
    return (f"limits not yet converted: {len(todo)} (hits {hits}, hand-found {misses}); "
            f"tags: {tags}")


def cli(cfg: Config, argv: list[str]) -> int:
    if "--emit" in argv:
        found = collect(cfg)
        for hit in found.hits:
            if not accepted(hit, found, cfg):
                print(" | ".join(hit.key()))
        return 0
    if "--strict" in argv:
        lines = read_lines(cfg.guard_dir / TODO_NAME)
        print("\n".join(lines))
        print(summary_line(cfg.guard_dir))
        return 1 if lines else 0
    print(summary_line(cfg.guard_dir))
    return 0


if __name__ == "__main__":
    sys.exit("run the repository's test_limitguard.py, which holds its Config")
