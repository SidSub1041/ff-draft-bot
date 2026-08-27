"""Local web panel - the draft board as a page on 127.0.0.1.

A third surface on the same engine: the CLI is a REPL, the MCP plugin is
Claude's view, and this serves a one-page panel you can park next to the
Sleeper tab.  Stdlib only, like the rest of the runtime.

Security posture, because this runs inside a browser that is logged into
everything else you own:
  * loopback only, never 0.0.0.0
  * a per-process random token, compared in constant time, required on every
    /api/ call and injected into the page server-side so it never rides in a
    URL or has to be pasted
  * any request carrying a foreign Origin is refused before it reaches a
    handler, which is what stops a hostile page in another tab from driving
    the panel through your browser
  * exactly three static files, resolved through an allowlist, so nothing in
    the request path can escape ffbot/web

Still advisory only: nothing here writes to Sleeper.
"""

from __future__ import annotations

import dataclasses
import errno
import hmac
import json
import re
import secrets
import sqlite3
import sys
import contextvars
import os
import threading
from collections import deque
import time
import traceback
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import explain, llm, sleeper, yahoo
from .cli import chat
from .engine import Engine
from .guide import get_guide
from .mockdraft import autopick, synthetic_state
from .store import DB_PATH, SCHEMA, Store
from .strategy import PRESETS, Strategy, for_league

try:                                    # the natural-language front end
    from . import nlu
except ImportError:                     # not built yet - commands still work
    nlu = None                          # type: ignore[assignment]

DEFAULT_PORT = 8770

# The browser extension docks this panel inside the Sleeper draft page, so the
# panel has to be framable - but only there. Without this header any site the
# user visits could frame a loopback panel and try to bait clicks through it.
# A cross-origin frame still cannot read the document, so the API token is not
# at risk either way; this simply removes the clickjacking surface.
FRAME_ANCESTORS = (
    "'self' https://sleeper.com https://*.sleeper.com "
    "https://sleeper.app https://*.sleeper.app"
)
FRAME_POLICY = f"frame-ancestors {FRAME_ANCESTORS}"

# Chrome guards requests that go from a public site to a private address
# (Private Network Access). The browser extension docks this panel inside
# sleeper.com, which is exactly that shape, so the panel has to opt in: answer
# the preflight and say the private-network hop is intended. Only the Sleeper
# origins are ever answered, and /api/* still demands the token, so this widens
# nothing beyond letting the sidebar load.
SIDEBAR_ORIGINS = frozenset({
    "https://sleeper.com", "https://www.sleeper.com",
    "https://sleeper.app", "https://www.sleeper.app",
})


def sidebar_cors(origin: str | None) -> list[tuple[str, str]]:
    """CORS/PNA headers for a Sleeper-origin request, or nothing at all."""
    if not origin or origin not in SIDEBAR_ORIGINS:
        return []
    return [
        ("Access-Control-Allow-Origin", origin),
        ("Access-Control-Allow-Private-Network", "true"),
        ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
        ("Access-Control-Allow-Headers", "X-FFBot-Token, Content-Type"),
        ("Vary", "Origin"),
    ]
MAX_BODY = 64 * 1024
# Rather than pile request threads up behind one slow Sleeper call, give up.
LOCK_TIMEOUT = 45.0
# Deep enough that `why` and `compare` almost always find their man, shallow
# enough that both share one cache entry.
LOOKUP_DEPTH = 40
# Below this, the built-in parse is a guess, so it is worth asking a model to
# look at the sentence again - when there is one, which today there is not.
LOW_CONFIDENCE = 0.45
# Rewriting a scored shortlist as prose loses the columns that make it
# readable, so phrasing is only offered a short, prose-shaped answer.
PHRASE_MAX_LINES = 8
PHRASE_MAX_CHARS = 600

WEB = Path(__file__).resolve().parent / "web"

# The only paths that map to a file.  An allowlist, not a join against the
# request path, so no amount of ../ gets anywhere.
STATIC: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}

PLACEHOLDER = """<!doctype html>
<meta charset="utf-8"><title>ff-draft-bot</title>
<body style="font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
             max-width:40rem;margin:4rem auto;padding:0 1rem">
<h1>ff-draft-bot panel</h1>
<p>The API is up, but <code>ffbot/web/index.html</code> has not been built
yet, so there is no UI to show.</p>
<p>Everything still works over HTTP - try <code>GET /api/ping</code>, and see
the token in <code>window.FFBOT_TOKEN</code> for the rest.</p>
"""


# --------------------------------------------------------------- plumbing


class ApiError(Exception):
    """An error with an HTTP status, rendered as {"error": ...}."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: list = field(default_factory=list)


@dataclass
class Req:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    body: dict = field(default_factory=dict)
    panel: "PanelServer | None" = None
    headers: Any = None


def _json(status: int, payload: Any) -> Response:
    body = json.dumps(payload, default=str).encode("utf-8")
    return Response(status, body, "application/json; charset=utf-8")


def _int(req: Req, name: str, default: int, lo: int, hi: int) -> int:
    raw = req.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ApiError(400, f"{name} must be a whole number") from None
    if not lo <= val <= hi:
        raise ApiError(400, f"{name} must be between {lo} and {hi}")
    return val


def _flag(body: dict, name: str, default: bool = False) -> bool:
    val = body.get(name, default)
    return bool(val) if not isinstance(val, str) else val.lower() in (
        "1", "true", "yes", "on")


# ---------------------------------------------------------------- session


class PanelStore(Store):
    """Store whose SQLite connection is allowed to cross request threads.

    ThreadingHTTPServer hands every request to a fresh thread and sqlite3
    refuses, by default, to use a connection opened in another one.  The
    engine lock below already serialises every touch of the store, so that
    check is the only thing in the way.
    """

    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()


# Draft sessions.  Locally there is exactly one, named "local", and nothing
# about the old single-user panel changes.  In public mode (--public) every
# /api/connect or /api/mock creates a fresh session whose unguessable id is
# the caller's bearer credential, and an idle sweeper retires forgotten ones.
_PUBLIC = (os.environ.get("FFBOT_PUBLIC") or "").strip().lower() in (
    "1", "true", "yes", "on")
LOCAL_SID = "local"
SESSION_TTL = max(600, int(os.environ.get("FFBOT_SESSION_TTL") or 6 * 3600))
MAX_SESSIONS = max(2, int(os.environ.get("FFBOT_MAX_SESSIONS") or 150))
SESSIONS_PER_IP_HOUR = 6


class DraftSession:
    """Everything one draft's panel needs, isolated from every other one."""

    __slots__ = ("sid", "engine", "last_recs", "chat_history", "deep_detail",
                 "rec_cache", "rec_stamp", "epoch", "lock", "created",
                 "last_seen", "client_ip", "yahoo")

    def __init__(self, sid: str, client_ip: str = "") -> None:
        self.sid = sid
        self.engine: Engine | None = None
        self.last_recs: list = []
        self.chat_history: deque = deque(maxlen=16)
        self.deep_detail: str = ""
        self.rec_cache: dict[tuple, tuple[dict, list]] = {}
        self.rec_stamp: tuple | None = None
        self.epoch = 0
        self.lock = threading.RLock()
        self.created = time.time()
        self.last_seen = time.time()
        self.client_ip = client_ip
        self.yahoo: dict | None = None      # OAuth tokens, public+yahoo only


