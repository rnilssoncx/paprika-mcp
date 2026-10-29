"""Restore a recipe from a nightly export (moneypenny plans/012 § 1c).

*Writes go through the MCP*, so undoing one is a tool too, not a script run on
the Pi. It reads the exports `paprika_mcp.export` writes, from EXPORT_DIR —
mounted read-only into the serving container — and uploads one recipe's
`update_recipe.FIELDS` from the chosen night over the current ones.

What it never writes: `uid`, `hash`, `created`, `in_trash`, or the photo fields.
The upload carries the recipe as it is now with only FIELDS replaced, so the
photo a member sees is the one it has today (which is why the answer can say
"Photo unchanged"), and `hash` is the library's to recompute on upload, as for
every edit.

Split the way meal_plan is: `decide_restore` compares a saved copy with now and
returns what to write; `apply_restore` is the only place an upload happens and
carries the FIELDS-only guard; the reading of copies and the wording of the
answer are pure functions over plain inputs. So each guard is tested with
hand-built decisions and fixture files, not through a scripted upload.

**The absence rule.** An empty `/export`, an unmounted one, or a directory of
files none of which reads, is *can't tell* — never "nothing to restore" and
never "the same as before". A file that does not read is named as unreadable,
not skipped as if it were not there.
"""

import gzip
import json
import os
import re
from typing import Any

from mcp.types import TextContent

# The module, not its names: export imports tools.update_recipe, which runs
# this package's __init__, which imports this file — so under
# `python -m paprika_mcp.export` export is only part-initialised here, and its
# names are looked up when a call runs instead.
from .. import export
from ..utils import AmbiguousTitle, find_by_title, get_remote
from .update_recipe import FIELDS, TEXT_FIELDS, _upload

EXPORT_DIR = "/export"
# Only these names are copies; `.tmp` files (a write in progress, or one that
# died) and the other role's files never match.
COPY_NAME = re.compile(r"paprika-([0-9]{4}-[0-9]{2}-[0-9]{2})\.json\.gz")

# The words — moneypenny plans/012 § 1c's table, verbatim; tests assert them.
RESTORED = 'Restored "{title}" to how it was on {date}. Photo unchanged.'
ALREADY_SAME = '"{title}" is already the same as on {date}; nothing was changed.'
DIFFERING = '"{title}" has {k} saved copies that differ from now: {dates}. Say which date to restore.'
NONE_DIFFER = '"{title}" is the same in every readable saved copy ({first} to {last}).'
NO_READABLE = 'No saved copies are readable here, so I can\'t tell what "{title}" used to be.'
SOME_UNREADABLE = " Couldn't read {n} saved copies: {dates}."
NO_SUCH_DATE = "There is no saved copy from {date}. Saved copies: {dates}."
NOT_IN_COPY = '"{title}" was not in the {date} copy — it may have been added after.'
COPY_UNREADABLE = "Couldn't read the {date} copy: {error}. Nothing was changed."
TRASHED = '"{title}" is in Paprika\'s trash; restore it from the trash in the app first.'
BAD_DATE = '"{date}" is not a date; give it as YYYY-MM-DD.'

# Not in 012's table, which has no row for them. A recipe in no readable copy
# (added since the last export) would otherwise fall to NONE_DIFFER, which
# claims copies of it exist.
NOT_IN_ANY = '"{title}" was not in any readable saved copy ({first} to {last}) — it may have been added after.'
NO_SUCH_RECIPE = 'No recipe has the id or title "{recipe}".'
AMBIGUOUS = '{n} recipes are titled "{recipe}": {ids}. Restore one by id.'
NO_RECIPE_GIVEN = "Give the recipe's id or its title."


class Unreadable(Exception):
    """A saved copy, or one recipe in it, could not be read; the message says why."""


def _text(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=message)]


# ---- pure: reading copies ------------------------------------------------------


def copy_dates(export_dir: str) -> list[str]:
    """The dates of every saved copy present, readable or not, oldest first.

    A directory that cannot be listed — not mounted, not permitted — has no
    dates, which every caller reads as *can't tell*, never as "no copies were
    ever taken".
    """
    try:
        names = os.listdir(export_dir)
    except OSError:
        return []
    return sorted(m.group(1) for n in names if (m := COPY_NAME.fullmatch(n)))


