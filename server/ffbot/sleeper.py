"""Sleeper API client + draft state tracking.

Read-only.  The bot never submits a pick; it only observes drafts and advises.

Endpoints used (all public, no auth):
  GET /v1/user/{username}                      -> user_id
  GET /v1/user/{user_id}/drafts/nfl/{season}   -> that user's drafts
  GET /v1/league/{league_id}/drafts            -> league drafts
  GET /v1/league/{league_id}                   -> scoring + roster settings
  GET /v1/draft/{draft_id}                     -> draft metadata/settings
  GET /v1/draft/{draft_id}/picks               -> picks so far
  GET /v1/players/nfl                          -> full player universe (~5MB)
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://api.sleeper.app/v1"
CACHE = Path(__file__).resolve().parents[1] / "data" / "cache"
PLAYERS_TTL = 24 * 3600

FANTASY_POS = ("QB", "RB", "WR", "TE", "K", "DEF")


class SleeperError(RuntimeError):
    pass


def _get(path: str, timeout: float = 20.0) -> Any:
    url = f"{BASE}{path}"
    req = Request(url, headers={"User-Agent": "ffbot/1.0 (local draft assistant)"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        if e.code == 404:
            return None
        raise SleeperError(f"{url} -> HTTP {e.code}") from e
    except URLError as e:
        raise SleeperError(f"{url} -> {e.reason}") from e


def extract_draft_id(text: str) -> str:
    """Accept a raw id, a sleeper.com draft URL, or an app deep link."""
    text = text.strip()
    m = re.search(r"(?:draft/(?:nfl/)?)(\d{6,})", text)
    if m:
        return m.group(1)
    m = re.fullmatch(r"\d{6,}", text)
    if m:
        return text
    raise ValueError(f"could not find a Sleeper draft id in {text!r}")


# ---------------------------------------------------------------- players


def load_players(refresh: bool = False) -> dict[str, dict]:
    """Full NFL player universe, cached to disk for 24h (the payload is ~5MB)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / "players_nfl.json"
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < PLAYERS_TTL:
        return json.loads(path.read_text(encoding="utf-8"))
    data = _get("/players/nfl", timeout=90.0)
    if not data:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        raise SleeperError("could not fetch /players/nfl and no cache is present")
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def user_id(username: str) -> str | None:
    u = _get(f"/user/{username}")
    return u.get("user_id") if u else None


def user_drafts(uid: str, season: str = "2026") -> list[dict]:
    return _get(f"/user/{uid}/drafts/nfl/{season}") or []


def league(league_id: str) -> dict | None:
    return _get(f"/league/{league_id}")


def league_drafts(league_id: str) -> list[dict]:
    return _get(f"/league/{league_id}/drafts") or []


def draft(draft_id: str) -> dict | None:
    return _get(f"/draft/{draft_id}")


def draft_picks(draft_id: str) -> list[dict]:
    return _get(f"/draft/{draft_id}/picks") or []


# ---------------------------------------------------------------- state


@dataclass
class Pick:
    pick_no: int
    round: int
    slot: int
    roster_id: int | None
    player_id: str
    name: str
    pos: str
    team: str | None
    picked_by: str | None


