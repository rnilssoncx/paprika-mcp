"""The server's one upstream credential, read from its vault copy (moneypenny plan 039, order 4).

Paprika used to be handed `PAPRIKA_EMAIL` and `PAPRIKA_PASSWORD` in its environment, or a
`~/.paprika-mcp/config.json`. It now holds neither: the login is the `Paprika` item in the
Services vault (the email its username, the password its `password` field), copied into a read-only
mount by that vault's fetcher and read here through `control.vault`. **This module reads files,
makes one call to test the login, and reports to Q Branch over the server's spoke**; the fetcher is
the only process that holds a 1Password token. It is `kroger_mcp/credentials.py`'s twin, with the
differences below.

## The item is the only source

`get_credentials` reads the item on each call that needs one, so an item edited in 1Password reaches
the next call after the fetcher's next sync with no restart. Before the first complete sync (`.synced`
absent) it cannot tell whether the item exists: it raises `CredentialsUnavailable` carrying 039's
sentence (`words.cant_read_credentials`) before any request is built, and the vault raises the flag
that asks the fetcher to sync. "No item" is said only after one whole sync (the absence rule, applied
to a credential). **No environment variable and no file in a home directory is a fallback**: a
fallback is a last-good copy of a credential, which is what deleting the item is meant to stop.

**`CredentialsUnavailable` is not a `PaprikaError`.** `utils.get_remote` rewraps those into "Paprika
authentication failed ..." text, which would put the words behind a sentence about a password; the
credentials are read outside its `try`, and `server.call_tool` answers this class with its own words
and one log line.

## No state on the call path, so no lock

A tool call reads the item, builds a `Remote` (which logs in at once) and holds nothing: no token is
kept, so nothing is dropped when the item changes. **Only the watcher touches the slot's bookkeeping**
(`_tested`, `_tried`, `_seen`, `_flagged`), and it runs on one thread. The one thing a call hands the
watcher is `rejected(version)`, a single attribute assignment; a write lost to the watcher clearing it
at the same moment costs one re-test an hour later, never a wrong state.

**The watcher is a thread, not a coroutine** (Quartermaster's intake's shape, not Kroger's): the login
is synchronous `requests`, and on the event loop a slow login would freeze every request and the
`tools/list` that Q Branch's health look needs.

## The test, and what it says

The test is **one `POST` to the login endpoint with the User-Agent and no retry adapter**: the library's
own login discards the status and its `Retry` re-posts a login into a 429 (four posts for one test), and
Paprika's login limits are why no token is held. The verdicts:

- a 200 carrying a token is `ok`;
- a 200 carrying an `error` object is `refused by the service`, **except** *Unrecognized client*, which
  is a stale User-Agent after a Paprika app update and must never point at the password. The recorded
  answer to a wrong login is `tests/fixtures/paprika_wrong_password.json`, taken with a made-up email;
- every other answer (429, 5xx, a 200 with no token and no error, a timeout, no answer) says nothing
  about the credential: the slot is left as it was and the test is tried again after `RETEST_AFTER`.

**What was recorded is the answer for an account that does not exist** (*Account not found.*), because the
household's own account is never used for a fixture (its lockout limit is unknown). A real wrong password
for a real account may be worded differently; any other `error` message is read as a refusal, and any
other *shape* of answer as inconclusive.

**When it is tested:** when an item first appears and when its `updatedAt` changes (`_tested` is keyed by
the version, `(item id, updatedAt)`, never by the value). A login refused during a tool call marks that
version for one re-test, **at most once per `RETEST_AFTER`**, so a password changed in Paprika but not in
1Password reaches the page within the hour instead of reading `ok` for ever; nothing is reported from the
call itself.

## The flag

A survey **never raises the flag by looking** at an absence it has already seen: it is raised when what a
survey sees *changes* into `missing`, `unreadable` or `not synced`, and **once per refused version**, so a
revoked password under steady calls cannot spend the Services fetcher's hundred syncs a day.

## Logging

An exception is logged by its type alone: its text can quote the login that was in flight.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import requests
from control import words
from control.fetch import VAULTS
from control.logs import event
from control.spoke_client import HttpTransport, SpokeClient, Transport
from control.vault import Item, Vault, VaultRefusal
from paprika_recipes.user_agent import detect_user_agent

__all__ = [
    "LOGIN_URL",
    "REFUSED",
    "RETEST_AFTER",
    "SLOT",
    "TITLE",
    "VAULT_TITLE",
    "WATCH_EVERY",
    "Credentials",
    "CredentialsUnavailable",
    "Held",
    "accepted",
    "current",
    "default_probe",
    "get_credentials",
    "install",
    "open_vault",
    "rejected",
]

log = logging.getLogger(__name__)

VAULT_TITLE = VAULTS["services"]
TITLE = "Paprika"
SLOT = "services/paprika"
#: The field the item keeps the password in; the email is the item's username.
SECRET_FIELD = "password"
REFUSED = "refused by the service"
#: How often the slot is looked at when no call has been made.
WATCH_EVERY = 60.0
#: The soonest a test that could not tell, or a re-test a refused call asked for, is made again.
RETEST_AFTER = 3600.0
LOGIN_URL = "https://www.paprikaapp.com/api/v2/account/login/"
#: A probe that has not been answered by now is inconclusive.
PROBE_TIMEOUT = 10.0
VAULT_DIR = Path("/vault")
WANT_DIR = Path("/want")

#: (the item's id, its `updatedAt`): what a test, a refusal and a re-test are held against.
Version = tuple[str, str]


def no_fields() -> str:
    """The item is there but holds nothing where the login goes. 039's register."""
    return (f"The {TITLE} item in {VAULT_TITLE} has no username or no password field. "
            "Fix it in 1Password; it is picked up at the fetcher's next sync.")


