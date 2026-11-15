"""Hermetic by construction: no test may reach a spoke, or read a vault that is not its own.

`control.spoke_client.HttpTransport` is what `credentials.open_vault` builds from `SPOKE_URL`; a test that
wants a spoke injects a transport, and one that lets the real class send fails here. The fixture also
records the attempt and fails at teardown, because the spoke client sends from its own thread, which
swallows what it raises.

The installed credentials are reset around every test (`http.build` installs one, and a module global
that outlives its test would be the next test's login), and the vault and flag directories point at
nothing, so a test never reads `/vault` and never sees a real copy; a test that wants one sets its own.
"""

import pytest
from control.spoke_client import HttpTransport

from paprika_mcp import credentials


@pytest.fixture(autouse=True)
def _no_real_spoke(monkeypatch):
    attempts: list[str] = []

    def refuse(name):
        def blocked(self, path, *args, **kwargs):
            attempts.append(f"HttpTransport.{name}({path!r})")
            raise AssertionError(f"a test reached the real spoke transport: HttpTransport.{name}({path!r}); inject a transport")
        return blocked

    for name in ("get", "post", "post_json"):
        monkeypatch.setattr(HttpTransport, name, refuse(name))
    yield
    assert attempts == []


@pytest.fixture(autouse=True)
def _own_vault(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPRIKA_VAULT_DIR", str(tmp_path / "no-vault"))
    monkeypatch.setenv("PAPRIKA_WANT_DIR", str(tmp_path / "no-want"))
    monkeypatch.delenv("SPOKE_URL", raising=False)
    monkeypatch.setattr(credentials, "_current", None)
    yield
    credentials._current = None