@dataclass
class DraftState:
    """Everything the engine needs to know about a live draft."""

    draft_id: str
    teams: int
    rounds: int
    draft_type: str            # snake | linear | auction
    scoring: str               # ppr | half_ppr | std
    status: str
    reversal_round: int = 0
    roster_slots: dict[str, int] = field(default_factory=dict)
    my_slot: int | None = None
    my_user_id: str | None = None
    slot_names: dict[int, str] = field(default_factory=dict)
    picks: list[Pick] = field(default_factory=list)
    league_id: str | None = None
    name: str = ""
    is_dynasty: bool = False
    raw: dict = field(default_factory=dict)
    # Non-Sleeper platforms plug in here: an object with refresh(state) that
    # updates state.picks/state.status from its own API.  None = Sleeper.
    provider: Any = None

    # -------------------------------------------------------- pick ordering

    def slot_for_pick(self, pick_no: int) -> int:
        """Which draft slot owns overall pick number `pick_no` (1-indexed)."""
        rnd = (pick_no - 1) // self.teams + 1
        idx = (pick_no - 1) % self.teams
        if self.draft_type == "linear":
            return idx + 1
        forward = rnd % 2 == 1
        if self.reversal_round and rnd >= self.reversal_round:
            forward = not forward
        return idx + 1 if forward else self.teams - idx

    def picks_for_slot(self, slot: int) -> list[int]:
        return [p for p in range(1, self.teams * self.rounds + 1)
                if self.slot_for_pick(p) == slot]

    @property
    def total_picks(self) -> int:
        return self.teams * self.rounds

    @property
    def next_pick_no(self) -> int:
        return len(self.picks) + 1

    @property
    def current_round(self) -> int:
        return min((self.next_pick_no - 1) // self.teams + 1, self.rounds)

    @property
    def on_the_clock_slot(self) -> int | None:
        if self.next_pick_no > self.total_picks:
            return None
        return self.slot_for_pick(self.next_pick_no)

    def my_upcoming_picks(self, limit: int = 4) -> list[int]:
        if self.my_slot is None:
            return []
        return [p for p in self.picks_for_slot(self.my_slot)
                if p >= self.next_pick_no][:limit]

    @property
    def is_my_turn(self) -> bool:
        return self.my_slot is not None and self.on_the_clock_slot == self.my_slot

    def picks_until_my_turn(self) -> int | None:
        up = self.my_upcoming_picks(1)
        return up[0] - self.next_pick_no if up else None

    # -------------------------------------------------------- roster views

    def drafted_ids(self) -> set[str]:
        return {p.player_id for p in self.picks}

    def roster_of_slot(self, slot: int) -> list[Pick]:
        return [p for p in self.picks if p.slot == slot]

    def my_roster(self) -> list[Pick]:
        return self.roster_of_slot(self.my_slot) if self.my_slot else []

    def pos_counts(self, slot: int) -> dict[str, int]:
        out = dict.fromkeys(FANTASY_POS, 0)
        for p in self.roster_of_slot(slot):
            if p.pos in out:
                out[p.pos] += 1
        return out

    # -------------------------------------------------------- construction

    @property
    def starters(self) -> dict[str, int]:
        """Starting-lineup slot counts (bench excluded)."""
        return {k: v for k, v in self.roster_slots.items()
                if k not in ("BN", "IR", "TAXI") and v > 0}

    @property
    def superflex(self) -> bool:
        return self.roster_slots.get("SUPER_FLEX", 0) > 0

    @property
    def fmt(self) -> str:
        """Which guide board to use: the guide ships PPR and half-PPR."""
        return "ppr" if self.scoring == "ppr" else "half_ppr"


SLOT_MAP = {
    "slots_qb": "QB", "slots_rb": "RB", "slots_wr": "WR", "slots_te": "TE",
    "slots_flex": "FLEX", "slots_super_flex": "SUPER_FLEX", "slots_k": "K",
    "slots_def": "DEF", "slots_bn": "BN", "slots_wr_rb": "FLEX",
    "slots_wr_te": "FLEX", "slots_rb_wr_te": "FLEX", "slots_idp_flex": "IDP",
    "slots_dl": "IDP", "slots_lb": "IDP", "slots_db": "IDP",
}


def build_state(draft_id: str, my_user_id: str | None = None,
                my_slot: int | None = None,
                players: dict[str, dict] | None = None) -> DraftState:
    d = draft(draft_id)
    if not d:
        raise SleeperError(f"draft {draft_id} not found (is it a valid Sleeper draft id?)")

    s = d.get("settings") or {}
    meta = d.get("metadata") or {}
    teams = int(s.get("teams") or 12)
    rounds = int(s.get("rounds") or 15)

    roster_slots: dict[str, int] = {}
    for k, v in s.items():
        if k.startswith("slots_") and v:
            roster_slots[SLOT_MAP.get(k, k[6:].upper())] = \
                roster_slots.get(SLOT_MAP.get(k, k[6:].upper()), 0) + int(v)

    order = d.get("draft_order") or {}
    if my_slot is None and my_user_id and my_user_id in order:
        my_slot = int(order[my_user_id])

    slot_names: dict[int, str] = {}
    for uid, slot in order.items():
        slot_names[int(slot)] = uid

    league_id = d.get("league_id")
    is_dynasty = False
    lg = league(league_id) if league_id else None
    if lg:
        st = (lg.get("settings") or {})
        is_dynasty = str(st.get("type", "")) == "2" or \
            "dynasty" in (lg.get("name") or "").lower()
        if not roster_slots:
            for rp in lg.get("roster_positions") or []:
                roster_slots[rp] = roster_slots.get(rp, 0) + 1

    state = DraftState(
        draft_id=draft_id,
        teams=teams,
        rounds=rounds,
        draft_type=d.get("type") or "snake",
        scoring=(meta.get("scoring_type") or "ppr").lower(),
        status=d.get("status") or "unknown",
        reversal_round=int(s.get("reversal_round") or 0),
        roster_slots=roster_slots,
        my_slot=my_slot,
        my_user_id=my_user_id,
        slot_names=slot_names,
        league_id=league_id,
        name=meta.get("name") or (lg or {}).get("name") or f"Draft {draft_id}",
        is_dynasty=is_dynasty,
        raw=d,
    )
    refresh_picks(state, players)
    return state


def refresh_picks(state: DraftState, players: dict[str, dict] | None = None) -> int:
    """Pull picks; returns how many new picks appeared since the last call."""
    raw = draft_picks(state.draft_id)
    before = len(state.picks)
    out: list[Pick] = []
    for p in sorted(raw, key=lambda x: x.get("pick_no") or 0):
        pid = str(p.get("player_id") or "")
        md = p.get("metadata") or {}
        name = " ".join(x for x in (md.get("first_name"), md.get("last_name")) if x)
        pos = md.get("position") or ""
        team = md.get("team")
        if (not name or not pos) and players and pid in players:
            pl = players[pid]
            name = name or pl.get("full_name") or \
                f"{pl.get('first_name','')} {pl.get('last_name','')}".strip()
            pos = pos or (pl.get("position") or "")
            team = team or pl.get("team")
        pick_no = int(p.get("pick_no") or 0)
        out.append(Pick(
            pick_no=pick_no,
            round=int(p.get("round") or ((pick_no - 1) // state.teams + 1)),
            slot=int(p.get("draft_slot") or state.slot_for_pick(pick_no)),
            roster_id=p.get("roster_id"),
            player_id=pid,
            name=name,
            pos=pos.upper(),
            team=team,
            picked_by=p.get("picked_by") or None,
        ))
    state.picks = out
    d = draft(state.draft_id)
    if d:
        state.status = d.get("status") or state.status
    return len(out) - before