_REGISTRY: dict[str, DraftSession] = {}
_REG_LOCK = threading.Lock()
_IP_BIRTHS: dict[str, deque] = {}
_CTX: "contextvars.ContextVar[DraftSession | None]" = \
    contextvars.ContextVar("ffbot_session", default=None)
_store: Store | None = None


def _cur() -> DraftSession:
    ses = _CTX.get()
    if ses is not None:
        return ses
    # Outside a request (tests, the CLI pre-connect) fall back to the local
    # session so module-level helpers keep working unchanged.
    return _get_or_create(LOCAL_SID)


def _get_or_create(sid: str, client_ip: str = "") -> DraftSession:
    with _REG_LOCK:
        ses = _REGISTRY.get(sid)
        if ses is None:
            ses = DraftSession(sid, client_ip)
            _REGISTRY[sid] = ses
        return ses


def _new_session(client_ip: str) -> DraftSession:
    """A fresh public session, or a clear refusal.  Registry lock inside."""
    now = time.time()
    with _REG_LOCK:
        births = _IP_BIRTHS.setdefault(client_ip, deque(maxlen=32))
        while births and now - births[0] > 3600:
            births.popleft()
        if len(births) >= SESSIONS_PER_IP_HOUR:
            raise ApiError(429, "too many new drafts from this address - "
                                "try again in a bit")
        live = [x for x in _REGISTRY.values() if x.sid != LOCAL_SID]
        if len(live) >= MAX_SESSIONS:
            raise ApiError(503, "the service is at capacity right now")
        births.append(now)
        ses = DraftSession(secrets.token_urlsafe(18), client_ip)
        _REGISTRY[ses.sid] = ses
        return ses


def _sweep_sessions() -> int:
    """Drop idle public sessions; the local one never expires."""
    cutoff = time.time() - SESSION_TTL
    with _REG_LOCK:
        dead = [sid for sid, x in _REGISTRY.items()
                if sid != LOCAL_SID and x.last_seen < cutoff]
        for sid in dead:
            del _REGISTRY[sid]
    return len(dead)


def _sweeper_loop() -> None:
    while True:
        time.sleep(300)
        try:
            n = _sweep_sessions()
            if n:
                print(f"[ffbot] retired {n} idle session(s)", file=sys.stderr)
        except Exception:
            pass


@contextmanager
def engine_lock():
    """Exclusive access to the current session's state, with a deadline."""
    lock = _cur().lock
    if not lock.acquire(timeout=LOCK_TIMEOUT):
        raise ApiError(503, "engine busy - a Sleeper call is still running")
    try:
        yield
    finally:
        lock.release()


def _need() -> Engine:
    engine = _cur().engine
    if engine is None:
        raise ApiError(409, "no draft connected")
    return engine


def _shared_store() -> Store:
    """The one store every engine in this process writes through."""
    global _store
    if _store is None:
        _store = PanelStore()
    return _store


def _install(engine: Engine) -> None:
    """Swap a new draft into the current session.  Caller holds its lock."""
    ses = _cur()
    ses.engine = engine
    ses.last_recs = []
    ses.chat_history.clear()
    ses.deep_detail = ""
    ses.rec_cache.clear()
    ses.rec_stamp = None
    ses.epoch += 1


def _state_blob(e: Engine) -> dict:
    """The draft in one JSON object.

    Deliberately the same field names as `mcp_server._state_blob`, plus the
    three the panel needs on top, so the plugin and the page never describe
    the same draft two different ways.
    """
    st = e.state
    return {
        "draft_id": st.draft_id,
        "name": st.name,
        "teams": st.teams,
        "rounds": st.rounds,
        "draft_type": st.draft_type,
        "scoring": st.scoring,
        "superflex": st.superflex,
        "dynasty": st.is_dynasty,
        "guide_board": st.fmt,
        "my_slot": st.my_slot,
        "status": st.status,
        "picks_made": len(st.picks),
        "total_picks": st.total_picks,
        "current_round": st.current_round,
        "next_pick_no": st.next_pick_no,
        "on_the_clock_slot": st.on_the_clock_slot,
        "is_my_turn": st.is_my_turn,
        "picks_until_my_turn": st.picks_until_my_turn(),
        "my_next_picks": st.my_upcoming_picks(4),
        "roster_slots": st.roster_slots,
        "strategy": e.strategy.name,
        "my_roster": [
            {"pick": p.pick_no, "round": p.round, "pos": p.pos, "name": p.name,
             "team": p.team}
            for p in sorted(st.my_roster(), key=lambda x: x.pick_no)
        ],
    }


def _state_response() -> dict:
    if _cur().engine is None:
        return {"connected": False, "state": None}
    return {"connected": True, "state": _state_blob(_cur().engine)}


# ------------------------------------------------------- recommendation cache


def _stamp(e: Engine) -> tuple:
    """Everything a recommendation depends on, cheaply fingerprinted."""
    strat = json.dumps(e.strategy.to_dict(), sort_keys=True, default=str)
    return (_cur().epoch, len(e.state.picks), e.state.my_slot, hash(strat))


