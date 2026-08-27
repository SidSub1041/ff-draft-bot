"""ESPN Fantasy support.  EXPERIMENTAL - read the module docstring, honestly.

ESPN has no official public fantasy API.  This adapter speaks the unofficial
v3 JSON endpoints the ESPN web app itself uses.  Two consequences the user
must understand:

* Private leagues need the user's own espn_s2 and SWID browser cookies,
  pasted in at connect time.  They are held in the draft session only, used
  solely for reads against ESPN, and expire with it.
* The endpoints are undocumented and ESPN may change or rate-limit them at
  any time.  This adapter has NOT been exercised against a live ESPN draft
  room; the draft detail view is known to fill for completed drafts, and its
  cadence during a live one is unverified.  Treat it as experimental and
  rehearse with an ESPN mock before trusting it in a league that matters.

Read-only, as everywhere in this program: it observes, it never picks.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from .sleeper import DraftState, Pick

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
TIMEOUT = 15.0

POS_BY_ID = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

PRO_TEAMS = {
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR",
    15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ",
    21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA",
    27: "TB", 28: "WAS", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}

WARNING = ("ESPN support is EXPERIMENTAL: it uses ESPN's unofficial API and "
           "has not been proven against a live ESPN draft room. Rehearse "
           "with an ESPN mock before a league that matters.")


class EspnError(RuntimeError):
    pass


def _get(url: str, cookies: dict[str, str] | None = None,
         headers: dict[str, str] | None = None) -> Any:
    head = {"User-Agent": "ffbot/1.0 (draft assistant; read-only)",
            "Accept": "application/json"}
    if cookies:
        head["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if headers:
        head.update(headers)
    req = urllib.request.Request(url, headers=head)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        raise EspnError(f"ESPN API: {e}") from e


class EspnProvider:
    """Feeds a DraftState from ESPN's mDraftDetail view."""

    def __init__(self, league_id: str, season: int,
                 espn_s2: str = "", swid: str = "",
                 my_team_id: int | None = None) -> None:
        self.league_id = str(league_id).strip()
        self.season = int(season)
        self.cookies = {}
        if espn_s2 and swid:
            self.cookies = {"espn_s2": espn_s2.strip(), "SWID": swid.strip()}
        self.my_team_id = my_team_id
        self._players: dict[int, dict] = {}
        self._slot_of: dict[int, int] = {}

    # ------------------------------------------------------------- reads

    def _league(self, views: str) -> dict:
        url = (f"{BASE}/seasons/{self.season}/segments/0/leagues/"
               f"{self.league_id}?{views}")
        data = _get(url, self.cookies)
        if isinstance(data, list):          # some responses arrive wrapped
            data = data[0] if data else {}
        if not isinstance(data, dict):
            raise EspnError("unexpected league payload shape")
        return data

    def _load_players(self) -> None:
        """Season player list, fetched once - id -> {name,pos,team}."""
        if self._players:
            return
        url = f"{BASE}/seasons/{self.season}/players?scoringPeriodId=0&view=players_wl"
        flt = json.dumps({"filterActive": {"value": True}})
        data = _get(url, self.cookies, {"X-Fantasy-Filter": flt})
        if not isinstance(data, list):
            raise EspnError("unexpected players payload shape")
        for p in data:
            pid = p.get("id")
            name = p.get("fullName") or ""
            if pid is None or not name:
                continue
            self._players[int(pid)] = {
                "name": name,
                "pos": POS_BY_ID.get(p.get("defaultPositionId"), ""),
                "team": PRO_TEAMS.get(p.get("proTeamId")),
            }

    # ---------------------------------------------------------- provider

    def build_state(self) -> DraftState:
        data = self._league("view=mSettings&view=mDraftDetail")
        settings = data.get("settings") or {}
        size = int(settings.get("size") or 10)
        roster = ((settings.get("rosterSettings") or {})
                  .get("lineupSlotCounts") or {})
        # ESPN lineup slot ids: 0 QB, 2 RB, 4 WR, 6 TE, 16 DST, 17 K,
        # 23 FLEX, 20 bench.
        slot_map = {"0": "QB", "2": "RB", "4": "WR", "6": "TE",
                    "16": "DEF", "17": "K", "23": "FLEX", "20": "BN"}
        roster_slots: dict[str, int] = {}
        for sid, count in roster.items():
            name = slot_map.get(str(sid))
            if name and int(count):
                roster_slots[name] = roster_slots.get(name, 0) + int(count)
        rounds = max(1, sum(v for k, v in roster_slots.items()
                            if k != "IR")) or 16
        scoring_id = ((settings.get("scoringSettings") or {})
                      .get("scoringType") or "")
        scoring = "ppr" if "PPR" in str(scoring_id).upper() else "half_ppr"
        state = DraftState(
            draft_id=f"espn:{self.league_id}:{self.season}",
            teams=size,
            rounds=rounds,
            draft_type="snake",
            scoring=scoring,
            status="drafting",
            roster_slots=roster_slots,
            name=str(settings.get("name") or f"ESPN {self.league_id}"),
            provider=self,
        )
        self._apply_picks(state, data)
        return state

    def refresh(self, state: DraftState) -> None:
        data = self._league("view=mDraftDetail")
        self._apply_picks(state, data)

    def _apply_picks(self, state: DraftState, data: dict) -> None:
        detail = data.get("draftDetail") or {}
        rows = detail.get("picks") or []
        if not rows:
            return
        self._load_players()
        rows = sorted(rows, key=lambda r: int(r.get("overallPickNumber")
                                              or r.get("id") or 0))
        for row in rows:
            if int(row.get("roundId") or 0) == 1:
                tid = int(row.get("teamId") or 0)
                if tid and tid not in self._slot_of:
                    self._slot_of[tid] = \
                        ((int(row.get("overallPickNumber") or 1) - 1)
                         % state.teams) + 1
        picks: list[Pick] = []
        for row in rows:
            pid = int(row.get("playerId") or 0)
            meta = self._players.get(pid) or {}
            no = int(row.get("overallPickNumber") or len(picks) + 1)
            picks.append(Pick(
                pick_no=no,
                round=int(row.get("roundId")
                          or ((no - 1) // state.teams + 1)),
                slot=self._slot_of.get(int(row.get("teamId") or 0),
                                       state.slot_for_pick(no)),
                roster_id=None,
                player_id=str(pid),
                name=meta.get("name") or f"ESPN player {pid}",
                pos=meta.get("pos") or "",
                team=meta.get("team"),
                picked_by=str(row.get("teamId") or ""),
            ))
        state.picks = picks
        if self.my_team_id and self.my_team_id in self._slot_of:
            state.my_slot = self._slot_of[self.my_team_id]
        if detail.get("drafted") or len(picks) >= state.total_picks:
            state.status = "complete"
