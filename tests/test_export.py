"""The nightly export, `python -m paprika_mcp.export` (moneypenny plans/012 § 1a).

The file is a contract with three other programs — the Pi's host script, Walt's
puller and restore_recipe — so the tests check the shared contract's rules
from the outside: the file gunzips, parses, carries every key at its type, and
`fields_sha256` is the contract's formula byte for byte. Row 22 of 012's table
is the atomic write: a run that dies leaves no `<name>`, and never a broken one
over a good one.
"""

import dataclasses
import datetime
import gzip
import hashlib
import inspect
import json
import os
import stat
import subprocess
import sys

import pytest
from fakes import RecipeRemote, category
from paprika_recipes.remote import RemoteRecipe

from paprika_mcp import export
from paprika_mcp.tools.update_recipe import FIELDS

DAY = "2026-10-28"
NAME = f"paprika-{DAY}.json.gz"

ROAST = {"uid": "7F3A-0C", "name": "Roast chicken", "ingredients": "1 chicken", "categories": ["C-DIN"], "rating": 4, "photo": "p.jpg", "photo_hash": "ph", "photo_url": "https://example.invalid/p?sig=1", "image_url": "img", "hash": "h1", "created": "2026-10-01 10:00:00"}
BRULEE = {"uid": "D1-5E", "name": "Crème brûlée", "ingredients": "cream", "categories": ["C-DES"], "rating": 5}
TRASHED = {"uid": "0A-77", "name": "Old roast", "in_trash": True}


def meal(day, name="Dinner"):
    return {"uid": f"M-{day}", "date": f"{day} 00:00:00", "name": name}


@pytest.fixture(autouse=True)
def keep_umask():
    # main() sets the process umask, as it should in its own process; put the
    # test runner's back.
    old = os.umask(0o022)
    os.umask(old)
    yield
    os.umask(old)


def remote(*recipes, meals=()):
    r = RecipeRemote([RemoteRecipe(**x) for x in recipes], [category("C-DIN", "Dinner"), category("C-DES", "Dessert")])
    r.meals = list(meals)
    return r


def run(tmp_path, source, date=DAY):
    return export.main(out_dir=str(tmp_path), environ={} if date is None else {"EXPORT_DATE": date}, remote_factory=lambda: source)


def read(tmp_path, name=NAME):
    return json.loads(gzip.decompress((tmp_path / name).read_bytes()).decode("utf-8"))


# ---- the file ----


def test_the_export_is_a_valid_export_by_the_contracts_rule(tmp_path):
    assert run(tmp_path, remote(ROAST, BRULEE, TRASHED)) == 0
    document = read(tmp_path)
    assert document["format"] == "paprika-export/1"
    for key, kind in {"exported_at": str, "recipes": list, "categories": list, "meals": list}.items():
        assert isinstance(document[key], kind), key
    assert len(document["recipes"]) > 0
    assert datetime.datetime.fromisoformat(document["exported_at"]).utcoffset() is not None
    assert os.listdir(tmp_path) == [NAME]


def test_every_live_recipe_is_exported_with_every_model_field(tmp_path):
    run(tmp_path, remote(ROAST, BRULEE, TRASHED))
    recipes = read(tmp_path)["recipes"]
    assert [r["uid"] for r in recipes] == ["7F3A-0C", "D1-5E"]
    model = {f.name for f in dataclasses.fields(RemoteRecipe)}
    assert all(set(r) == model | {"fields_sha256"} for r in recipes)
    # The photo fields as the library has them; there are no bytes to include.
    roast = recipes[0]
    assert (roast["photo"], roast["photo_hash"], roast["photo_url"], roast["image_url"]) == ("p.jpg", "ph", "https://example.invalid/p?sig=1", "img")
    assert all(r["fields_sha256"] == export.fields_sha256(r) for r in recipes)


def test_the_categories_and_the_meals_from_30_days_back_to_90_ahead(tmp_path):
    meals = [meal("2026-09-27"), meal("2026-09-28"), meal("2027-01-26"), meal("2027-01-27"), {"uid": "M-bad", "date": "someday", "name": "?"}]
    run(tmp_path, remote(ROAST, meals=meals))
    document = read(tmp_path)
    assert [c["uid"] for c in document["categories"]] == ["C-DIN", "C-DES"]
    # A meal whose date does not read is kept: the export loses nothing.
    assert [m["uid"] for m in document["meals"]] == ["M-2026-09-28", "M-2027-01-26", "M-bad"]


def test_the_file_is_readable_by_the_group_and_nobody_else(tmp_path):
    os.umask(0o000)
    run(tmp_path, remote(ROAST))
    assert stat.S_IMODE((tmp_path / NAME).stat().st_mode) == 0o640


def test_a_same_day_rerun_replaces_the_file(tmp_path):
    run(tmp_path, remote(ROAST))
    run(tmp_path, remote(ROAST, BRULEE))
    assert len(read(tmp_path)["recipes"]) == 2
    assert os.listdir(tmp_path) == [NAME]


def test_the_entry_point_writes_to_export_out_by_default():
    assert export.OUT_DIR == "/export-out"
    assert inspect.signature(export.main).parameters["out_dir"].default == "/export-out"