def _rec_payload(e: Engine, recs: list, pick: int | None) -> dict:
    target = pick or (e.state.my_upcoming_picks(1) or [e.state.next_pick_no])[0]
    rnd = min(e.state.rounds, (target - 1) // e.state.teams + 1)
    return {
        "pick_no": target,
        "round": rnd,
        "header": explain.pick_header(e, target),
        "script_says": e.strategy.target_for_round(rnd),
        "strategy": e.strategy.name,
        "recommendations": [
            dict(r.to_dict(), explanation=explain.explain(e, r, target))
            for r in recs
        ],
    }


def _recommend(e: Engine, n: int, pos: str | None,
               pick: int | None, sims: int | None = None) -> tuple[dict, list]:
    """recommend() behind a cache, keyed by what was asked for.

    The panel polls.  Re-running a 400-draft Monte Carlo every three seconds
    would heat the laptop to produce the identical answer, because nothing
    that feeds the simulation moves until a pick lands or the strategy does -
    which is exactly what the stamp watches.  Caller holds the lock.
    """
    stamp = _stamp(e)
    if stamp != _cur().rec_stamp:
        _cur().rec_cache.clear()
        _cur().rec_stamp = stamp
    key = (pick, n, pos, sims)
    hit = _cur().rec_cache.get(key)
    if hit is None:
        recs = e.recommend(n, pick_no=pick, pos_filter=pos, sim_runs=sims)
        hit = (_rec_payload(e, recs, pick), recs)
        _cur().rec_cache[key] = hit
    if hit[1]:
        # So a follow-up "why X" in the chat box scores against the same list
        # the user is looking at.
        _cur().last_recs = hit[1]
    return hit


# ------------------------------------------------------------ session setup


def _players() -> dict[str, dict]:
    """Player universe, fetched outside the lock.  A mock survives without it."""
    try:
        return sleeper.load_players()
    except sleeper.SleeperError as e:
        print(f"[ffbot] no Sleeper player file ({e}); running guide-only",
              file=sys.stderr)
        return {}


def connect_draft(draft: str, user: str = "", slot: int = 0,
                  strategy: str = "") -> dict:
    """Attach to a live or mock Sleeper draft.  Read-only, as everywhere else.

    The three network calls run *outside* the engine lock: on a cold player
    cache they take tens of seconds, and none of them touch shared state, so
    a poll already in flight has no reason to wait for them.  Only the swap
    itself is locked.
    """
    draft_id = sleeper.extract_draft_id(draft)
    uid = sleeper.user_id(user) if user else None
    players = sleeper.load_players()
    state = sleeper.build_state(draft_id, my_user_id=uid,
                                my_slot=slot or None, players=players)
    strat = Strategy.load(strategy) if strategy else \
        for_league(state.teams, state.superflex, state.is_dynasty)
    with engine_lock():
        _install(Engine(state, strategy=strat, store=_shared_store(),
                        players=players))
        out = _state_response()
    if state.my_slot is None:
        out["warning"] = ("Draft slot unknown - pass your Sleeper username or "
                          "a slot number so the advice can be roster-aware.")
    return out


def start_offline_mock(teams: int = 12, rounds: int = 15, slot: int = 5,
                       scoring: str = "ppr", superflex: bool = False,
                       dynasty: bool = False, upto: int = 0,
                       strategy: str = "") -> dict:
    """Rehearsal draft with no Sleeper room; rivals are simulated."""
    state = synthetic_state(teams=teams, rounds=rounds, my_slot=slot,
                            scoring=scoring, superflex=superflex,
                            dynasty=dynasty)
    strat = Strategy.load(strategy) if strategy else \
        for_league(teams, superflex, dynasty)
    players = _players()
    with engine_lock():
        engine = Engine(state, strategy=strat, store=_shared_store(),
                        players=players)
        _install(engine)
        if upto > 1:
            autopick(engine, upto)
        return _state_response()


# ---------------------------------------------------------------- handlers


def _api_ping(req: Req) -> dict:
    return {"ok": True, "name": "ff-draft-bot"}


def _api_state(req: Req) -> dict:
    with engine_lock():
        return _state_response()


def _api_connect(req: Req) -> dict:
    platform = str(req.body.get("platform") or "sleeper").strip().lower()
    if platform == "yahoo":
        return _connect_yahoo(req)
    draft = str(req.body.get("draft") or "").strip()
    if not draft:
        raise ApiError(400, "draft id or sleeper.com URL is required")
    slot = req.body.get("slot") or 0
    return connect_draft(draft, user=str(req.body.get("user") or ""),
                         slot=int(slot), strategy=str(req.body.get("strategy")
                                                      or ""))


def _connect_yahoo(req: Req) -> dict:
    """Attach to a Yahoo league's draft.  BETA - see ffbot/yahoo.py."""
    if not yahoo.configured():
        raise ApiError(404, "Yahoo support is not enabled on this server")
    ses = _cur()
    if not ses.yahoo:
        raise ApiError(409, "link Yahoo first: open /auth/yahoo/start")
    league_key = str(req.body.get("league_key") or "").strip()
    if not league_key:
        raise ApiError(400, "league_key is required (see /api/yahoo/leagues)")
    provider = yahoo.YahooProvider(
        ses.yahoo, league_key,
        my_team_key=str(req.body.get("team_key") or "") or None)
    state = provider.build_state()
    slot = req.body.get("slot")
    if slot:
        state.my_slot = int(slot)
    strat_name = str(req.body.get("strategy") or "")
    strat = Strategy.load(strat_name) if strat_name else None
    engine = Engine(state, strategy=strat, store=_shared_store(),
                    players=_players())
    with engine_lock():
        _install(engine)
        blob = {"connected": True, "state": _state_blob(engine),
                "platform": "yahoo",
                "warning": ("Yahoo support is beta: pick updates depend on "
                            "Yahoo's draftresults feed. Rehearse in a Yahoo "
                            "mock before a league that matters."
                            + ("" if state.my_slot else " Your slot is "
                               "unknown - pass slot or team_key."))}
    return blob


def _api_mock(req: Req) -> dict:
    b = req.body
    return start_offline_mock(
        teams=int(b.get("teams") or 12), rounds=int(b.get("rounds") or 15),
        slot=int(b.get("slot") or 5), scoring=str(b.get("scoring") or "ppr"),
        superflex=_flag(b, "superflex"), dynasty=_flag(b, "dynasty"),
        upto=int(b.get("upto") or 0), strategy=str(b.get("strategy") or ""))


def _api_refresh(req: Req) -> dict:
    """Cheap enough to poll every few seconds.

    The Sleeper fetch stays inside the lock on purpose: it mutates the shared
    DraftState, so it cannot run alongside a recommend() reading the same
    board.  It is two small GETs, and the lock deadline above means a hung
    Sleeper makes the panel slow rather than stuck.
    """
    with engine_lock():
        e = _need()
        # An offline mock has no room to poll, so never touch the network.
        events = e.refresh(fetch=e.state.draft_id != "offline")
        return {"events": events, "state": _state_blob(e),
                "changed": bool(events)}


def _api_recommend(req: Req) -> dict:
    n = _int(req, "n", 5, 1, 30)
    pos = (req.query.get("pos") or "").strip().upper() or None
    if pos and pos not in sleeper.FANTASY_POS:
        raise ApiError(400, f"unknown position {pos!r}")
    pick = _int(req, "pick", 0, 0, 1000) or None
    with engine_lock():
        e = _need()
        if pick and pick > e.state.total_picks:
            raise ApiError(400, f"this draft ends at pick {e.state.total_picks}")
        return _recommend(e, n, pos, pick)[0]


def _api_player(req: Req) -> dict:
    name = (req.query.get("name") or "").strip()
    if not name:
        raise ApiError(400, "name is required")
    with engine_lock():
        e = _need()
        key = e.resolve_key(name)
        if not key:
            raise ApiError(404, f"no player matching {name!r}")
        proj = e.projection(key)
        if proj is None:
            raise ApiError(404, f"no projection for {name!r}")
        gp = e.guide.players.get(key)
        recs = _recommend(e, LOOKUP_DEPTH, None, None)[1]
        match = next((r for r in recs if r.key == key), None)
        out = {
            "name": proj.name,
            "pos": proj.pos,
            "available": e.is_available(key),
            "projected_ppg": round(proj.ppg, 2),
            "floor": round(proj.floor, 2),
            "ceiling": round(proj.upside, 2),
            "sigma": round(proj.sigma, 2),
            "vor": round(proj.vor, 2),
            "replacement": round(proj.replacement, 2),
            "guide_pos_rank": proj.pos_rank,
            "guide_overall_rank": proj.overall_rank,
            "adj_ppg_2025": gp.adj_ppg_2025 if gp else None,
            "adp": round(e.adp.adp_of(proj.name, proj.pos), 1),
            "tier": list(e.model.tier_of(key) or []) or None,
            "guide_notes": proj.notes,
            "off_guide": key in e._off_guide,
        }
        if match:
            out["scored_this_pick"] = match.to_dict()
            out["explanation"] = explain.explain(e, match)
        else:
            out["explanation"] = (
                f"{proj.name} is not in the shortlist for this pick - the "
                f"engine does not rate him here.")
        return out


def _api_compare(req: Req) -> dict:
    a = (req.query.get("a") or "").strip()
    b = (req.query.get("b") or "").strip()
    if not a or not b:
        raise ApiError(400, "both a and b are required")
    with engine_lock():
        e = _need()
        recs = _recommend(e, LOOKUP_DEPTH, None, None)[1]
        return {"comparison": explain.compare(e, recs, a, b)}


def _api_board(req: Req) -> dict:
    with engine_lock():
        e = _need()
        if e.last_sim is None or not e.last_sim.runs:
            _recommend(e, 5, None, None)   # fills in the survival curves
        return {
            "outlook": e.positional_outlook(),
            "summary": explain.board_summary(e),
            "best_available": [
                {"name": p.name, "pos": p.pos, "guide_pos_rank": p.pos_rank,
                 "ppg": round(p.ppg, 1),
                 "adp": round(e.adp.adp_of(p.name, p.pos), 1)}
                for p in e.available()[:20]
            ],
        }


def _api_boardgrid(req: Req) -> dict:
    """The draft board as a rectangle, one row per round, one column per slot.

    Cells come back in *slot* order rather than pick order, so the page can
    lay the grid out straight through without knowing anything about snakes,
    third-round reversals or linear drafts - `slot_for_pick` already knows,
    and it is the same function the simulator drafts against.
    """
    with engine_lock():
        e = _need()
        st = e.state
        by_pick = {p.pick_no: p for p in st.picks}
        slots = list(range(1, st.teams + 1))
        current = st.next_pick_no if st.next_pick_no <= st.total_picks else None

        rows = []
        for rnd in range(1, st.rounds + 1):
            cells: dict[int, dict] = {}
            for offset in range(st.teams):
                pick_no = (rnd - 1) * st.teams + offset + 1
                slot = st.slot_for_pick(pick_no)
                pick = by_pick.get(pick_no)
                cells[slot] = {
                    "slot": slot,
                    "pick_no": pick_no,
                    "name": pick.name if pick else None,
                    "pos": pick.pos if pick else None,
                    "team": pick.team if pick else None,
                    "is_me": slot == st.my_slot,
                    "is_current": pick_no == current,
                    "empty": pick is None,
                }
            rows.append({"round": rnd, "cells": [cells[s] for s in slots]})

        return {
            "teams": st.teams,
            "rounds": st.rounds,
            "my_slot": st.my_slot,
            "current_pick": current,
            "on_clock_slot": st.on_the_clock_slot,
            # slot_names holds Sleeper user ids, not display names, so a
            # column header made from one would be a wall of digits.
            "columns": [{"slot": s, "label": "YOU" if s == st.my_slot
                         else f"T{s}", "is_me": s == st.my_slot}
                        for s in slots],
            "rows": rows,
        }


def _api_bypos(req: Req) -> dict:
    """The shortlist split four ways, so every position is visible at once.

    Each list is scored by `recommend(pos_filter=...)`, not sorted by rank, so
    what shows up under RB is what the active strategy would actually take -
    and every call goes through the same cache the single-column view uses,
    because otherwise one page load is four Monte Carlo runs.
    """
    n = _int(req, "n", 6, 1, 15)
    with engine_lock():
        e = _need()
        # The outlook reads survival curves off the last simulation, so make
        # sure one exists before any of it is quoted.
        if e.last_sim is None or not e.last_sim.runs:
            _recommend(e, 5, None, None)

        positions: dict[str, list[dict]] = {}
        shown: list = []
        for pos in ("QB", "RB", "WR", "TE"):
            recs = _recommend(e, n, pos, None, sims=120)[1]
            positions[pos] = [r.to_dict() for r in recs]
            shown += recs

        # _recommend leaves _cur().last_recs pointing at whichever position ran
        # last; on this page the user is looking at all four, so a follow-up
        # "why him" should be able to find any of them.
        if shown:
            _cur().last_recs = shown

        target = (e.state.my_upcoming_picks(1) or [e.state.next_pick_no])[0]
        rnd = min(e.state.rounds, (target - 1) // e.state.teams + 1)
        return {
            "pick_no": target,
            "round": rnd,
            "script_says": e.strategy.target_for_round(rnd),
            "positions": positions,
            "outlook": e.positional_outlook(),
        }


def _api_room(req: Req) -> dict:
    with engine_lock():
        e = _need()
        return {
            "summary": explain.opponent_summary(e),
            "field_positional_share": {k: round(v, 3) for k, v
                                       in e.opponents.field_share.items()},
            "run_pressure": {p: round(e.opponents.run_pressure(p), 3)
                             for p in ("QB", "RB", "WR", "TE")},
            "slots": [
                {"slot": s, "is_me": s == e.state.my_slot, "read": desc,
                 "roster": [{"round": pk.round, "pos": pk.pos, "name": pk.name}
                            for pk in e.state.roster_of_slot(s)]}
                for s, desc in e.opponents.summary()
            ],
        }


def _api_roster(req: Req) -> dict:
    with engine_lock():
        e = _need()
        return {"summary": explain.roster_summary(e),
                "counts": e.my_counts(),
                "rb_inside_guide_top_n": e.my_rb_top_n(),
                "state": _state_blob(e)}


def _api_strategy(req: Req) -> dict:
    with engine_lock():
        e = _need()
        return {"summary": explain.strategy_summary(e),
                "presets": Strategy.available(),
                "config": e.strategy.to_dict()}


def _api_set_strategy(req: Req) -> dict:
    preset = str(req.body.get("preset") or "").strip()
    settings = req.body.get("settings") or {}
    save_as = str(req.body.get("save_as") or "").strip()
    if not isinstance(settings, dict):
        raise ApiError(400, "settings must be an object of field -> value")
    if any(not isinstance(k, str) for k in settings):
        raise ApiError(400, "settings keys must be strategy field names")

    with engine_lock():
        e = _need()
        log: list[str] = []
        if preset:
            if preset not in PRESETS and preset not in Strategy.available():
                raise ApiError(404, f"unknown strategy {preset!r}")
            e.strategy = Strategy.load(preset)
            log.append(f"preset -> {preset}")
        if settings:
            log += e.strategy.adjust(**settings)
        if save_as:
            log.append(f"saved to {e.strategy.save(save_as)}")
        if log:
            e.store.add_feedback(e.state.draft_id, "strategy", "; ".join(log),
                                 {"preset": preset, "settings": settings})
        return {"changes": log, "summary": explain.strategy_summary(e),
                "config": e.strategy.to_dict()}


def _api_bump(req: Req) -> dict:
    name = str(req.body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "name is required")
    try:
        delta = float(req.body.get("delta"))
    except (TypeError, ValueError):
        raise ApiError(400, "delta must be a number of projected points") from None
    reason = str(req.body.get("reason") or "manual bump")
    with engine_lock():
        e = _need()
        key = e.resolve_key(name)
        if not key:
            raise ApiError(404, f"no player matching {name!r}")
        e.strategy.player_bumps[key] = delta
        e.store.set_player_bias(key, delta, reason)
        e.biases = e.store.player_biases()
        proj = e.projection(key)
        return {"player": proj.name if proj else key, "delta": delta}


def _api_ban(req: Req) -> dict:
    name = str(req.body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "name is required")
    with engine_lock():
        e = _need()
        key = e.resolve_key(name)
        if not key:
            raise ApiError(404, f"no player matching {name!r}")
        if key not in e.strategy.banned:
            e.strategy.banned.append(key)
        return {"banned": key, "count": len(e.strategy.banned)}


def _api_note(req: Req) -> dict:
    text = str(req.body.get("text") or "").strip()
    if not text:
        raise ApiError(400, "text is required")
    kind = str(req.body.get("kind") or "note")
    with engine_lock():
        store = _cur().engine.store if _cur().engine else _shared_store()
        draft_id = _cur().engine.state.draft_id if _cur().engine else None
        return {"recorded": store.add_feedback(draft_id, kind, text)}


# --------------------------------------------------------------------- chat

# Every word the old CLI router answers to.  Typing one still runs the old
# command: the muscle memory from a season of mock drafts is worth more than
# a prettier parse, and the CLI and the panel stay one program.
CLI_COMMANDS = {
    "help", "?", "rec", "why", "explain", "compare", "cmp", "vs", "board",
    "room", "roster", "strategy", "preset", "set", "bump", "ban", "note",
    "stats", "player", "quit", "exit", "q",
}

# ...but half of those are also ordinary English.  "why" opens a command and
# a sentence equally well, so an ambiguous head only counts as a command when
# the rest of the message is shaped like arguments rather than a question.
# Below this parse confidence an ambiguous head is handed back to the old
# router, which still knows the bare forms ("board", "room") verbatim.
AMBIGUOUS_FLOOR = 0.45

CLI_AMBIGUOUS = {"why", "explain", "compare", "vs", "board", "room", "roster",
                 "strategy", "player"}

# Function words that give a sentence away.  A command line has none of them.
SENTENCE_WORDS = {
    "a", "about", "am", "an", "and", "are", "be", "best", "better", "can",
    "did", "do", "does", "for", "get", "good", "how", "i", "if", "is", "it",
    "look", "looks", "me", "my", "need", "of", "on", "or", "our", "should",
    "take", "tell", "than", "that", "the", "think", "to", "us", "want", "was",
    "we", "what", "when", "which", "who", "worse", "worst", "would", "you",
}

# Intents that only read the board.  Anything else is assumed to have retuned
# the engine, so the recommendation cache can no longer be trusted.
READ_ONLY_INTENTS = {
    "recommend", "recommend_pos", "why", "compare", "availability", "board",
    "roster", "room", "player_info", "help", "greeting", "stats", "guide",
    "unknown",
}

# What the NLU calls a shrug.  These come back as understood: false.
UNKNOWN_INTENTS = {"", "unknown", "unclear", "fallback", "none", "noop"}

DEFAULT_SUGGESTIONS: dict[str, list[str]] = {
    "recommend": ["Why him?", "Compare the top two", "Who else at RB?"],
    "recommend_pos": ["Why him?", "Who should I take overall?",
                      "How does the board look?"],
    "why": ["Compare him to the next guy", "Will he last to my next pick?",
            "What does my roster need?"],
    "compare": ["Why the winner?", "Who should I take?",
                "What does my roster need?"],
    "availability": ["Who should I take instead?", "How does the board look?",
                     "Why him?"],
    "board": ["Who should I take?", "What are the other teams doing?",
              "Who is left at WR?"],
    "roster": ["Who should I take?", "What position do I need most?",
               "How does the board look?"],
    "room": ["Who should I take?", "How does the board look?",
             "Will there be a run at RB?"],
    "strategy_set": ["Who should I take now?", "What is my strategy?",
                     "How does the board look?"],
    "player_info": ["Should I take him?", "Compare him to someone",
                    "Will he last to my next pick?"],
}

FALLBACK_SUGGESTIONS = ["Who should I take?", "How does the board look?",
                        "What does my roster need?"]


def _is_command(message: str) -> bool:
    """True when the message should go to the old CLI router verbatim."""
    parts = message.split()
    if not parts:
        return False
    head = parts[0].lower().strip(",:")
    if head not in CLI_COMMANDS:
        return False
    if head not in CLI_AMBIGUOUS:
        return True
    if "?" in message or len(parts) > 6:
        return False
    return not any(p.lower().strip(",.?!") in SENTENCE_WORDS
                   for p in parts[1:])


def _intent_name(intent: Any) -> str:
    try:
        return str(getattr(intent, "name", "") or "").strip().lower()
    except Exception:
        return ""


def _confidence(intent: Any) -> float:
    try:
        return float(getattr(intent, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


# Phrases that mark a judgement call rather than a lookup.  A lookup gets the
# engine's own text; a judgement call earns the full fact sheet and the model.
_DEEP_RE = re.compile(
    r"should i|worth|what if|why not|instead of|rather than|trade|"
    r"next (?:few|two|three|couple)|rest of (?:the|my) draft|game ?plan|"
    r"plan for|strategy for|thoughts on|talk me|walk me|convince|"
    r"how (?:do|would|should) (?:i|you)|do you (?:like|prefer|trust)|"
    r"am i (?:crazy|wrong|overthinking)|help me (?:think|decide)", re.I)


def _looks_deep(message: str) -> bool:
    if _DEEP_RE.search(message):
        return True
    words = message.split()
    return len(words) >= 14 or message.count("?") >= 2


# Elliptical follow-ups: "and if he is gone", "what about the round after".
# The builtin parser resolves a bare pronoun to the top of the last list,
# which mid-conversation is usually the wrong referent - the model, holding
# the chat history, is the only component that knows who "he" is.
_FOLLOWUP_RE = re.compile(
    r"\b(he|him|his|that guy|this guy|the same|still|instead)\b"
    r"|^\s*(and|but|ok|so|what about|how about|what if)\b", re.I)


def _followup_shaped(message: str, intent: Any) -> bool:
    """A continuation that names no player and leans on prior context."""
    slots = getattr(intent, "slots", None) or {}
    if slots.get("players"):
        return False
    return bool(_FOLLOWUP_RE.search(message))


def _fact_pack(e: Engine) -> str:
    """Everything the model may reason from, produced by the engine just now.

    This is the grounding contract for llm.deep_answer: a claim that is not
    in this sheet is a claim the model was told not to make.  Caller holds
    the lock.
    """
    st = e.state
    parts = [explain.pick_header(e)]

    payload, recs = _recommend(e, 8, None, None)
    if recs:
        lines = ["Engine recommendations, best first (score = its judgement):"]
        for i, r in enumerate(recs, 1):
            top = "; ".join(t.text for t in r.reasons[:3])
            lines.append(
                f"{i}. {r.name} ({r.pos}) score {r.score:.2f}, "
                f"{r.proj.ppg:.1f} PPG, ADP {r.adp:.0f}, "
                f"{r.survival:.0%} lasts to next pick. {top}")
        parts.append("\n".join(lines))

    parts.append(explain.roster_summary(e))
    parts.append(explain.board_summary(e))
    parts.append(explain.opponent_summary(e))
    parts.append(explain.strategy_summary(e))

    recent = st.picks[-10:]
    if recent:
        parts.append("Last picks: " + "; ".join(
            f"{p.pick_no}.{p.pos} {p.name} (slot {p.slot})" for p in recent))
    upcoming = st.my_upcoming_picks(4)
    if upcoming:
        parts.append(f"Your upcoming picks: {upcoming}")

    return "\n\n".join(x for x in parts if x)[:9000]


def _llm_context(e: Engine) -> dict:
    """Board facts an LLM needs to place a half-typed name.  Built on demand.

    Only assembled when a backend actually exists, because with none - the
    normal case - this is pure waste on the chat path.
    """
    st = e.state
    return {
        "pick_no": st.next_pick_no,
        "round": st.current_round,
        "my_turn": st.is_my_turn,
        "strategy": e.strategy.name,
        "my_counts": e.my_counts(),
        "top_available": [p.name for p in e.available()[:20]],
        "just_recommended": [r.name for r in _cur().last_recs[:8]],
    }


def _refine_intent(intent: Any, refined: dict) -> Any | None:
    """Rebuild the parse around the model's label, keeping our own slots.

    The built-in extractor resolves names against the real player index, so
    its slots are better grounded than anything a model can invent; the model
    only gets to overwrite what it actually filled in.
    """
    slots = dict(getattr(intent, "slots", None) or {})
    slots.update(refined.get("slots") or {})
    fields = {"name": refined["intent"],
              "confidence": float(refined.get("confidence") or 0.5),
              "slots": slots}
    try:
        return dataclasses.replace(intent, **fields)
    except Exception:
        pass
    try:
        for key, val in fields.items():
            setattr(intent, key, val)
        return intent
    except Exception:
        return None


def _adopt_recs(e: Engine, data: Any) -> None:
    """Point follow-up questions at the list the user was just shown.

    The NLU builds its payload out of plain dicts, so the only place to get
    the Recommendation objects back is the cache - and only while the stamp
    still matches, otherwise those objects describe a board that has moved.
    Best effort: if nothing matches, the previous list stays.
    """
    if not isinstance(data, dict) or _cur().rec_stamp != _stamp(e):
        return
    wanted = [r.get("key") for r in data.get("recommendations") or []
              if isinstance(r, dict) and r.get("key")]
    if not wanted:
        return
    pool = {r.key: r for _, recs in _cur().rec_cache.values() for r in recs}
    found = [pool[k] for k in wanted if k in pool]
    if found:
        _cur().last_recs = found


def _suggestions(raw: Any, intent_name: str) -> list[str]:
    """Two to four follow-up chips, whatever the NLU did or did not offer."""
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
    for text in DEFAULT_SUGGESTIONS.get(intent_name, FALLBACK_SUGGESTIONS):
        if len(out) >= 2:
            break
        if text not in out:
            out.append(text)
    return out[:4]


def _phraseable(text: str) -> bool:
    """Only prose gets reworded - a scored table needs its columns."""
    return (bool(text) and len(text) <= PHRASE_MAX_CHARS
            and text.count("\n") < PHRASE_MAX_LINES)


def _chat_result(output: str, intent: str, confidence: float, understood: bool,
                 engine_name: str, data: Any, suggestions: Any) -> dict:
    return {
        "output": output,
        "intent": intent,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "understood": understood,
        "engine": engine_name,
        "data": data if isinstance(data, dict) else None,
        "suggestions": _suggestions(suggestions, intent),
    }


def _chat_command(message: str) -> dict:
    """The old CLI router, unchanged, wearing the new response shape."""
    with engine_lock():
        e = _need()
        out, recs = chat(e, message, _cur().last_recs)
        _cur().last_recs = recs
        # `set`, `preset`, `bump` and `ban` all retune the engine mid-chat, so
        # nothing cached for this pick is what it would now say.
        _cur().rec_cache.clear()
    if out == "__QUIT__":
        out = "Nothing to quit here - close the panel tab when you're done."
    understood = not out.startswith("unknown command")
    return _chat_result(out, "command" if understood else "unknown",
                        1.0 if understood else 0.0, understood, "builtin",
                        None, None)


def _chat_natural(message: str) -> dict:
    """Parse, optionally get a second opinion, answer, optionally reword.

    The two model calls happen outside the engine lock on purpose.  They are
    the only part of a request that can take seconds, and holding the lock
    across them would stall the panel's poll - and every other tab - behind
    somebody's chat message.
    """
    # In public mode the operator's model key never serves strangers unless
    # they have explicitly opted in with FFBOT_ALLOW_PUBLIC_LLM=1.
    allow_llm = not _PUBLIC or (os.environ.get("FFBOT_ALLOW_PUBLIC_LLM")
                                or "").strip().lower() in ("1", "true", "yes")
    backend = llm.detect() if allow_llm else None
    used_llm = False

    with engine_lock():
        e = _need()
        intent = nlu.parse(message, e)
        want_deep = backend is not None and (
            _intent_name(intent) in UNKNOWN_INTENTS
            or _confidence(intent) < LOW_CONFIDENCE
            or _looks_deep(message)
            or (bool(_cur().chat_history) and _followup_shaped(message, intent)))
        facts = _fact_pack(e) if want_deep else ""
        ctx = (_llm_context(e) if backend is not None and not want_deep
               and _confidence(intent) < LOW_CONFIDENCE else None)

    if want_deep and facts:
        # Model call happens outside the lock: it is the only slow part of
        # the request and the poll must not queue behind it.
        answer = llm.deep_answer(message, facts, list(_cur().chat_history))
        if answer:
            verdict, _cur().deep_detail = llm.split_deep(answer)
            _cur().chat_history.append({"role": "user", "content": message})
            _cur().chat_history.append({"role": "assistant", "content": answer})
            chips = (["more detail"] if _cur().deep_detail else []) +                 ["will he last?", "my roster", "read the room"]
            return _chat_result(verdict, "deep", 0.9, True, backend.label,
                                None, chips[:4])
        # The model failed or declined; fall through to the built-in path.

    if ctx is not None:
        refined = llm.assist_parse(message, ctx)
        if refined:
            better = _refine_intent(intent, refined)
            if better is not None:
                intent, used_llm = better, True

    with engine_lock():
        e = _need()
        text, data, suggestions = nlu.respond(intent, e, _cur().last_recs)
        name = _intent_name(intent)
        _adopt_recs(e, data)
        if name not in READ_ONLY_INTENTS:
            _cur().rec_cache.clear()

    output = str(text or "")
    understood = name not in UNKNOWN_INTENTS
    if backend is not None and understood and _phraseable(output):
        pretty = llm.phrase(output, message)
        if pretty:
            output, used_llm = pretty, True

    engine_name = backend.label if (backend and used_llm) else "builtin"
    if understood:
        _cur().chat_history.append({"role": "user", "content": message})
        _cur().chat_history.append({"role": "assistant", "content": output[:1500]})
    return _chat_result(output, name or "unknown", _confidence(intent),
                        understood, engine_name, data, suggestions)


_MORE_RE = re.compile(
    r"^\s*(more( detail| info)?|tell me more|expand|go deeper|why\??|"
    r"full analysis|details?)\s*[?.!]*\s*$", re.I)


def _api_chat(req: Req) -> dict:
    """The panel's chat box: natural language first, CLI commands always."""
    message = str(req.body.get("message") or "").strip()
    if not message:
        raise ApiError(400, "message is required")
    if _MORE_RE.match(message) and _cur().deep_detail:
        # The other half of the last deep answer, already paid for.
        detail = _cur().deep_detail
        _cur().chat_history.append({"role": "user", "content": message})
        _cur().chat_history.append({"role": "assistant", "content": detail})
        return _chat_result(detail, "deep_detail", 1.0, True,
                            llm.backend_label(), None,
                            ["who should I take?", "read the room"])
    if nlu is None:
        return _chat_command(message)
    # An unambiguous CLI head ("rec 5", "set x y") goes straight to the router.
    # An ambiguous one ("why olave", "board") tries natural language first and
    # only falls back when the parse is weak, so the common phrasings keep
    # their structured data and follow-up chips instead of being swallowed.
    head = message.split()[0].lower().strip(",:") if message.split() else ""
    if _is_command(message) and head not in CLI_AMBIGUOUS:
        return _chat_command(message)
    try:
        res = _chat_natural(message)
        if (not res.get("understood")
                or float(res.get("confidence") or 0.0) < AMBIGUOUS_FLOOR) \
                and _is_command(message):
            return _chat_command(message)
        return res
    except (ApiError, sleeper.SleeperError):
        # A missing draft or a dead Sleeper is real news; report it as such
        # rather than hiding it behind a command that would fail anyway.
        raise
    except Exception:
        # Mid-draft, a chat box that answers nothing is worse than one that
        # answers bluntly, so a broken parse falls back to the old router.
        traceback.print_exc()
        return _chat_command(message)


def _api_review(req: Req) -> dict:
    wanted = (req.query.get("draft_id") or "").strip()
    with engine_lock():
        store = _cur().engine.store if _cur().engine else _shared_store()
        drafts = store.list_drafts()
        if not drafts and not wanted:
            raise ApiError(404, "no drafts recorded yet")
        draft_id = wanted or drafts[0].draft_id
        rep = store.agreement_report(draft_id)
        rep["draft_id"] = draft_id
        rep["notes"] = [f for f in store.list_feedback(40)
                        if f["draft_id"] == draft_id]
        rep["recent_drafts"] = [
            {"draft_id": d.draft_id, "name": d.name, "teams": d.teams,
             "scoring": d.scoring, "picks": d.picks, "status": d.status}
            for d in drafts[:10]]
        return rep


def _api_guide(req: Req) -> dict:
    """Guide lookup.  Needs no draft - the guide is a static document."""
    query = (req.query.get("query") or "").strip()
    if not query:
        raise ApiError(400, "query is required")
    g = get_guide()
    out: dict[str, Any] = {"query": query}
    gp = g.resolve(query)
    if gp:
        out["player"] = {
            "name": gp.name, "pos": gp.pos,
            "ppr_overall": gp.rank("ppr"), "ppr_pos": gp.prank("ppr"),
            "half_ppr_overall": gp.rank("half_ppr"),
            "half_ppr_pos": gp.prank("half_ppr"),
            "adj_ppg_2025": gp.adj_ppg_2025,
            "adj_ppg_rank_2025": gp.adj_ppg_rank_2025,
            "dynasty_rookie_rank": gp.rookie_rank,
            "notes": gp.notes,
        }
    hits = g.stat_search(query, limit=8)
    if hits:
        out["stats"] = hits
    if not gp and not hits:
        raise ApiError(404, f"nothing in the guide matches {query!r}")
    return out


# ------------------------------------------------------------------- yahoo


def _request_base(req: Req, handler_headers) -> str:
    """scheme://host for redirect URIs, honouring the TLS proxy's header."""
    host = handler_headers.get("Host") or "127.0.0.1"
    proto = handler_headers.get("X-Forwarded-Proto") or \
        ("https" if _PUBLIC else "http")
    return f"{proto}://{host}"


def _yahoo_session(req: Req) -> DraftSession:
    """The session named in the OAuth state/query, outside /api dispatch."""
    sid = (req.query.get("session") or req.query.get("state") or "").strip()
    if not _PUBLIC:
        return _get_or_create(LOCAL_SID)
    with _REG_LOCK:
        ses = _REGISTRY.get(sid)
    if ses is None or sid == LOCAL_SID:
        raise ApiError(401, "unknown session - connect first, then link Yahoo")
    ses.last_seen = time.time()
    return ses


def _auth_yahoo_start(req: Req) -> Response:
    if not yahoo.configured():
        raise ApiError(404, "Yahoo support is not enabled on this server")
    ses = _yahoo_session(req)
    base = _request_base(req, req.headers or {})
    url = yahoo.authorize_url(f"{base}/auth/yahoo/callback", state=ses.sid)
    return Response(302, b"", "text/plain", [("Location", url)])


def _auth_yahoo_callback(req: Req) -> Response:
    if not yahoo.configured():
        raise ApiError(404, "Yahoo support is not enabled on this server")
    ses = _yahoo_session(req)
    code = (req.query.get("code") or "").strip()
    if not code:
        raise ApiError(400, "Yahoo did not return a code")
    base = _request_base(req, req.headers or {})
    tokens = yahoo.exchange_code(code, f"{base}/auth/yahoo/callback")
    with ses.lock:
        ses.yahoo = tokens
    return Response(302, b"", "text/plain",
                    [("Location", "/app" if _PUBLIC else "/")])


def _api_yahoo_leagues(req: Req) -> dict:
    if not yahoo.configured():
        raise ApiError(404, "Yahoo support is not enabled on this server")
    ses = _cur()
    if not ses.yahoo:
        raise ApiError(409, "link Yahoo first (menu -> connect Yahoo)")
    return {"leagues": yahoo.user_leagues(ses.yahoo)}


# ------------------------------------------------------------------ static


def _inject_token(html: str, token: str) -> str:
    """Hand the page its token server-side.

    The alternatives - a query string, or making the user paste it - both put
    the token somewhere that outlives the page: shell history, browser
    history, a Referer header.
    """
    tag = f"<script>window.FFBOT_TOKEN={json.dumps(token)};</script>"
    if "</head>" in html:
        return html.replace("</head>", f"  {tag}\n</head>", 1)
    return f"{tag}\n{html}"


SITE = Path(__file__).resolve().parents[1] / "site"
SITE_PAGES = {"/privacy": "privacy.html", "/terms": "terms.html"}


def _panel_page(req: Req) -> Response:
    name, ctype = STATIC["/"]
    path = WEB / name
    html = path.read_text(encoding="utf-8") if path.is_file() else PLACEHOLDER
    # Public pages authenticate with the session issued by /api/connect, so
    # they get no process token; leaking it would hand every visitor the
    # local-mode credential.
    token = "" if _PUBLIC else (req.panel.token if req.panel else "")
    return Response(200, _inject_token(html, token).encode("utf-8"), ctype)


def _index(req: Req) -> Response:
    landing = SITE / "index.html"
    if _PUBLIC and landing.is_file():
        return Response(200, landing.read_bytes(),
                        "text/html; charset=utf-8")
    return _panel_page(req)


def _site_page(req: Req) -> Response:
    name = SITE_PAGES.get(req.path)
    path = SITE / name if name else None
    if not path or not path.is_file():
        raise ApiError(404, "page not published")
    return Response(200, path.read_bytes(), "text/html; charset=utf-8")


def _asset(req: Req) -> Response:
    name, ctype = STATIC[req.path]
    path = WEB / name
    if not path.is_file():
        raise ApiError(404, f"{name} has not been built yet")
    return Response(200, path.read_bytes(), ctype)


ROUTES: dict[tuple[str, str], Callable[[Req], Any]] = {
    ("GET", "/"): _index,
    ("GET", "/app"): _panel_page,
    ("GET", "/privacy"): _site_page,
    ("GET", "/terms"): _site_page,
    ("GET", "/app.js"): _asset,
    ("GET", "/styles.css"): _asset,
    ("GET", "/api/ping"): _api_ping,
    ("GET", "/api/state"): _api_state,
    ("POST", "/api/connect"): _api_connect,
    ("POST", "/api/mock"): _api_mock,
    ("GET", "/api/refresh"): _api_refresh,
    ("GET", "/api/recommend"): _api_recommend,
    ("GET", "/api/player"): _api_player,
    ("GET", "/api/compare"): _api_compare,
    ("GET", "/api/board"): _api_board,
    ("GET", "/api/boardgrid"): _api_boardgrid,
    ("GET", "/api/bypos"): _api_bypos,
    ("GET", "/api/room"): _api_room,
    ("GET", "/api/roster"): _api_roster,
    ("GET", "/api/strategy"): _api_strategy,
    ("POST", "/api/strategy"): _api_set_strategy,
    ("POST", "/api/bump"): _api_bump,
    ("POST", "/api/ban"): _api_ban,
    ("POST", "/api/note"): _api_note,
    ("POST", "/api/chat"): _api_chat,
    ("GET", "/api/review"): _api_review,
    ("GET", "/api/guide"): _api_guide,
    ("GET", "/auth/yahoo/start"): _auth_yahoo_start,
    ("GET", "/auth/yahoo/callback"): _auth_yahoo_callback,
    ("GET", "/api/yahoo/leagues"): _api_yahoo_leagues,
}


# ------------------------------------------------------------------- server


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "ffbot-panel/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"   # keep-alive: the UI polls every few seconds

    def do_OPTIONS(self) -> None:
        """Answer the Private Network Access preflight for the sidebar.

        Anything that is not a recognised Sleeper origin gets a bare 403, so
        this cannot be used to probe the panel from an arbitrary page.
        """
        headers = sidebar_cors(self.headers.get("Origin"))
        if not headers:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        for key, val in headers:
            self.send_header(key, val)
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    # ------------------------------------------------------------- gate

    def _panel(self) -> "PanelServer":
        return self.server.panel        # type: ignore[attr-defined]

    def _check_origin(self) -> None:
        """Refuse anything a page on another origin sent us.

        Browsers attach Origin to cross-site requests but not to plain
        navigation, so this blocks drive-by CSRF without breaking the panel.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return
        if _PUBLIC:
            # Domain-agnostic same-origin check: whatever host this request
            # reached is the only origin allowed to script it.
            if urlparse(origin).netloc != (self.headers.get("Host") or ""):
                raise ApiError(403, f"origin {origin} is not allowed")
            return
        if origin not in self._panel().origins:
            raise ApiError(403, f"origin {origin} is not allowed")

    def _check_token(self, path: str) -> None:
        if _PUBLIC:
            # Anonymous multi-user mode: the unguessable session id issued by
            # /api/connect is the credential, checked in _resolve_session.
            return
        if not path.startswith("/api/") or path == "/api/ping":
            return
        got = (self.headers.get("X-FFBot-Token") or "").encode("utf-8", "ignore")
        if not hmac.compare_digest(got, self._panel().token.encode("utf-8")):
            raise ApiError(401, "bad or missing X-FFBot-Token header")

    # Endpoints that would expose the operator's own draft history to
    # strangers stay private-mode only.
    PUBLIC_BLOCKED = frozenset({"/api/review"})

    def _resolve_session(self, method: str, path: str,
                         query: dict) -> DraftSession:
        if not _PUBLIC or not path.startswith("/api/"):
            return _get_or_create(LOCAL_SID)
        if path in self.PUBLIC_BLOCKED:
            raise ApiError(404, "not available on the hosted service")
        ip = str(self.client_address[0])
        if method == "POST" and path in ("/api/connect", "/api/mock"):
            return _new_session(ip)
        if path in ("/api/ping", "/api/guide"):
            return _get_or_create(LOCAL_SID)      # stateless, no draft needed
        sid = (self.headers.get("X-FFBot-Session")
               or query.get("session") or "").strip()
        with _REG_LOCK:
            ses = _REGISTRY.get(sid)
        if ses is None or sid == LOCAL_SID:
            raise ApiError(401, "missing or expired draft session - reconnect")
        ses.last_seen = time.time()
        return ses

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad Content-Length") from None
        if length > MAX_BODY:
            # Do not drain it; the connection is no longer trustworthy.
            self.close_connection = True
            raise ApiError(413, f"request body over {MAX_BODY} bytes")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ApiError(400, f"body is not valid JSON: {e}") from None
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    # ---------------------------------------------------------- dispatch

    def _handle(self, method: str) -> None:
        started = time.time()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        status = 500
        try:
            body = self._read_body() if method == "POST" else {}
            self._check_origin()
            self._check_token(path)
            handler = ROUTES.get((method, path))
            if handler is None:
                raise ApiError(404, f"no route for {method} {path}")
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            ses = self._resolve_session(method, path, query)
            ctx_token = _CTX.set(ses)
            try:
                result = handler(Req(method, path, query, body,
                                     self._panel(), self.headers))
                if (_PUBLIC and isinstance(result, dict)
                        and path in ("/api/connect", "/api/mock")):
                    result["session"] = ses.sid
            finally:
                _CTX.reset(ctx_token)
            resp = result if isinstance(result, Response) else _json(200, result)
            status = resp.status
            self._send(resp)
        except ApiError as e:
            status = e.status
            self._send(_json(e.status, {"error": e.message}))
        except sleeper.SleeperError as e:
            status = 502
            self._send(_json(502, {"error": f"Sleeper: {e}"}))
        except (ValueError, KeyError) as e:
            status = 400
            self._send(_json(400, {"error": str(e)}))
        except Exception as e:          # a bad request must not kill the panel
            status = 500
            traceback.print_exc()
            self._send(_json(500, {"error": f"{type(e).__name__}: {e}"}))
        finally:
            ms = (time.time() - started) * 1000
            print(f"[ffbot] {method} {self.path} -> {status} ({ms:.0f}ms)",
                  file=sys.stderr)

    def _send(self, resp: Response) -> None:
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", FRAME_POLICY)
            for key, val in resp.headers:
                self.send_header(key, val)
            for key, val in sidebar_cors(self.headers.get("Origin")):
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(resp.body)
        except (BrokenPipeError, ConnectionResetError):
            # The panel navigated away mid-poll; nothing to report.
            self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:
        """Silence the default per-request noise; _handle logs one line."""


class PanelServer:
    """ThreadingHTTPServer plus this panel's identity.

    The token and allowed origins live on the instance rather than in module
    globals so a second server (a test, a second draft) gets its own.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
        if not _PUBLIC and host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                "the panel binds loopback only unless --public is set - "
                f"refusing to listen on {host!r}")
        self.token = secrets.token_urlsafe(24)
        self.httpd = ThreadingHTTPServer((host, port), PanelHandler)
        self.httpd.daemon_threads = True
        self.httpd.panel = self         # type: ignore[attr-defined]
        self.host = host
        self.port = self.httpd.server_address[1]
        self.origins = {f"http://{h}:{self.port}"
                        for h in ("127.0.0.1", "localhost")}
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        name="ffbot-panel", daemon=True)
        self._thread.start()

    def wait(self) -> None:
        """Block until Ctrl-C, then shut down cleanly."""
        try:
            while self._thread and self._thread.is_alive():
                self._thread.join(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread = None


_SWEEPER_STARTED = False


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          open_browser: bool = False, tries: int = 20,
          public: bool | None = None) -> PanelServer:
    """Bind, start serving in a background thread, hand back the server.

    The requested port is usually taken by a previous panel or someone's dev
    server, and dying on EADDRINUSE would be a silly way to lose a draft, so
    walk up to `tries` ports before giving up.
    """
    global _PUBLIC, _SWEEPER_STARTED
    if public is not None:
        _PUBLIC = bool(public)
    if _PUBLIC and not _SWEEPER_STARTED:
        _SWEEPER_STARTED = True
        threading.Thread(target=_sweeper_loop, name="ffbot-sweeper",
                         daemon=True).start()
    last: OSError | None = None
    for candidate in range(port, port + tries + 1):
        try:
            srv = PanelServer(host, candidate)
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            last = e
            continue
        srv.start()
        if open_browser:
            webbrowser.open(srv.url)
        return srv
    raise OSError(f"no free port in {port}-{port + tries}") from last


def main(argv: list[str] | None = None) -> int:
    """`python -m ffbot.server` - the panel with no draft attached."""
    port = int(argv[0]) if argv else DEFAULT_PORT
    srv = serve(port=port)
    print(f"ff-draft-bot panel on {srv.url} (Ctrl-C to stop)", file=sys.stderr)
    srv.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
