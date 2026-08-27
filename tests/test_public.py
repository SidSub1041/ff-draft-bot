"""Public multi-session mode: isolation, auth, and limits.

Runs the real HTTP server in-process with public mode forced on, then
restores local mode so the rest of the suite is unaffected.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from ffbot import server


@pytest.fixture(scope="module")
def public_srv():
    prev = server._PUBLIC
    srv = server.serve(port=0, public=True)
    yield f"http://127.0.0.1:{srv.port}"
    srv.stop()
    server._PUBLIC = prev
    with server._REG_LOCK:
        server._REGISTRY.clear()
        server._IP_BIRTHS.clear()


def call(base, path, body=None, sid=None, expect=200):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=({"Content-Type": "application/json"} if body is not None
                 else {}) | ({"X-FFBot-Session": sid} if sid else {}),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == expect, f"{path}: {r.status} != {expect}"
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        assert e.code == expect, f"{path}: HTTP {e.code}, wanted {expect}"
        return json.loads(e.read().decode() or "{}")


def test_mock_issues_session_and_isolates(public_srv):
    a = call(public_srv, "/api/mock", {"teams": 8, "rounds": 4, "slot": 1})
    b = call(public_srv, "/api/mock", {"teams": 12, "rounds": 4, "slot": 7})
    assert a["session"] and b["session"] and a["session"] != b["session"]

    sa = call(public_srv, "/api/state", sid=a["session"])
    sb = call(public_srv, "/api/state", sid=b["session"])
    assert sa["state"]["teams"] == 8 and sa["state"]["my_slot"] == 1
    assert sb["state"]["teams"] == 12 and sb["state"]["my_slot"] == 7


def test_api_requires_a_session(public_srv):
    call(public_srv, "/api/state", expect=401)
    call(public_srv, "/api/state", sid="local", expect=401)
    call(public_srv, "/api/state", sid="not-a-real-session", expect=401)


def test_ping_and_guide_stay_open(public_srv):
    assert call(public_srv, "/api/ping")["name"] == "ff-draft-bot"
    out = call(public_srv, "/api/guide?query=Kincaid")
    assert out.get("player") or out.get("stats")


def test_review_is_not_served_publicly(public_srv):
    """The operator's draft history is not a public dataset."""
    call(public_srv, "/api/review", sid="anything", expect=404)


def test_panel_page_leaks_no_token(public_srv):
    with urllib.request.urlopen(public_srv + "/app", timeout=10) as r:
        html = r.read().decode()
    assert 'FFBOT_TOKEN=""' in html


def test_per_ip_session_rate_limit(public_srv):
    made = 0
    for _ in range(server.SESSIONS_PER_IP_HOUR + 2):
        out = call(public_srv, "/api/mock",
                   {"teams": 8, "rounds": 2, "slot": 1}, expect=None
                   ) if False else None
    # do it plainly: keep creating until refused
    got_429 = False
    for _ in range(server.SESSIONS_PER_IP_HOUR + 2):
        req = urllib.request.Request(
            public_srv + "/api/mock",
            data=json.dumps({"teams": 8, "rounds": 2, "slot": 1}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
            made += 1
        except urllib.error.HTTPError as e:
            if e.code == 429:
                got_429 = True
                break
            raise
    assert got_429, f"made {made} sessions with no rate limit"
