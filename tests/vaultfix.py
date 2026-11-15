"""A vault copy on disk, as the fetcher writes it, and the doubles the credential tests share."""

from __future__ import annotations

import itertools
import threading
from pathlib import Path

from control.vault import SYNCED, Item, Vault

from paprika_mcp.credentials import VAULT_TITLE

_ids = itertools.count(1)

# Distinctive values, so a log or a text that holds either is found by `in`.
EMAIL = "cook-7f3a@example.invalid"
PASSWORD = "päss word-91x"
FIRST = "2026-11-14T10:00:00Z"
LATER = "2026-11-14T11:00:00Z"


def item_id(n: int | None = None) -> str:
    """A 26-character id; a smaller number sorts first."""
    return f"{n if n is not None else next(_ids):026d}"


class Spy(Vault):
    """A vault that counts how often the flag was asked for."""

    flags = 0

    def raise_flag(self) -> bool:
        self.flags += 1
        return super().raise_flag()


class Copy:
    """One reader's directory and flag directory, as the fetcher leaves them."""

    def __init__(self, root: Path, *, synced: bool = True) -> None:
        self.dir = root / "vault"
        self.want = root / "want"
        self.dir.mkdir()
        self.want.mkdir()
        if synced:
            self.sync()
        self.vault = Spy(self.dir, self.want, VAULT_TITLE)

    @classmethod
    def at(cls, root: Path, *, synced: bool = True) -> Copy:
        root.mkdir()
        return cls(root, synced=synced)

    def sync(self) -> None:
        (self.dir / SYNCED).write_text("1")

    def put(self, n: int, title: str = "Paprika", *, username: str = EMAIL, password: str | None = PASSWORD,
            updated: str = FIRST, fields: dict[str, str] | None = None) -> str:
        found = dict(fields or {})
        if password is not None:
            found["password"] = password
        ident = item_id(n)
        (self.dir / f"{ident}.json").write_text(Item(ident, title, "LOGIN", updated, username, found).to_json())
        return ident

    def remove(self, n: int) -> None:
        (self.dir / f"{item_id(n)}.json").unlink()

    def tear(self, n: int) -> None:
        """An item file the fetcher was part way through writing."""
        (self.dir / f"{item_id(n)}.json").write_text('{"id": "torn')

    def flag_raised(self) -> bool:
        return (self.want / "sync").exists()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Reports:
    """What the spoke client was told: every call, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, slot: str, state: str) -> None:
        self.calls.append((slot, state))

    def last(self) -> str | None:
        return self.calls[-1][1] if self.calls else None


class Probes:
    """The test, recording what it was handed and answering from a script (the last answer repeats)."""

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers) or ["ok"]
        self.calls: list[tuple[str, str, tuple[str, str]]] = []
        self.raises: Exception | None = None
        self.hold: threading.Event | None = None
        self.entered = threading.Event()

    def __call__(self, held):
        self.calls.append((held.username, held.secret, held.version))
        self.entered.set()
        if self.hold is not None:
            self.hold.wait(30)
        if self.raises is not None:
            raise self.raises
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    @property
    def count(self) -> int:
        return len(self.calls)