class CredentialsUnavailable(Exception):
    """A call that needs the login cannot have it. `str()` is the words the tool answers
    (`server.call_tool` puts `Error: ` before them). Not a `PaprikaError`, deliberately: see the
    module docstring."""


@dataclass(frozen=True, slots=True)
class Held:
    """The login and the item it was read from. `version` travels with the values, so a refusal is
    held against the item the values came from and not whatever is current. The values are plain
    strings, never stripped: a password may begin or end with a space."""

    item_id: str
    updated_at: str
    username: str = field(repr=False)
    secret: str = field(repr=False)

    @property
    def version(self) -> Version:
        return (self.item_id, self.updated_at)


#: The test: the login in, `ok` or `refused by the service` out, or None when the answer says
#: nothing about the login. Synchronous, because it runs on the watcher's thread.
Probe = Callable[[Held], str | None]


def verdict(status: int, body: bytes) -> str | None:
    """The slot state one login answer gives, or None for an inconclusive one."""
    if status != 200:
        return None
    try:
        document = json.loads(body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    result = document.get("result")
    if isinstance(result, dict) and result.get("token"):
        return "ok"
    error = document.get("error")
    if isinstance(error, dict):
        message = str(error.get("message", ""))
        if "unrecognized client" in message.lower():
            return None
        return REFUSED
    return None


def default_probe(*, url: str = LOGIN_URL, timeout: float = PROBE_TIMEOUT,
                  user_agent: Callable[[], str | None] | None = None) -> Probe:
    """The test. `requests.post` directly: a bare call has urllib3's default of no retries, where
    the library's `Remote` mounts `Retry(total=3)` on POST. A redirect is not followed, so the login
    cannot follow one; a failure to get an answer is inconclusive; nothing is ever logged but a type."""
    agent = user_agent if user_agent is not None else _user_agent

    def probe(held: Held) -> str | None:
        headers = {}
        found = agent()
        if found:
            headers["User-Agent"] = found
        try:
            response = requests.post(url, data={"email": held.username, "password": held.secret},
                                     headers=headers, timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            event(log, "paprika.credential_probe_unreachable", level=logging.WARNING, error=type(exc).__name__)
            return None
        answer = verdict(response.status_code, response.content)
        if answer is None:
            event(log, "paprika.credential_probe_inconclusive", level=logging.WARNING, status=response.status_code)
        return answer

    return probe


def _user_agent() -> str | None:
    """The Mac app's version string (`PAPRIKA_USER_AGENT`, not a secret), else what the library
    would detect, as its `Remote` does."""
    return os.environ.get("PAPRIKA_USER_AGENT") or detect_user_agent()


class Credentials:
    """The item, read from `vault` when asked, and the state reported for it.

    `report` is `SpokeClient.report` (or None where no spoke is configured); it validates the state
    and records it only when it changed. `probe` is the test. **One object serves the tools and the
    watcher**: a second one would test one copy of the item and serve another.
    """

    def __init__(self, vault: Vault, report: Callable[[str, str], None] | None, probe: Probe,
                 *, clock: Callable[[], float] = time.monotonic) -> None:
        self._vault = vault
        self._report = report
        self._probe = probe
        self._clock = clock
        # Everything below is the watcher's alone, except `_rejection` (see the module docstring).
        #: (version, state) of the last test that told: not tested twice.
        self._tested: tuple[Version, str] | None = None
        #: (version, when) of a test that could not tell.
        self._tried: tuple[Version, float] | None = None
        #: what the last survey found, so the flag is raised on a change and not on a repeat.
        self._seen: str | None = None
        #: the version a refusal last raised the flag for: once per version.
        self._flagged: Version | None = None
        #: when a refused call last cleared the test: at most once per RETEST_AFTER.
        self._retested_at: float | None = None
        #: the version a tool call's login was refused for, until the watcher takes it.
        self._rejection: Version | None = None
        #: the version a tool call's login succeeded for, until the watcher takes it.
        self._acceptance: Version | None = None

    # -- what a call asks ----------------------------------------------------------------

    def paprika(self, *, quiet: bool = False) -> Held:
        """The login, or `CredentialsUnavailable` with the words for why not. Loud by default:
        a call that cannot be served raises the flag (once per call, bounded by the fetcher's floor
        and budget). `quiet` is the nightly export's, which has no `/want` to raise it in."""
        try:
            item = self._vault.for_title(TITLE, quiet=quiet)
        except VaultRefusal as exc:
            raise CredentialsUnavailable(str(exc)) from None
        held = _held_of(item)
        if held is None:
            raise CredentialsUnavailable(no_fields())
        return held

    def rejected(self, version: Version) -> None:
        """A login made with the item as of `version` was refused. Not a verdict: it asks the
        watcher to test that version again, which reports the refusal if it is real."""
        self._rejection = version

    def accepted(self, version: Version) -> None:
        """A login made with the item as of `version` worked. If the page holds `refused by the
        service` for that version, the watcher tests it again: the refusal was wrong or is mended."""
        self._acceptance = version

    # -- what the page is told -----------------------------------------------------------

    def survey(self) -> None:
        """Look at the slot now, test the login if it is new, and report what changed. Never
        raises: a slot that cannot be read is a state, and a test that fails is inconclusive or
        logged by its type."""
        try:
            self._survey()
        except Exception as exc:  # noqa: BLE001 - a survey that dies would leave the slot unreported
            event(log, "paprika.survey_failed", level=logging.WARNING, error=type(exc).__name__)

    def watch(self, stop: threading.Event, every: float | None = None) -> None:
        """`survey` until `stop` is set, so an item that appears, changes or goes is reported
        without waiting for a call to need it."""
        while not stop.is_set():
            self.survey()
            stop.wait(WATCH_EVERY if every is None else every)

    def _survey(self) -> None:
        held = self._look()
        if held is None:
            return
        self._take_signals(held)
        if self._tested is not None and self._tested[0] == held.version:
            return
        if (self._tried is not None and self._tried[0] == held.version
                and self._clock() - self._tried[1] < RETEST_AFTER):
            return
        answer = self._probe(held)
        if answer is None:
            self._tried = (held.version, self._clock())
            return
        self._tried = None
        self._tested = (held.version, answer)
        if answer == REFUSED and self._flagged != held.version:
            self._flagged = held.version
            self._vault.raise_flag()            # a rotated password: have the fetcher look again, once
        self._look()                            # the item as it is now, with the verdict if it is still the one

    def _take_signals(self, held: Held) -> None:
        """A refused call asked for a re-test of `held`'s version, or a successful one found it held
        as refused: clear the test, at most once per RETEST_AFTER, so a login that is refused on every
        call cannot be tested on every survey."""
        rejection, self._rejection = self._rejection, None
        acceptance, self._acceptance = self._acceptance, None
        if rejection != held.version and not (acceptance == held.version and self._tested == (held.version, REFUSED)):
            return
        now = self._clock()
        if self._retested_at is not None and now - self._retested_at < RETEST_AFTER:
            return
        self._retested_at = now
        self._tested = None
        self._tried = None

    def _look(self) -> Held | None:
        """The login as it is now, or None; reports the slot's state when there is one to report.
        A survey is quiet: it never raises the flag by reading, and raises it itself when what it
        sees has changed into a state that wants a sync."""
        try:
            item = self._vault.for_title(TITLE, quiet=True)
        except VaultRefusal as exc:
            if not self._vault.synced():
                self._seen_as("not synced")            # nothing is reported before a sync
            elif str(exc) == words.cant_read_credentials:
                self._seen_as("unreadable")
                self._note("unreadable")
            else:
                self._seen_as("missing")
                self._note("missing")
            return None
        held = _held_of(item)
        if held is None:
            self._seen_as("wrong kind")
            self._note("wrong kind")
            return None
        self._seen = "present"
        if self._tested is not None and self._tested[0] == held.version:
            self._note(self._tested[1])
        return held

    def _seen_as(self, state: str) -> None:
        changed = self._seen != state
        self._seen = state
        if changed and state in ("missing", "unreadable", "not synced"):
            self._vault.raise_flag()

    def _note(self, state: str) -> None:
        if self._report is not None:
            self._report(SLOT, state)


def _held_of(item: Item) -> Held | None:
    """The login in `item`, or None when either field is empty. **No shape check beyond that**: a
    login is a form body and carries any string, so a rule such as Kroger's printable-ASCII one would
    refuse a real password with a space or an accent. Values are sent as they are, unstripped."""
    username = item.username
    secret = item.fields.get(SECRET_FIELD, "")
    if not username or not secret:
        return None
    return Held(item.id, item.updated_at, username, secret)


# -- the wiring -----------------------------------------------------------------------------


def vault_from(environ: Mapping[str, str]) -> Vault:
    """`PAPRIKA_VAULT_DIR` (default `/vault`, read-only) and `PAPRIKA_WANT_DIR` (default `/want`,
    read-write) are paths, not secrets. A missing vault directory is not special-cased: it is a copy
    that has never synced, which every read answers in the vault's own words."""
    return Vault(Path(environ.get("PAPRIKA_VAULT_DIR") or VAULT_DIR),
                 Path(environ.get("PAPRIKA_WANT_DIR") or WANT_DIR), VAULT_TITLE)


def open_vault(
    environ: Mapping[str, str], *, probe: Probe | None = None, transport: Transport | None = None,
) -> tuple[Credentials, SpokeClient | None]:
    """The credentials and the spoke client they report through (the server's).

    `SPOKE_URL` is an address, not a secret. With none there is no spoke client and nothing hears the
    reports; the watcher still surveys and tests. `poll=False`: this server reports and asks for
    nothing, as it is household-only (039 § Servers ask Q Branch)."""
    spoke_url = environ.get("SPOKE_URL") or None
    if transport is None and spoke_url:
        transport = HttpTransport(spoke_url)
    spoke = SpokeClient(transport, time.monotonic, poll=False) if transport is not None else None
    credentials = Credentials(vault_from(environ), spoke.report if spoke is not None else None,
                              default_probe() if probe is None else probe)
    return credentials, spoke


_current: Credentials | None = None


def install(credentials: Credentials) -> None:
    """Make `credentials` the one the tools read. `http.build` does this with the object whose
    watcher it starts, so the tools and the watcher never look at two copies."""
    global _current
    _current = credentials


def current() -> Credentials:
    """The installed credentials; where none is (stdio mode, the nightly export) one is built from
    the environment with no spoke and no watcher."""
    global _current
    if _current is None:
        _current = Credentials(vault_from(os.environ), None, default_probe())
    return _current


def get_credentials(*, quiet: bool = False) -> Held:
    """The Paprika login, from the item and from nowhere else. See `Credentials.paprika`."""
    return current().paprika(quiet=quiet)


def accepted(version: Version) -> None:
    """A login made with this version of the item worked (`utils.get_remote`)."""
    current().accepted(version)


def rejected(version: Version) -> None:
    """A login made with this version of the item was refused (`utils.get_remote`)."""
    current().rejected(version)
