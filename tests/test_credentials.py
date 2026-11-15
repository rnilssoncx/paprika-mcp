"""039 order 4, Paprika reads its vault copy (plans/039 § Paprika → Its tests).

The doubles are at the seams the code documents: the vault copy is real files in a temporary directory
(`vaultfix.Copy`), the login endpoint is a real HTTP server on localhost for the tests of the probe, the
`Remote` the tools build is a double, and the spoke's transport is a double. The gate, the lifespan, the
watcher thread and the spoke thread are the ones production runs. **Each test names the wrong
implementation it catches**; the ones that guard a production line say which line, and were verified
by reverting it (plans/039 § Paprika, build log).
"""

import asyncio
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest
import requests
from control import words
from control.identity import Settings
from control.spoke_client import SpokeClient
from control.spokes import SERVER_SLOTS
from control.vault import HOUSEHOLD_TITLES, SERVICES_READERS
from fakes import RecipeRemote, category
from mcp.shared.memory import create_connected_server_and_client_session
from paprika_recipes.exceptions import PaprikaError, RequestError
from paprika_recipes.remote import RemoteRecipe
from starlette.testclient import TestClient
from test_http import ENV, LIST_TOOLS, body_of, call, post, text_of
from test_server import CALLS
from vaultfix import EMAIL, FIRST, LATER, PASSWORD, Clock, Copy, Probes, Reports

from paprika_mcp import credentials as cred
from paprika_mcp import export, http, utils
from paprika_mcp.credentials import (
    REFUSED,
    RETEST_AFTER,
    SLOT,
    Credentials,
    CredentialsUnavailable,
    default_probe,
)
from paprika_mcp.server import app

UA = "Paprika Recipe Manager 3/4.0.22 (test; build:1; macOS 1)"
NO_YET = "Error: " + words.cant_read_credentials
NO_ITEM = "Error: " + words.no_household_item("Paprika", "MP - Services")
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "paprika_wrong_password.json").read_text())
FETCHING = [name for name in CALLS if name != "format_fraction"]   # the one tool that never reaches Paprika


# ---- doubles -------------------------------------------------------------------------------


class Remotes:
    """A stand-in for the library's `Remote`: records what it was built with, and logs in or fails."""

    def __init__(self, monkeypatch):
        self.built = []
        self.agents = []
        self.error = None
        outer = self

        class Remote(RecipeRemote):
            def __init__(self, email, password, cache=None, user_agent=None, timeout=None):
                super().__init__([RemoteRecipe(uid="A1-0A", name="Soup")], [category("C-DIN", "Dinner")])
                outer.built.append((email, password))
                outer.agents.append(user_agent)

            @property
            def bearer_token(self):
                if outer.error is not None:
                    raise outer.error
                return "token"

        monkeypatch.setattr(utils, "Remote", Remote)