def read_copy(path: str) -> dict[str, dict]:
    """One saved copy's recipes, by uid. Unreadable for any failure to read it.

    Anything that stops the read — a missing file, a truncated gzip, a bad
    deflate stream (`zlib.error`, not an OSError), JSON that is not an export —
    is the same answer: this copy cannot be read. It is never "the recipe was
    not in it".
    """
    try:
        with gzip.open(path, "rb") as f:
            document = json.loads(f.read().decode("utf-8"))
    except Exception as e:
        raise Unreadable(" ".join(f"{type(e).__name__}: {e}".split())) from None
    reason = export.problem(document)
    if reason:
        raise Unreadable(f"it is not a valid export: {reason}")
    return {r["uid"]: r for r in document["recipes"] if isinstance(r, dict) and isinstance(r.get("uid"), str)}


def read_all(export_dir: str) -> tuple[dict[str, dict[str, dict]], dict[str, str]]:
    """Every copy present: (readable copies by date, unreadable dates → why)."""
    readable, unreadable = {}, {}
    for date in copy_dates(export_dir):
        try:
            readable[date] = read_copy(os.path.join(export_dir, export.file_name(date)))
        except Unreadable as e:
            unreadable[date] = str(e)
    return readable, unreadable


# ---- pure: deciding ----------------------------------------------------------------


def stored_fields(stored: dict) -> dict[str, Any]:
    """FIELDS from a saved recipe, checked before any of it can be uploaded.

    A missing field is refused, not defaulted: `stored.get("ingredients")`
    would upload None over the member's ingredients, which is the damage restore
    exists to undo.
    """
    out = {}
    for f in FIELDS:
        if f not in stored:
            raise Unreadable(f"its recipe {stored.get('uid')} has no {f!r}")
        value = stored[f]
        ok = (
            (f in TEXT_FIELDS and (value is None or isinstance(value, str)))
            or (f == "categories" and isinstance(value, list) and all(isinstance(c, str) for c in value))
            or (f == "rating" and isinstance(value, int) and not isinstance(value, bool))
        )
        if not ok:
            raise Unreadable(f"its recipe {stored.get('uid')} has a malformed {f!r}")
        out[f] = value
    return out


def decide_restore(current: Any, stored: dict) -> dict[str, Any] | None:
    """What restoring `stored` over `current` would write, or None if nothing.

    Returns exactly FIELDS. Compared by `fields_sha256`, the export's own
    definition of a change, so a copy that differs only in `hash`, `created` or
    a photo field — which Paprika changes on its own (012 spike S6) — is the
    same recipe, both here and in the host script's churn count.
    """
    wanted = stored_fields(stored)
    now = {f: getattr(current, f) for f in FIELDS}
    if export.fields_sha256(wanted) == export.fields_sha256(now):
        return None
    return wanted


def apply_restore(remote: Any, current: Any, decision: dict[str, Any], date: str) -> list[TextContent]:
    """Upload `decision` over `current`: FIELDS and only FIELDS.

    Iterates FIELDS, not the decision, so whatever else a decision carries —
    `uid`, `hash`, `in_trash`, a photo — never reaches the recipe. `uid` alone
    would upload a second copy of the recipe under another id.
    """
    # The § 1b write budget's spend() goes here: after every refusal and after
    # the already-the-same check, so neither is counted.
    for f in FIELDS:
        setattr(current, f, decision[f])
    return _upload(remote, current, RESTORED.format(title=current.name, date=date))


# ---- pure: the no-date answer ---------------------------------------------------------


