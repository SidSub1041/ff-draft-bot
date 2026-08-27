"""Accounts: Google flow (stubbed), ownership boundaries, review fallback."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ffbot import accounts, server
from ffbot.store import Store


class _Google(BaseHTTPRequestHandler):
    """Plays Google's token + tokeninfo endpoints."""

    def do_POST(self):                    # token exchange
        out = json.dumps({"id_token": "fake-jwt"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):                     # tokeninfo
        out = json.dumps({"aud": "test-client", "sub": "sub-123",
                          "iss": "https://accounts.google.com",
                          "email": "s@example.test", "name": "Sid T"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    gsrv = ThreadingHTTPServer(("127.0.0.1", 0), _Google)
    threading.Thread(target=gsrv.serve_forever, daemon=True).start()
    import os
    os.environ["GOOGLE_CLIENT_ID"] = "test-client"
    os.environ["GOOGLE_CLIENT_SECRET"] = "test-secret"
    base = f"http://127.0.0.1:{gsrv.server_port}"
    os.environ["FFBOT_GOOGLE_TOKEN_URL"] = base + "/token"
    os.environ["FFBOT_GOOGLE_TOKENINFO_URL"] = base + "/tokeninfo"
    srv = server.serve(port=0)
    yield f"http://127.0.0.1:{srv.port}", srv.token
    srv.stop()
    gsrv.shutdown()
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
              "FFBOT_GOOGLE_TOKEN_URL", "FFBOT_GOOGLE_TOKENINFO_URL"):
        os.environ.pop(k, None)


def call(base, path, tok, body=None, cookie="", redirects=False,
         expect=200):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(
        *([] if redirects else [NoRedirect]))
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-FFBot-Token": tok,
                 **({"Content-Type": "application/json"}
                    if body is not None else {}),
                 **({"Cookie": cookie} if cookie else {})})
    try:
        with opener.open(req, timeout=20) as r:
            assert r.status == expect
            return dict(r.headers), (json.loads(r.read().decode() or "{}")
                                     if expect == 200 else {})
    except urllib.error.HTTPError as e:
        assert e.code == expect, f"{path}: {e.code} != {expect}"
        return dict(e.headers), {}


def _login(base, tok):
    state = accounts.make_state()
    heads, _ = call(base, f"/auth/google/callback?code=x&state={state}",
                    tok, expect=302)
    cookie = heads.get("Set-Cookie", "").split(";")[0]
    assert cookie.startswith("ffbot_auth=") and len(cookie) > 20
    return cookie


def test_google_callback_sets_cookie_and_account_works(rig):
    base, tok = rig
    cookie = _login(base, tok)
    _, me = call(base, "/api/account", tok, cookie=cookie)
    assert me["signed_in"] and me["user"]["email"] == "s@example.test"


def test_bad_state_rejected(rig):
    base, tok = rig
    call(base, "/auth/google/callback?code=x&state=forged.1.deadbeef",
         tok, expect=400)


def test_my_drafts_needs_auth_and_owns_only_mine(rig):
    base, tok = rig
    call(base, "/api/my/drafts", tok, expect=401)
    cookie = _login(base, tok)
    # connect a small mock while signed in -> archived under the account
    call(base, "/api/mock", tok, body={"teams": 8, "rounds": 3, "slot": 1},
         cookie=cookie)
    _, mine = call(base, "/api/my/drafts", tok, cookie=cookie)
    assert mine["drafts"], "signed-in mock was not archived"
    did = mine["drafts"][0]["draft_id"]
    # another user must not be able to read it
    other = Store().upsert_user("google", "someone-else", "e@e", "E")
    other_cookie = "ffbot_auth=" + Store().create_auth(other["id"])
    call(base, f"/api/my/draft?draft_id={did}", tok,
         cookie=other_cookie, expect=404)
    # the owner can, and review chat answers without an LLM
    _, arch = call(base, f"/api/my/draft?draft_id={did}", tok, cookie=cookie)
    assert arch["meta"]["draft_id"] == did
    _, rev = call(base, "/api/my/review_chat", tok,
                  body={"draft_id": did, "message": "how did I do"},
                  cookie=cookie)
    assert rev["output"] and rev["engine"] == "builtin"


def test_logout_kills_the_session(rig):
    base, tok = rig
    cookie = _login(base, tok)
    call(base, "/api/logout", tok, body={}, cookie=cookie)
    call(base, "/api/my/drafts", tok, cookie=cookie, expect=401)


def test_espn_pick_parsing():
    """The provider maps ESPN's draftDetail shape onto our state."""
    from ffbot.espn import EspnProvider
    from ffbot.mockdraft import synthetic_state

    prov = EspnProvider("1", 2026)
    prov._players = {10: {"name": "Test Back", "pos": "RB", "team": "KC"},
                     11: {"name": "Test Wideout", "pos": "WR", "team": "GB"}}
    prov._load_players = lambda: None
    state = synthetic_state(teams=2, rounds=2, my_slot=1)
    state.provider = prov
    prov.my_team_id = 7
    prov._apply_picks(state, {"draftDetail": {"picks": [
        {"overallPickNumber": 1, "roundId": 1, "teamId": 7, "playerId": 10},
        {"overallPickNumber": 2, "roundId": 1, "teamId": 9, "playerId": 11},
    ]}})
    assert [p.name for p in state.picks] == ["Test Back", "Test Wideout"]
    assert state.picks[0].slot == 1 and state.picks[1].slot == 2
    assert state.my_slot == 1