def test_python_dash_m_runs_the_export_entry_point(tmp_path):
    # A bad date is refused before any credential or network is touched, so
    # this proves `python -m paprika_mcp.export` reaches main() hermetically.
    result = subprocess.run([sys.executable, "-m", "paprika_mcp.export"], env={"EXPORT_DATE": "bad", "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=30)
    assert (result.returncode, result.stdout, result.stderr) == (2, "", "paprika export: refused: EXPORT_DATE must be YYYY-MM-DD, got 'bad'\n")


# ---- fields_sha256 ----


def test_fields_sha256_is_the_contracts_formula_byte_for_byte():
    # Written out by hand, not with json.dumps: sorted keys, no spaces, and the
    # accents as UTF-8 rather than \\u escapes (ensure_ascii=False).
    canonical = (
        '{"categories":["C-DES"],"cook_time":"","description":"","difficulty":"","directions":"",'
        '"ingredients":"cream","name":"Crème brûlée","notes":"","nutritional_info":"","prep_time":"",'
        '"rating":5,"servings":"","source":"","source_url":"","total_time":""}'
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert export.fields_sha256(RemoteRecipe(**BRULEE).as_dict()) == expected


VOLATILE = ("hash", "created", "photo", "photo_hash", "photo_large", "photo_url", "image_url", "in_trash", "is_pinned", "on_favorites", "on_grocery_list", "scale", "uid")


@pytest.mark.parametrize("field", VOLATILE)
def test_fields_sha256_ignores_a_volatile_field(field):
    base = RemoteRecipe(**ROAST).as_dict()
    assert export.fields_sha256({**base, field: "different"}) == export.fields_sha256(base)


@pytest.mark.parametrize("field", FIELDS)
def test_fields_sha256_changes_with_every_field(field):
    base = RemoteRecipe(**ROAST).as_dict()
    value = 1 if field == "rating" else ["C-OTHER"] if field == "categories" else "changed"
    assert export.fields_sha256({**base, field: value}) != export.fields_sha256(base)


# ---- refusals and failures: one line, no file ----


@pytest.mark.parametrize("date", [None, "", "2026-10-28\n", "2026-13-01", "../x", "28-10-2026"])
def test_a_bad_export_date_is_refused_before_paprika_is_asked(tmp_path, capsys, date):
    def never():
        raise AssertionError("Paprika was asked")

    assert export.main(out_dir=str(tmp_path), environ={} if date is None else {"EXPORT_DATE": date}, remote_factory=never) == 2
    assert capsys.readouterr().err == f"paprika export: refused: EXPORT_DATE must be YYYY-MM-DD, got {(date or '')!r}\n"
    assert os.listdir(tmp_path) == []


def test_paprika_unreachable_fails_with_one_line_and_no_file(tmp_path, capsys):
    def offline():
        raise RuntimeError("Paprika authentication failed:\nconnection refused")

    assert export.main(out_dir=str(tmp_path), environ={"EXPORT_DATE": DAY}, remote_factory=offline) == 1
    assert capsys.readouterr().err == "paprika export: failed: RuntimeError: Paprika authentication failed: connection refused\n"
    assert os.listdir(tmp_path) == []


def test_categories_unavailable_fails_rather_than_exporting_none(tmp_path, capsys):
    source = remote(ROAST)
    source.category_error = OSError("timed out")
    assert run(tmp_path, source) == 1
    assert capsys.readouterr().err.startswith("paprika export: failed: CategoriesUnavailable: Could not fetch categories")
    assert os.listdir(tmp_path) == []


def test_a_book_with_no_live_recipes_is_not_written(tmp_path, capsys):
    assert run(tmp_path, remote(TRASHED)) == 1
    assert capsys.readouterr().err == "paprika export: failed: the export would be invalid (it holds no recipes); nothing was written\n"
    assert os.listdir(tmp_path) == []


# ---- row 22: the atomic write ----


def test_a_crash_before_the_rename_leaves_no_file(tmp_path, capsys, monkeypatch):
    def crash(*args):
        raise OSError("killed between write and rename")

    monkeypatch.setattr(export.os, "rename", crash)
    assert run(tmp_path, remote(ROAST)) == 1
    assert capsys.readouterr().err == "paprika export: failed: OSError: killed between write and rename\n"
    assert os.listdir(tmp_path) == []


def test_a_crash_mid_write_leaves_the_earlier_same_day_file_whole(tmp_path, monkeypatch):
    (tmp_path / NAME).write_bytes(b"the good copy from the first run")

    def crash(fd):
        raise OSError("disk full")

    monkeypatch.setattr(export.os, "fsync", crash)
    assert run(tmp_path, remote(ROAST)) == 1
    assert (tmp_path / NAME).read_bytes() == b"the good copy from the first run"
    assert os.listdir(tmp_path) == [NAME]


def test_the_partial_file_is_dot_name_dot_tmp_in_the_output_directory(tmp_path, monkeypatch):
    # The contract's seam with the Pi's host script (mcp-host pi/mcp-export's
    # TMP pattern, pinned by its own tests): the partial is exactly `.` + name +
    # `.tmp` beside the export. Any other spelling and the host script would
    # neither recognise it as a partial nor ever clear it.
    renames = []
    real = os.rename

    def spy(src, dst):
        renames.append((src, dst))
        real(src, dst)

    monkeypatch.setattr(export.os, "rename", spy)
    assert run(tmp_path, remote(ROAST)) == 0
    assert renames == [(os.path.join(str(tmp_path), f".{NAME}.tmp"), os.path.join(str(tmp_path), NAME))]
