"""THROWAWAY: moneypenny plans/053 spike S3's probe. Deleted with upload_probe.py."""

import hashlib
import json

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from paprika_mcp import upload_probe as probe


def client() -> TestClient:
    return TestClient(Starlette(routes=[Route("/upload/{token}", endpoint=probe.upload, methods=["PUT"])]))


def test_a_good_token_works_once_and_the_answer_is_the_bytes_hash():
    body = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4000
    token = probe.make_token(__import__("time").time())
    first = client().put(f"/upload/{token}", content=body)
    assert first.status_code == 200
    assert json.loads(first.text)["bytes"] == len(body)
    assert json.loads(first.text)["sha256"] == hashlib.sha256(body).hexdigest()
    assert json.loads(first.text)["first"] == "ffd8ffe0"
    again = client().put(f"/upload/{token}", content=body)
    assert (again.status_code, json.loads(again.text)) == (403, {"refused": "used"})


def test_a_forged_or_expired_token_is_refused():
    now = 1_000_000.0
    token = probe.make_token(now)
    exp, nonce, mac = token.split(".")
    assert probe.take(f"{int(exp) + 1}.{nonce}.{mac}", now) == "bad"  # expiry moved, mac no longer fits
    assert probe.take(f"{exp}.{nonce}.{'0' * 32}", now) == "bad"
    assert probe.take("nonsense", now) == "bad"
    assert probe.take(token, now + probe.TTL + 1) == "expired"
    assert probe.take(token, now) is None


def test_more_than_the_cap_is_refused(monkeypatch):
    monkeypatch.setattr(probe, "CAP", 10)
    token = probe.make_token(__import__("time").time())
    answer = client().put(f"/upload/{token}", content=b"x" * 11)
    assert (answer.status_code, json.loads(answer.text)) == (413, {"refused": "too-large"})
