"""The nightly export of the recipe book: `python -m paprika_mcp.export`.

moneypenny plans/012 § 1a. Paprika keeps no history — an edit overwrites one
object — so the only undo is a copy taken before the damage. The Pi's host
script runs this once a night in a throwaway container (`start.sh paprika
export`), reads the file it leaves, and logs the result line itself; this
process only fetches and writes. restore_recipe reads the same files back,
read-only, from `/export`.

The file is `paprika-<EXPORT_DATE>.json.gz` in OUT_DIR: gzip of one UTF-8 JSON
object (FORMAT below), every non-trashed recipe with every field the library
models — photo *fields* included, never photo bytes, which the library does not
fetch — plus the categories and the meal plan from 30 days back to 90 ahead.

The date comes from the host (`EXPORT_DATE`), not from this container's clock,
so one clock names the file and sets the meal window; the container's clock is
UTC and the household's is not.
"""

import datetime
import functools
import gzip
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from typing import Any

from .tools.meal_plan import _get, meal_date
from .tools.update_recipe import FIELDS
from .utils import get_categories, get_remote

FORMAT = "paprika-export/1"
# Where the export run's read-write mount lands. Not `/export`: the serving
# container already declares `/export:ro`, and a second `-v` onto the same
# target is a duplicate mount point whose outcome compose does not document
# (the 012 § 1a+1c contract, departing from 012's text).
OUT_DIR = "/export-out"
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MEALS_BACK, MEALS_AHEAD = 30, 90
# What a valid export holds at the top level, and of what type. Restore and the
# host script's "readable retained file" both rest on this one rule.
TOP_LEVEL = {"format": str, "exported_at": str, "recipes": list, "categories": list, "meals": list}


def file_name(date: str) -> str:
    return f"paprika-{date}.json.gz"