class LoginServer:
    """Paprika's login endpoint, on localhost: answers what it is told and records every request."""

    def __init__(self):
        self.status, self.body, self.delay = 200, b"{}", 0.0
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"path": self.path, "agent": self.headers.get("User-Agent"), "form": parse_qs(raw.decode())})
                time.sleep(outer.delay)
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(outer.body)))
                    self.end_headers()
                    self.wfile.write(outer.body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v2/account/login/"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = LoginServer()
    yield s
    s.close()


class Spoke:
    """A spoke transport that keeps what was posted. A household-only server never polls."""

    def __init__(self):
        self.posts, self.gets = [], []

    def get(self, path):
        self.gets.append(path)
        raise OSError("a household-only server never polls")

    def post(self, path, body):
        assert path == "/report"
        self.posts.append(json.loads(body))
        return 200

    def states(self):
        seen = {}
        for post_ in self.posts:
            for row in post_:
                seen[row["slot"]] = row["state"]
        return seen


def logged(caplog):
    """Everything the records carry: the formatted text, the traceback, and each `extra` field (an
    event's fields live there, which `caplog.text` does not print)."""
    return caplog.text + " ".join(str(v) for r in caplog.records for v in vars(r).values())


def until(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A home directory holding a valid old-style config.json, with both old variables set: the
    sources the item replaced, left in place so any fallback to them is visible."""
    (tmp_path / ".paprika-mcp").mkdir()
    (tmp_path / ".paprika-mcp" / "config.json").write_text(json.dumps({"email": "old-config@example.invalid", "password": "old-config-pw"}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PAPRIKA_EMAIL", "old-env@example.invalid")
    monkeypatch.setenv("PAPRIKA_PASSWORD", "old-env-pw")
    return tmp_path


def install(copy, probes=None, *, report=None, clock=None):
    """The credentials the tools read, over `copy`; returns (credentials, reports, probes, clock)."""
    reports = Reports() if report is None else report
    probes = Probes() if probes is None else probes
    clock = Clock() if clock is None else clock
    credentials = Credentials(copy.vault, reports, probes, clock=clock)
    cred.install(credentials)
    return credentials, reports, probes, clock


def ask(name, arguments=None):
    """One tool call through a real MCP session on the shared server (what stdio mode serves): the
    real `call_tool`, the real handler, the real `get_remote`."""
    async def go():
        async with create_connected_server_and_client_session(app) as client:
            result = await client.call_tool(name, arguments or {})
            return result.content[0].text

    return asyncio.run(go())


# ---- the item is the only source ---------------------------------------------------------------


def test_before_the_first_sync_every_tool_answers_the_vaults_words_and_raises_the_flag(monkeypatch, tmp_path, home, caplog):
    """Both old variables set, a valid config.json under HOME, `Remote` built nowhere, the copy
    unsynced: every fetching tool, through the real `call_tool` and the real `get_remote`, answers
    the can't-tell words by equality and the flag stands. Catches an env or config fallback (the
    Remote would be built and the text would differ), a test that patches `get_remote` (it never
    reaches credentials), and a read moved inside `get_remote`'s `try`: the text would still be the
    words (the class is not a `PaprikaError`), but `get_remote`'s catch-all would log it as an ERROR
    with a traceback, which the record check below refuses. **Reverting `utils.py`'s read into the
    `try`, or restoring the env read in `credentials.paprika`, fails it.**"""
    unsynced = Copy.at(tmp_path / "unsynced", synced=False)
    install(unsynced)
    remotes = Remotes(monkeypatch)
    remotes.error = AssertionError("a Remote was built from a fallback")
    caplog.set_level(logging.DEBUG)
    for name in FETCHING:
        assert ask(name, CALLS[name]) == NO_YET, name
    assert remotes.built == []
    assert unsynced.flag_raised()
    # One line and no traceback for the refusal (`call_tool`): a wrong implementation that logs it as an
    # error with `exc_info` prints a traceback per call for something that is expected for a minute.
    lines = [r for r in caplog.records if r.name == "paprika_mcp.server"]
    assert len(lines) == len(FETCHING) and all(r.levelno == logging.WARNING and r.exc_info is None for r in lines)
    # Nothing else of this package logged an error or a traceback either (`get_remote`'s catch-all).
    assert not [r for r in caplog.records if r.name.startswith("paprika_mcp") and (r.exc_info or r.levelno >= logging.ERROR)]
    assert all("CredentialsUnavailable" in r.getMessage() for r in lines)


def test_with_a_complete_sync_and_no_item_the_no_item_words_come_back_and_nothing_is_built(monkeypatch, tmp_path, home):
    """Env and config.json still present, the copy synced with no `Paprika` item (only decoys under
    other titles). Catches an env or config fallback that answers once the vault says no."""
    c = Copy.at(tmp_path / "c")
    c.put(1, "AnyList", username="decoy", password="decoy-anylist")
    install(c)
    remotes = Remotes(monkeypatch)
    for name in FETCHING:
        assert ask(name, CALLS[name]) == NO_ITEM, name
    assert remotes.built == []


def test_an_item_that_goes_is_refused_at_the_next_call_and_the_slot_reads_missing(monkeypatch, tmp_path, home):
    """A call that succeeds, the item unlinked, the next call: no-item words, no `Remote`, and the
    next survey reports `missing`. Catches a last-good fallback (the cached pair still logs in) and a
    survey that keeps reporting `ok` for a deleted item. **Reverting `rejected`-free read in
    `paprika()` to a cached value fails it.**"""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    credentials, reports, _, _ = install(c)
    remotes = Remotes(monkeypatch)
    utils.get_remote()
    credentials.survey()
    assert reports.last() == "ok" and remotes.built == [(EMAIL, PASSWORD)]
    c.remove(5)
    assert ask("list_categories") == NO_ITEM
    assert remotes.built == [(EMAIL, PASSWORD)]
    credentials.survey()
    assert reports.last() == "missing"


def test_an_edited_item_reaches_the_next_call(monkeypatch, tmp_path, home):
    """Catches a login read once at start, or cached by the item's id."""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    install(c)
    remotes = Remotes(monkeypatch)
    utils.get_remote()
    c.put(5, username="new@example.invalid", password="new-pw", updated=LATER)
    utils.get_remote()
    assert remotes.built == [(EMAIL, PASSWORD), ("new@example.invalid", "new-pw")]


def test_the_login_is_read_from_the_username_and_the_password_field_and_not_a_decoy(monkeypatch, tmp_path):
    """`fields["username"]` holds a different address than the item's username, and `notes` holds a
    decoy password beside `password`; two other titles hold other logins. Catches a reader of the
    wrong field (`fields["username"]`, a notes line) and one that takes the first item."""
    c = Copy.at(tmp_path / "c")
    c.put(1, "AnyList", username="anylist@example.invalid", password="anylist-pw")
    c.put(2, "Paprika", fields={"username": "decoy-field@example.invalid", "notes": "password: decoy-notes-pw"})
    install(c)
    held = cred.get_credentials()
    assert (held.username, held.secret) == (EMAIL, PASSWORD)
    assert "decoy" not in repr(held) and PASSWORD not in repr(held) and EMAIL not in repr(held)   # `repr=False` on both


def test_a_password_with_spaces_and_an_accent_reaches_the_login_byte_for_byte(monkeypatch, tmp_path, server):
    """A password with a space, an accent and spaces at both ends, an email with a space: through
    `get_remote` into the `Remote`, and through the probe into the form body. Catches Kroger's
    printable-ASCII rule copied over (a real password refused as `wrong kind`), and a `.strip()`."""
    odd = "  päss wörd  "
    c = Copy.at(tmp_path / "c")
    c.put(5, username=" odd email@example.invalid ", password=odd)
    install(c)
    remotes = Remotes(monkeypatch)
    utils.get_remote()
    assert remotes.built == [(" odd email@example.invalid ", odd)]
    server.body = b'{"result":{"token":"t"}}'
    assert default_probe(url=server.url, user_agent=lambda: UA)(cred.get_credentials()) == "ok"
    assert server.requests[0]["form"] == {"email": [" odd email@example.invalid "], "password": [odd]}


@pytest.mark.parametrize(("username", "password"), [("", PASSWORD), (EMAIL, None), (EMAIL, "")], ids=["no email", "no password field", "empty password"])
def test_an_empty_field_is_wrong_kind_and_never_sent(monkeypatch, tmp_path, username, password):
    """The one shape check there is. Catches a read that sends an empty login (the library would post
    it), a probe handed one, and a refusal worded as no-item."""
    c = Copy.at(tmp_path / "c")
    c.put(5, username=username, password=password)
    credentials, reports, probes, _ = install(c)
    remotes = Remotes(monkeypatch)
    for name in FETCHING:
        text = ask(name, CALLS[name])
        assert text == ("Error: The Paprika item in MP - Services has no username or no password field. "
                        "Fix it in 1Password; it is picked up at the fetcher's next sync."), name
    assert "no username or no password field" in text and remotes.built == []
    credentials.survey()
    assert reports.last() == "wrong kind" and probes.count == 0


def test_the_get_remote_failure_text_names_the_item_and_holds_neither_value(monkeypatch, tmp_path, home, caplog):
    """A login that fails with a library message echoing the email and the password. The tool's text
    names the Paprika item in MP - Services (and never an environment variable or config.json), holds
    neither value, and the log (root at DEBUG, traceback included) holds neither. Catches the old text,
    a message passed through unscrubbed, and `raise ... from e` (`call_tool` logs the traceback, chain
    included). **Reverting `utils.py`'s `from None` or its `.replace` scrub fails it.**"""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    install(c)
    remotes = Remotes(monkeypatch)
    tail = "Check the Paprika item in MP - Services in 1Password; an edit is picked up at the fetcher's next sync."
    # The message as the library builds it: `f"... {result.content}"`, the bytes' repr.
    remotes.error = PaprikaError(f"No bearer token found in response: {json.dumps({'echo': EMAIL, 'p': PASSWORD}).encode()}")
    caplog.set_level(logging.DEBUG)
    text = ask("list_categories")
    assert text == f"Error: Paprika authentication failed: the login answer held no token. {tail}"
    assert "PAPRIKA_EMAIL" not in text and "config.json" not in text
    assert EMAIL not in logged(caplog) and PASSWORD not in logged(caplog)
    # A `RequestError` keeps the server's own message, with both values taken out whatever their case.
    remotes.error = RequestError(f"No such account {EMAIL.upper()} (password {PASSWORD.upper()} is wrong)")
    text = ask("list_categories")
    assert text == (f"Error: Paprika authentication failed: No such account [email] (password [password] is wrong). {tail}")
    assert EMAIL not in logged(caplog).lower() and PASSWORD.lower() not in logged(caplog).lower()


def test_a_stale_user_agent_is_never_put_on_the_password(monkeypatch, tmp_path):
    """The library raises the server's *Unrecognized client* for a User-Agent Paprika no longer
    accepts. The tool says so, names the fix, and never sends anyone to the 1Password item; and it
    asks for no re-test. Catches the generic failure text (which points at the password), and a
    `rejected` that lets a stale agent read as `refused by the service`. **Reverting `utils.py`'s
    `unrecognized client` branch fails it.**"""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    credentials, _, probes, _ = install(c)
    remotes = Remotes(monkeypatch)
    credentials.survey()
    remotes.error = RequestError("Unrecognized client")
    assert ask("list_categories") == "Error: " + utils.STALE_AGENT
    assert "1Password" not in utils.STALE_AGENT and "secrets-env.py paprika" in utils.STALE_AGENT
    credentials.survey()
    assert probes.count == 1


# ---- the probe's verdicts ---------------------------------------------------------------------


def test_the_recorded_wrong_password_answer_is_refused_by_the_service(server):
    """Replays the answer Paprika gave a made-up login (tests/fixtures/paprika_wrong_password.json),
    status and body as recorded. Catches a verdict keyed on a status the service does not use for
    this (it answered 200), and a probe that cannot read an `error` object."""
    server.status, server.body = FIXTURE["status"], FIXTURE["body"].encode()
    held = cred.Held("i", "u", "nobody@example.invalid", "made-up")
    assert default_probe(url=server.url, user_agent=lambda: UA)(held) == REFUSED


def test_any_other_error_message_on_a_200_is_also_refused_by_the_service(server):
    """The recorded answer is for an account that does not exist; a real account with a wrong
    password may say something else. The rule is broad on purpose (any `error` but *Unrecognized
    client*). Catches a verdict that matches the recorded message only, and one that reads a
    different message as inconclusive for ever."""
    server.body = b'{"error":{"code":0,"message":"Invalid password"}}'
    assert default_probe(url=server.url, user_agent=lambda: UA)(cred.Held("i", "u", EMAIL, PASSWORD)) == REFUSED


def test_the_probe_sends_the_environments_user_agent_and_get_remote_passes_it_too(monkeypatch, tmp_path, server):
    """`PAPRIKA_USER_AGENT` set, `default_probe()` built with no `user_agent`: the request carries
    it, and `get_remote` hands the same value to the `Remote`. Catches a probe that never reads the
    environment (the other probe tests inject the agent), and a `get_user_agent` that ignores it.
    **Reverting `credentials._user_agent`'s environment read fails it.**"""
    monkeypatch.setenv("PAPRIKA_USER_AGENT", UA)
    server.body = b'{"result":{"token":"t"}}'
    assert default_probe(url=server.url)(cred.Held("i", "u", EMAIL, PASSWORD)) == "ok"
    assert server.requests[0]["agent"] == UA
    c = Copy.at(tmp_path / "c")
    c.put(5)
    install(c)
    remotes = Remotes(monkeypatch)
    utils.get_remote()
    assert remotes.agents == [UA]


INCONCLUSIVE = [
    ("429", 429, b'{"error":{"message":"slow down"},"echo":"%s"}' % EMAIL.encode(), 0.0),
    ("503", 503, b"<html>down %s</html>" % EMAIL.encode(), 0.0),
    ("timeout", 200, b'{"result":{"token":"t"}}', 1.0),
    ("200 with no token", 200, b'{"result":{},"echo":"%s"}' % EMAIL.encode(), 0.0),
    ("unrecognized client", 200, b'{"error":{"code":0,"message":"Unrecognized client","echo":"%s"}}' % EMAIL.encode(), 0.0),
]


@pytest.mark.parametrize(("status", "body", "delay"), [c[1:] for c in INCONCLUSIVE], ids=[c[0] for c in INCONCLUSIVE])
def test_an_answer_that_says_nothing_about_the_login_reports_nothing_and_is_tried_again_after_an_hour(tmp_path, server, caplog, status, body, delay):
    """Through `Credentials.survey` with the real probe against a real server: nothing reported and
    the slot left as it was, **exactly one POST** per probe (the library's `Remote` would post four into a
    429 or 503), carrying the User-Agent, and a second attempt only after RETEST_AFTER. Catches any
    failure read as a refusal (*Unrecognized client* must never point at the password), the library's
    retry adapter, a probe with no User-Agent, a retry on every survey, and a probe that logs the
    login answer's body. **Reverting `default_probe`
    to a `Remote`-built session, or its `User-Agent` header, fails it.**"""
    caplog.set_level(logging.DEBUG)
    server.status, server.body, server.delay = status, body, delay
    c = Copy.at(tmp_path / "c")
    c.put(5)
    clock, reports = Clock(), Reports()
    credentials = Credentials(c.vault, reports, default_probe(url=server.url, timeout=0.3, user_agent=lambda: UA), clock=clock)
    credentials.survey()
    assert reports.calls == [] and len(server.requests) == 1
    assert server.requests[0]["agent"] == UA and server.requests[0]["path"] == "/api/v2/account/login/"
    clock.now += RETEST_AFTER - 1
    credentials.survey()
    assert len(server.requests) == 1
    clock.now += 1
    credentials.survey()
    assert len(server.requests) == 2 and reports.calls == []
    # The answer's body echoed the email: the probe logs a status or a type, never what it was told.
    assert EMAIL not in logged(caplog) and PASSWORD not in logged(caplog)


def test_a_retry_error_is_inconclusive_and_does_not_escape_the_probe(monkeypatch):
    """`RetryError` is neither `HTTPError` nor `PaprikaError`. Catches a probe that catches the
    narrower classes: it would escape, and a watcher that lets it escape dies."""
    def exhausted(*args, **kwargs):
        raise requests.exceptions.RetryError("too many 429 responses")

    monkeypatch.setattr(cred.requests, "post", exhausted)
    assert default_probe(user_agent=lambda: UA)(cred.Held("i", "u", EMAIL, PASSWORD)) is None


def test_a_probe_that_raises_leaves_the_watcher_alive_and_a_later_deletion_still_reaches_missing(tmp_path, caplog):
    """The probe raises `RuntimeError` with the password in its message. Catches a survey that does
    not catch it (the watcher thread dies, and the item that goes later is never reported), and a
    log that carries the message. **Reverting `survey`'s `except` fails it.**"""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    probes = Probes()
    probes.raises = RuntimeError(f"login failed for {EMAIL} with {PASSWORD}")
    credentials, reports, _, _ = install(c, probes)
    caplog.set_level(logging.DEBUG)
    stop = threading.Event()
    watcher = threading.Thread(target=credentials.watch, args=(stop, 0.01), daemon=True)
    watcher.start()
    try:
        assert until(lambda: probes.count >= 1)
        c.remove(5)
        assert until(lambda: reports.last() == "missing")
        assert watcher.is_alive()
    finally:
        stop.set()
        watcher.join(5)
    assert EMAIL not in logged(caplog) and PASSWORD not in logged(caplog)
    assert [r.error for r in caplog.records if getattr(r, "event", "") == "paprika.survey_failed"][:1] == ["RuntimeError"]


# ---- when it is tested ------------------------------------------------------------------------


def test_the_same_values_saved_under_a_new_updated_at_are_tested_again_and_new_values_under_the_same_one_are_not(tmp_path):
    """Catches `_tested` keyed by the password (the same values re-saved would not be tested) or by the
    item's id (neither would), and one that never re-tests. Nothing is reported before the first sync."""
    unsynced = Copy.at(tmp_path / "u", synced=False)
    unsynced.put(5)
    credentials, reports, probes, _ = install(unsynced)
    for _ in range(3):
        credentials.survey()
    assert reports.calls == [] and probes.count == 0
    c = Copy.at(tmp_path / "c")
    c.put(5)
    credentials, reports, probes, _ = install(c)
    credentials.survey()
    credentials.survey()
    assert probes.count == 1
    c.put(5, updated=LATER)                       # the same values, saved again
    credentials.survey()
    assert probes.count == 2
    c.put(5, password="another-pw", updated=LATER)  # other values, the same `updatedAt`
    credentials.survey()
    assert probes.count == 2


# ---- the flag ---------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["absent", "torn", "unsynced"])
def test_sixty_surveys_of_one_unchanged_state_raise_one_flag(tmp_path, state):
    """An item that is simply not there, a torn item file, an unsynced copy: each wants a sync once,
    when the survey first sees it. Catches a survey that raises the flag by looking (sixty syncs spent
    in an hour of a hundred a day), and one that never raises it on the change. **Reverting `_seen_as`'s
    `changed` guard fails it.**"""
    c = Copy.at(tmp_path / "c", synced=state != "unsynced")
    if state == "torn":
        c.tear(5)
    credentials, reports, _, _ = install(c)
    for _ in range(60):
        credentials.survey()
    assert c.vault.flags == 1
    assert reports.calls == ([] if state == "unsynced" else [(SLOT, "unreadable" if state == "torn" else "missing")] * 60)


def test_a_refused_version_raises_one_flag_however_often_it_is_refused(tmp_path):
    """Starting from a synced copy with the item present, so no change of state raises a flag as well
    (a flag on a change would shadow the one on a refusal). The test is refused, then refused again
    after each call's re-test across several hours. Catches a refusal that raises no flag, and one
    that raises it on every refusal of the same version. **Reverting `_survey`'s `_flagged` guard, or
    its `raise_flag`, fails it.**"""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    credentials, reports, probes, clock = install(c, Probes(REFUSED))
    credentials.survey()
    assert c.vault.flags == 1 and reports.last() == REFUSED
    for _ in range(4):
        clock.now += RETEST_AFTER
        credentials.rejected(("0" * 21 + "00005", FIRST))
        credentials.survey()
    assert probes.count == 5 and c.vault.flags == 1 and reports.last() == REFUSED
    c.put(5, updated=LATER)                      # a new version that is also refused: its own flag
    credentials.survey()
    assert c.vault.flags == 2


# ---- a refused call -----------------------------------------------------------------------------


def test_a_login_refused_during_a_call_is_tested_again_and_the_page_hears_it(monkeypatch, tmp_path, home):
    """Survey (`ok`), a tool call whose login is refused, survey: two probes, and the second says
    `refused by the service`. Catches a refusal at use that no one hears (the page reads `ok` for
    ever after a password changed in Paprika alone). **Reverting `utils.get_remote`'s `rejected(...)`
    fails it.**"""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    credentials, reports, probes, _ = install(c, Probes("ok", REFUSED))
    remotes = Remotes(monkeypatch)
    credentials.survey()
    assert reports.last() == "ok"
    remotes.error = PaprikaError("Authentication URL returned unexpected status")
    assert ask("list_categories").startswith("Error: Paprika authentication failed")
    credentials.survey()
    assert probes.count == 2 and reports.last() == REFUSED


def test_two_refused_calls_within_the_hour_cause_one_re_test(monkeypatch, tmp_path, home):
    """Catches a re-test per refused call (a login each, into Paprika's limits), and a re-test that is
    never again allowed. **Reverting `_take_rejection`'s `_retested_at` window fails it.**"""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    credentials, reports, probes, clock = install(c)
    remotes = Remotes(monkeypatch)
    remotes.error = PaprikaError("refused")
    credentials.survey()
    assert probes.count == 1
    for _ in range(2):
        with pytest.raises(PaprikaError):
            utils.get_remote()
        credentials.survey()
    assert probes.count == 2
    clock.now += RETEST_AFTER
    with pytest.raises(PaprikaError):
        utils.get_remote()
    credentials.survey()
    assert probes.count == 3


def test_a_successful_login_clears_a_refused_verdict_for_one_re_test(monkeypatch, tmp_path, home):
    """REFUSED is held for the version, then a call's login works (the verdict was a misread, or the
    password was mended in Paprika): the next survey tests again and says `ok`, and a second success
    within the hour tests nothing more. Catches a refusal that sticks for ever while calls succeed, and
    a re-test on every success. **Reverting `utils.get_remote`'s `accepted(...)` fails it.**"""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    credentials, reports, probes, clock = install(c, Probes(REFUSED, "ok", REFUSED))
    Remotes(monkeypatch)
    credentials.survey()
    assert reports.last() == REFUSED and probes.count == 1
    utils.get_remote()
    credentials.survey()
    assert probes.count == 2 and reports.last() == "ok"
    utils.get_remote()                               # nothing is refused now: no re-test for a success
    credentials.survey()
    assert probes.count == 2


def test_a_successful_login_while_refused_is_re_tested_at_most_once_an_hour(monkeypatch, tmp_path, home):
    """Catches the success path bypassing the window (a login per success while the test keeps saying
    refused). **Reverting `_take_signals`'s window fails it.**"""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    credentials, reports, probes, clock = install(c, Probes(REFUSED))
    Remotes(monkeypatch)
    credentials.survey()
    for _ in range(3):
        utils.get_remote()
        credentials.survey()
    assert probes.count == 2 and reports.last() == REFUSED
    clock.now += RETEST_AFTER
    utils.get_remote()
    credentials.survey()
    assert probes.count == 3


def test_a_refusal_of_a_version_that_is_no_longer_current_clears_nothing(tmp_path):
    """Catches a re-test asked for by a call that read the item before it was edited."""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    credentials, _, probes, _ = install(c)
    credentials.survey()
    c.put(5, updated=LATER)
    credentials.rejected(("0" * 21 + "00005", FIRST))
    credentials.survey()
    assert probes.count == 2        # the new version's first test, and nothing for the old version's refusal
    credentials.survey()
    assert probes.count == 2


# ---- a blocked test does not hold anything up -----------------------------------------------------


def test_a_blocked_probe_delays_neither_a_tool_call_nor_tools_list(monkeypatch, tmp_path):
    """Through the production app: the probe is held on an `Event`, and a tool call (through the real
    `get_remote`) and `tools/list` are both answered within two seconds. Catches a watcher on the event
    loop (everything waits behind the login), and a lock held across the probe (a call's credential read
    waits). **Reverting `http.py`'s watcher thread for a coroutine fails it.**"""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    Remotes(monkeypatch)
    probes = Probes()
    probes.hold = threading.Event()
    env = {"PAPRIKA_VAULT_DIR": str(c.dir), "PAPRIKA_WANT_DIR": str(c.want)}
    with TestClient(http.build(Settings.from_environ(ENV), env, probe=probes), base_url="http://10.0.0.5") as client:
        try:
            assert probes.entered.wait(5)
            started = time.monotonic()
            assert body_of(post(client, LIST_TOOLS))["result"]["tools"]
            reply = post(client, call("list_categories"))
            assert text_of(reply)[0] is False
            assert time.monotonic() - started < 2.0
        finally:
            probes.hold.set()


# ---- the wiring through http.build and http.run ------------------------------------------------------


def test_build_shares_one_credentials_object_and_starts_and_stops_its_threads(monkeypatch, tmp_path):
    """The production `build`, with a synced copy and a spoke transport double: the object the tools
    read is the one whose watcher runs, the watcher and the spoke's thread run for the life of the app,
    the spoke client never polls, the state reaches the spoke, and both threads end with the app.
    Each is a line in `http.py`: `install(credentials)` (revert: `current()` is another object),
    the watcher's `Thread(...).start()` (revert: the probe is never called), the spoke's (revert:
    nothing is posted), `poll=False` in `credentials.open_vault` (revert: the transport is asked to
    poll), `stopping.set()` (revert: the threads outlive the app)."""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    monkeypatch.setattr(http, "SPOKE_STEP", 0.02)
    spoke, probes, made = Spoke(), Probes(), []
    real = http.open_vault

    def spy(*args, **kwargs):
        made.append(real(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(http, "open_vault", spy)
    env = {"PAPRIKA_VAULT_DIR": str(c.dir), "PAPRIKA_WANT_DIR": str(c.want)}
    with TestClient(http.build(Settings.from_environ(ENV), env, probe=probes, transport=spoke), base_url="http://10.0.0.5"):
        assert len(made) == 1 and isinstance(made[0][1], SpokeClient)
        assert cred.current() is made[0][0]
        assert until(lambda: probes.count == 1)
        assert until(lambda: spoke.states() == {SLOT: "ok"})
        assert spoke.gets == []
        names = {t.name for t in threading.enumerate()}
        assert {"paprika-watch", "paprika-spoke"} <= names
    assert until(lambda: not ({"paprika-watch", "paprika-spoke"} & {t.name for t in threading.enumerate()}))


def test_run_attaches_a_log_handler_to_paprika_mcp_itself(monkeypatch, capsys):
    """This server configured no logging, so the watcher's lines reached a root logger with no handler
    at WARNING. Catches a `run()` that never attaches one (a line prints blank or not at all).
    **Reverting `attach_paprika_logging()` in `http.run` fails it.**"""
    logger = logging.getLogger("paprika_mcp")
    before = (list(logger.handlers), logger.level, logger.propagate)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(http.uvicorn, "run", lambda *a, **k: None)
    try:
        http.run()
        assert any(isinstance(h, http._LiveStderr) for h in logger.handlers) and logger.propagate is False
        cred.event(cred.log, "paprika.test_line", level=logging.WARNING, status=7)
        assert "event=paprika.test_line" in capsys.readouterr().err
        http.attach_paprika_logging()
        assert sum(isinstance(h, http._LiveStderr) for h in logger.handlers) == 1     # idempotent
    finally:
        logger.handlers[:] = before[0]
        logger.setLevel(before[1])
        logger.propagate = before[2]


# ---- no value in a log --------------------------------------------------------------------------------


def test_neither_value_appears_in_a_log_a_tool_text_or_the_exports_stderr(monkeypatch, tmp_path, home, caplog, capsys):
    """Distinctive values; a login answer echoes both (the raising probe is
    `test_a_probe_that_raises_...`'s). The root logger at DEBUG, the tool's text and the export's stderr
    hold neither. Catches `exc_info` on a chained original and the export's one-line failure quoting
    the library."""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    install(c)
    remotes = Remotes(monkeypatch)
    remotes.error = RequestError(f"Account {EMAIL} refused password {PASSWORD}")
    caplog.set_level(logging.DEBUG)
    text = ask("list_categories")
    assert export.main(out_dir=str(tmp_path), environ={"EXPORT_DATE": "2026-11-14"}) == 1
    seen = logged(caplog) + text + capsys.readouterr().err
    assert EMAIL not in seen and PASSWORD not in seen
    assert "paprika export: failed: RequestError: Paprika authentication failed: Account [email] refused password [password]" in seen


# ---- names pinned to qbranch -------------------------------------------------------------------------


def test_the_names_are_the_ones_qbranch_holds():
    """Catches a title or a slot renamed here and not there: the fetcher would copy nothing to this
    server, or Q Branch would refuse its report."""
    assert SERVICES_READERS["paprika"] == (cred.TITLE,) == ("Paprika",)
    assert cred.TITLE in HOUSEHOLD_TITLES
    assert SLOT == SERVER_SLOTS["paprika"][0] == "services/paprika"
    assert cred.VAULT_TITLE == "MP - Services"


# ---- the export ----------------------------------------------------------------------------------------


@pytest.fixture
def keep_umask():
    old = os.umask(0o022)
    os.umask(old)
    yield
    os.umask(old)


def test_the_export_reads_the_vault_copy_through_main_with_no_factory(monkeypatch, tmp_path, home, keep_umask, capsys):
    """`main()` with no factory, env and config.json present, the vault directory taken from the
    environment as the container's is: it logs in with the item's values, writes the file, and starts
    no thread. Catches `main` bypassing `get_remote` (an injected factory would), an env or config
    fallback, and a watcher started by the shared function."""
    c = Copy.at(tmp_path / "c")
    c.put(5)
    monkeypatch.setenv("PAPRIKA_VAULT_DIR", str(c.dir))
    monkeypatch.setenv("PAPRIKA_WANT_DIR", str(c.want))
    remotes = Remotes(monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    threads = {t.name for t in threading.enumerate()}
    assert export.main(out_dir=str(out), environ={"EXPORT_DATE": "2026-11-14"}) == 0
    assert [p.name for p in out.iterdir()] == ["paprika-2026-11-14.json.gz"]
    assert remotes.built == [(EMAIL, PASSWORD)]
    assert {t.name for t in threading.enumerate()} == threads
    assert not c.flag_raised()


def test_the_export_before_the_first_sync_fails_in_the_vaults_words_and_raises_no_flag(monkeypatch, tmp_path, home, keep_umask, capsys):
    """The export container has no `/want`: a loud read would try to write the flag. Catches a read
    that is not `quiet=True` (the flag file appears), an env or config fallback, and a failure that
    is not the vault's words. **Reverting `export.py`'s `quiet=True` partial fails it.**"""
    c = Copy.at(tmp_path / "c", synced=False)
    monkeypatch.setenv("PAPRIKA_VAULT_DIR", str(c.dir))
    monkeypatch.setenv("PAPRIKA_WANT_DIR", str(c.want))
    remotes = Remotes(monkeypatch)
    threads = {t.name for t in threading.enumerate()}
    assert export.main(out_dir=str(tmp_path), environ={"EXPORT_DATE": "2026-11-14"}) == 1
    assert capsys.readouterr().err == f"paprika export: failed: CredentialsUnavailable: {words.cant_read_credentials}\n"
    assert remotes.built == [] and not c.flag_raised()
    assert {t.name for t in threading.enumerate()} == threads
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".gz") == []


def test_stdio_mode_has_no_vault_and_says_it_cannot_tell(monkeypatch, home):
    """The server run by hand on a Mac: no `/vault`, so every read is the vault's can't-tell words
    (accepted, 039 § Paprika). Catches the old fallbacks serving a stdio client from env or config."""
    remotes = Remotes(monkeypatch)
    assert ask("list_categories") == NO_YET
    assert remotes.built == []
    with pytest.raises(CredentialsUnavailable):
        cred.get_credentials()