def describe_copies(current: Any, readable: dict[str, dict[str, dict]], unreadable: dict[str, str]) -> str:
    """Which saved copies differ from now, for a member choosing a date.

    Writes nothing. The listing is what shows a member, before they choose, that
    restoring a date undoes every edit since it.
    """
    unreadable = dict(unreadable)
    differing, holding = [], []
    for date in sorted(readable):
        stored = readable[date].get(current.uid)
        if stored is None:
            continue
        try:
            changed = decide_restore(current, stored) is not None
        except Unreadable as e:
            unreadable[date] = str(e)
            continue
        holding.append(date)
        if changed:
            differing.append(date)
    title = current.name
    readable_dates = sorted(d for d in readable if d not in unreadable)
    if not readable_dates:
        text = NO_READABLE.format(title=title)
    elif not holding:
        text = NOT_IN_ANY.format(title=title, first=readable_dates[0], last=readable_dates[-1])
    elif differing:
        text = DIFFERING.format(title=title, k=len(differing), dates=", ".join(differing))
    else:
        text = NONE_DIFFER.format(title=title, first=holding[0], last=holding[-1])
    if unreadable:
        text += SOME_UNREADABLE.format(n=len(unreadable), dates=", ".join(sorted(unreadable)))
    return text


# ---- the handler ------------------------------------------------------------------------


def restore_recipe_tool(args: dict[str, Any]) -> list[TextContent]:
    wanted = (args.get("recipe") or "").strip()
    date = args.get("date") or None

    # Checked before anything else, and before it can become part of a path:
    # `paprika-{date}.json.gz` with date "../x" names a file outside the
    # directory. fullmatch, not `$`, which also matches before a trailing newline.
    if date is not None and not (isinstance(date, str) and export.DATE.fullmatch(date)):
        return _text(BAD_DATE.format(date=date))

    if not wanted:
        # An empty title would match every recipe whose name is empty.
        return _text(NO_RECIPE_GIVEN)

    remote = get_remote()
    recipes = list(remote.recipes)

    # A uid first, over every recipe including the trash, so that a trashed one
    # is refused by name rather than reported missing. A title goes through the
    # lookup read_recipe uses, which skips the trash — so a title shared with a
    # trashed twin finds the live recipe.
    current = next((r for r in recipes if r.uid == wanted), None)
    if current is None:
        try:
            current = find_by_title(recipes, wanted)
        except AmbiguousTitle as e:
            return _text(AMBIGUOUS.format(n=len(e.matches), recipe=wanted, ids=", ".join(r.uid for r in e.matches)))
    if current is None:
        return _text(NO_SUCH_RECIPE.format(recipe=wanted))
    if current.in_trash:
        # Restoring fields onto a trashed recipe would upload it with
        # in_trash still set: an edit nobody can see, reported as a restore.
        return _text(TRASHED.format(title=current.name))

    if date is None:
        readable, unreadable = read_all(EXPORT_DIR)
        return _text(describe_copies(current, readable, unreadable))

    present = copy_dates(EXPORT_DIR)
    if not present:
        # An empty or unmounted directory says nothing about that date.
        return _text(NO_READABLE.format(title=current.name))
    if date not in present:
        return _text(NO_SUCH_DATE.format(date=date, dates=", ".join(present)))
    try:
        stored = read_copy(os.path.join(EXPORT_DIR, export.file_name(date))).get(current.uid)
        if stored is None:
            return _text(NOT_IN_COPY.format(title=current.name, date=date))
        decision = decide_restore(current, stored)
    except Unreadable as e:
        return _text(COPY_UNREADABLE.format(date=date, error=e))
    if decision is None:
        return _text(ALREADY_SAME.format(title=current.name, date=date))
    return apply_restore(remote, current, decision, date)


TOOL_DEFINITION = {
    "name": "restore_recipe",
    "description": (
        "Put a recipe back the way it was in a nightly saved copy, undoing every edit made since "
        "that date. Without a date it changes nothing and lists the dates whose saved copy differs "
        "from the recipe now — call it that way first, and show the member the dates before "
        "restoring. Restores the text fields, categories and rating; the photo is left as it is. "
        "Refuses a recipe in Paprika's trash."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "recipe": {"type": "string", "description": "The recipe's id, or its exact title."},
            "date": {
                "type": "string",
                "description": "YYYY-MM-DD of the saved copy to restore. Omit to list the dates that differ.",
            },
        },
        "required": ["recipe"],
    },
}