def fields_sha256(recipe: Mapping[str, Any]) -> str:
    """The recipe's content hash over update_recipe.FIELDS, and nothing else.

    The definition is the shared contract's, byte for byte, because the host
    script compares these values between nights (`churned`) and restore uses the
    same function to decide whether a saved copy differs from now. Paprika's own
    `hash` cannot serve: measured (012 spike S6), an edit and its revert leave a
    different `hash`, and it changes case between the app and the library. The
    volatile fields — `hash`, `created`, the photo fields, `photo_url`'s
    presigned signature that changes on every read — are left out so that only a
    change a member could make, and restore could undo, counts as one.

    Computed here, in the container, from FIELDS itself, so FIELDS has one
    definition and the host script never has to know it.
    """
    chosen = {f: recipe[f] for f in FIELDS}
    text = json.dumps(chosen, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def problem(document: Any) -> str | None:
    """Why `document` is not a valid export, or None when it is.

    The contract's rule: a JSON object of this FORMAT with every top-level key
    present at its type, and at least one recipe. An export with no recipes is
    not a copy of anything, and restore must not read it as "the recipe was not
    in that copy".
    """
    if not isinstance(document, dict):
        return "it is not a JSON object"
    if document.get("format") != FORMAT:
        return f"its format is {document.get('format')!r}, not {FORMAT!r}"
    for key, kind in TOP_LEVEL.items():
        if not isinstance(document.get(key), kind):
            return f"its {key!r} is missing or not a {kind.__name__}"
    if not document["recipes"]:
        return "it holds no recipes"
    return None


def _in_window(meal: dict, day: datetime.date) -> bool:
    # A meal whose date cannot be read is kept, not dropped: the export's job is
    # to lose nothing, and a malformed date is not evidence the meal is outside
    # the window.
    try:
        when = meal_date(meal)
    except (KeyError, TypeError, ValueError):
        return True
    return day - datetime.timedelta(days=MEALS_BACK) <= when <= day + datetime.timedelta(days=MEALS_AHEAD)


def collect(remote: Any, day: datetime.date) -> dict[str, Any]:
    """The whole export as one object. Any failure to fetch raises."""
    recipes = []
    for r in remote.recipes:
        if r.in_trash:
            continue
        fields = r.as_dict()
        fields["fields_sha256"] = fields_sha256(fields)
        recipes.append(fields)
    # Sorted, so two nights' files of an unchanged book differ only in
    # `exported_at` and a person diffing them by hand sees the real change.
    recipes.sort(key=lambda r: r["uid"])
    document = {
        "format": FORMAT,
        "exported_at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "recipes": recipes,
        # get_categories raises CategoriesUnavailable rather than answering an
        # empty list, so a failed fetch fails the export instead of writing a
        # copy that says the account has no categories.
        "categories": get_categories(remote)["all"],
        "meals": [m for m in _get(remote, "meals") if _in_window(m, day)],
    }
    return document


def write_atomically(document: dict[str, Any], out_dir: str, date: str) -> str:
    """Write `document` to `<out_dir>/paprika-<date>.json.gz` all at once.

    Written as `.<name>.tmp`, fsync'd, then renamed over `<name>`: a rename is
    atomic on POSIX, so a reader — the host script, `export-fetch` streaming it
    to Walt, restore — sees the old file or the whole new one, never half. A
    same-day re-run replaces the file the same way; writing in place would
    truncate the good copy first and leave a broken one if the run died.

    The tmp file is opened with `open`, not `tempfile.mkstemp`: mkstemp creates
    mode 0600 whatever the umask, and the `backup` user reads these through the
    group (`mcpexport`, from the setgid directory). The tmp name is fixed rather
    than random so the host script's "never read a `.tmp`" rule has one shape to
    match, and a stale one from a killed run is simply overwritten.
    """
    name = file_name(date)
    tmp = os.path.join(out_dir, f".{name}.tmp")
    final = os.path.join(out_dir, name)
    try:
        with open(tmp, "wb") as raw:
            # mtime=0 keeps the gzip header free of this clock; the date is in
            # the name and `exported_at`.
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(json.dumps(document, ensure_ascii=False).encode("utf-8"))
            raw.flush()
            os.fsync(raw.fileno())
        os.rename(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # The rename is durable only once the directory is: without this a power
    # cut after a "successful" run can leave the old directory entry.
    try:
        fd = os.open(out_dir, os.O_RDONLY)
    except OSError:
        return final
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
    return final


def _one_line(error: BaseException) -> str:
    # The host script relays stderr into the journal; a traceback or a
    # multi-line message would arrive as several unrelated lines.
    return " ".join(f"{type(error).__name__}: {error}".split())


def main(
    out_dir: str = OUT_DIR,
    environ: Mapping[str, str] | None = None,
    remote_factory: Callable[[], Any] | None = None,
) -> int:
    """Run one export. 0 on success; 2 for a bad EXPORT_DATE; 1 for anything else.

    Every failure prints one line to stderr and leaves no `<name>` behind. The
    arguments exist for tests; the entry point passes none, so the directory
    is always OUT_DIR in production — an environment variable would let a
    stray setting point the write somewhere else.
    """
    # 0640: readable by the `mcpexport` group (the `backup` user), by nobody
    # else, writable by nobody but this uid.
    os.umask(0o027)
    env = os.environ if environ is None else environ
    date = env.get("EXPORT_DATE", "")
    # fullmatch, not match with `$`: `$` also matches before a trailing newline.
    day = None
    if DATE.fullmatch(date):
        try:
            day = datetime.date.fromisoformat(date)
        except ValueError:
            day = None
    if day is None:
        print(f"paprika export: refused: EXPORT_DATE must be YYYY-MM-DD, got {date!r}", file=sys.stderr)
        return 2
    try:
        # Quietly: the export container has no `/want`, so it could not raise the flag. Before the
        # first sync the read still fails in the vault's own words, which is a failed run.
        remote = (remote_factory or functools.partial(get_remote, quiet=True))()
        document = collect(remote, day)
        reason = problem(document)
        if reason:
            # Not written: an invalid file would sit in the directory as an
            # export nobody can use, and the host script reports the run as
            # failed either way.
            print(f"paprika export: failed: the export would be invalid ({reason}); nothing was written", file=sys.stderr)
            return 1
        write_atomically(document, out_dir, date)
    except Exception as e:
        print(f"paprika export: failed: {_one_line(e)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
