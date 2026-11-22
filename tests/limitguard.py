"""The limit guard (068): finds a limit-shaped value that is neither a declared `Limit` nor marked.

**This file is identical in every repository.** Nothing in it names a repository. What differs
(extra skipped paths, where non-Python files are read, the modules that read Q Branch) is a
`Config` that the repository's own `test_limitguard.py` builds and passes in. Edit the copy under
`068-guard/` in the coordinator's notes, never one repository's file alone.

**What it reads.** Every file git lists in the working tree and does not ignore (tracked and
untracked, so a new file is read in the run that gates its commit), minus the skipped paths.
Python is every `*.py` and every extensionless file whose first line names python; it is read with
the `ast` module, and a file that does not parse is a problem, never a skip. Other files (the
household pages' JavaScript, compose, systemd units, shell, Dockerfiles, the Caddyfile) are read by
pattern, only under `Config.text_roots`.

**What it flags** (a literal other than 0, 1 and -1; literal arithmetic such as `64 << 20` counts
as one literal):

- any upper-case constant at module or class level, tuple targets paired with their values;
- any name, keyword or default argument carrying a limit word (`WORDS`), whatever the expression
  around the literal (`deadline = time.monotonic() + 30`, `timeout=httpx.Timeout(20.0)`);
- `.get("<limit word>", N)`, a literal compared with a limit-worded name or with `len(...)`,
  the limit argument of the calls in `CALL_ARGS` (tuples walked), `range(N)`, a slice bound of 16
  or more.

A literal inside a call named exactly `Limit` or `_limit` is a declaration and is never flagged.

**How a value is allowed to stay.** A comment `# limit-ok: <kind> — <reason of three words or
more>` (`//` in JavaScript) on the value's own lines or on a comment-only line directly above it.
The kinds are `KINDS`; `maximum`, `reply-bound` and `not-a-limit` each have a condition (see
`tag_problem`). Every tag is also a line in `limitguard-tags.txt`, so a new escape is an edit to
two files and Robert reads the registry in one place.

**What is not converted yet** is `limitguard-todo.txt`: a detector hit as `path | scope | what |
value` (no line number, so an edit elsewhere does not move it) or a hand-found miss as `path |
anchor`, an anchor being a piece of the line's text. The list is a multiset.

Run `python tests/test_limitguard.py --strict` (or `python -m tests.test_limitguard --strict`
where `tests/` is a package) to list everything still on the list and exit 1; `--emit` prints
every unmarked hit as a todo line.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import io
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
KINDS = ("maximum", "reply-bound", "format", "not-a-limit")
STRONG = frozenset(
    "timeout timeouts seconds wall deadline max cap caps bytes retries attempts limit".split()
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
    "readline": (0,), "fetchmany": (0,), "join": (0,),
}  # fmt: skip
# the calls whose arguments count inside an upper-case constant (a refusal's HTTP status does not)
TIME_CALLS = frozenset("timedelta Timeout Limits timeout frozenset set tuple dict list".split())

MALFORMED = "a `limit-ok` comment that does not read `limit-ok: <kind> — <reason>`"
STRAY = "a `limit-ok` tag with no limit on its line or on the line below it"
TAG = re.compile(r"limit-ok:\s*(?P<kind>[A-Za-z-]+)\s*(?:—|--|-|:)\s*(?P<reason>.*?)\s*$")
TAG_WORD = re.compile(r"limit-ok\b")


def words(name: str) -> set[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return {w for w in re.split(r"[_\-\s.]+", name.lower()) if w}


def limitish(name: str | None) -> bool:
    return bool(name) and bool(words(name or "") & WORDS)


@dataclass(frozen=True)
class Config:
    """What differs per repository. `root` is the repository root, `guard_dir` holds the two text
    files. `skip`: path prefixes (ending in `/`) or `fnmatch` patterns from the root, added to
    `DEFAULT_SKIP`. `text_roots`: path prefixes under which non-Python files are read.
    `reply_bound`: the paths of modules that read Q Branch, the only places `reply-bound` counts."""

    root: Path
    guard_dir: Path
    skip: tuple[str, ...] = ()
    text_roots: tuple[str, ...] = ()
    reply_bound: frozenset[str] = frozenset()


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
    return f"{text[:80]}...#{hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:12]}"


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
        text = shorten(re.sub(r"\s+", "", value if isinstance(value, str) else ast.unparse(value)))
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
            self.add(node, lits, "range(N)", ",".join(ast.unparse(a) for a in node.args), "")
        if (name == "get" and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str) and limitish(node.args[0].value)):
            self.add(node.args[1], literals(node.args[1]), f".get({node.args[0].value})",
                     node.args[1], node.args[0].value)
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
TEXT_SUFFIXES = frozenset(".js .mjs .cjs .yml .yaml .service .timer .socket .sh .conf".split())
JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs"})
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
                value = shorten(re.sub(r"\s+", "", val))
                hits.append(Hit(path, n, "-", f"{kind} {name}".strip(), value, name, tag))
    for n in sorted(set(tags) - used):
        problems.append(f"{path}:{n}: {STRAY}")
    return hits, problems


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
        if skipped(rel, cfg) or p.is_symlink() or not p.is_file():
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
        if python:
            hits, maxima, problems = python_hits(rel, source)
            found.maxima[rel] = maxima
        else:
            hits, problems = text_hits(rel, source, suffix in JS_SUFFIXES)
        found.hits += hits
        found.problems += problems
    return found


# --------------------------------------------------------------------------------------------
# The two lists


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]  # fmt: skip


def todo_lines(cfg: Config) -> tuple[Counter[tuple[str, ...]], list[tuple[str, str]]]:
    """The hit lines (a multiset of 4-field keys) and the hand-found anchors (path, anchor)."""
    hits: Counter[tuple[str, ...]] = Counter()
    anchors: list[tuple[str, str]] = []
    for line in read_lines(cfg.guard_dir / TODO_NAME):
        parts = line.split(" | ")
        if len(parts) == 4:
            hits[tuple(parts)] += 1
        else:
            path, _, anchor = line.partition(" | ")
            anchors.append((path, anchor))
    return hits, anchors


def tag_lines(cfg: Config) -> Counter[tuple[str, ...]]:
    out: Counter[tuple[str, ...]] = Counter()
    for line in read_lines(cfg.guard_dir / TAGS_NAME):
        out[tuple(line.split(" | ", 5))] += 1
    return out


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
    if tag.kind == "not-a-limit" and words(hit.name) & STRONG:
        strong = sorted(words(hit.name) & STRONG)[0]
        return f"`not-a-limit` is refused on a name carrying `{strong}`"
    return None


def accepted(hit: Hit, found: Found, cfg: Config) -> bool:
    return hit.tag is not None and tag_problem(hit, found, cfg) is None


HOW_OUT = (
    "Declare it as a Limit (Q Branch's Limits page; 059), or, if it is a maximum, a bound on Q "
    "Branch's own reply, a format fact or not a limit at all, mark it in place with "
    "`# limit-ok: <kind> — <why, three words or more>` and add the same line to "
    f"{TAGS_NAME}. Adding it to {TODO_NAME} is not a way out."
)


def check_new(cfg: Config) -> list[str]:
    """Every hit that is neither accepted by a tag nor on the list: a new limit."""
    found = collect(cfg)
    listed_hits, _ = todo_lines(cfg)
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
    listed_hits, anchors = todo_lines(cfg)
    have: Counter[tuple[str, ...]] = Counter(
        h.key() for h in found.hits if not accepted(h, found, cfg))
    out = []
    for key, n in sorted((listed_hits - have).items()):
        out.append(f"stale line (x{n}) in {TODO_NAME}: {' | '.join(key)}. The value is no longer a "
                   "hit (converted, tagged or moved): delete the line.")
    for path, anchor in anchors:
        p = cfg.root / path
        if not p.is_file() or anchor not in p.read_text(encoding="utf-8", errors="replace"):
            out.append(f"stale anchor in {TODO_NAME}: {path} | {anchor}. The text is gone from the "
                       "file (converted?): delete the line.")
    for line in read_lines(cfg.guard_dir / TODO_NAME):
        if line.startswith(NON_SHIPPING):
            out.append(f"{TODO_NAME}: `{line[:60]}` names a path that never ships")
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
    for line in read_lines(cfg.guard_dir / TAGS_NAME):
        if line.startswith(NON_SHIPPING):
            out.append(f"{TAGS_NAME}: `{line[:60]}` names a path that never ships")
    return out


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
