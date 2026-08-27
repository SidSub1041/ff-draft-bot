"""Yahoo Fantasy support via the official OAuth2 API.  BETA.

Read-only, like everything else here: the bot observes drafts and advises.
Yahoo, unlike Sleeper, has no anonymous API - the user signs in with Yahoo's
own consent screen and we receive a token scoped to Fantasy Sports reads.
We never see a password; the operator registers an app once and sets
YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET (nothing here activates without them).

Honesty about the beta label: Yahoo's draftresults endpoint is documented for
completed drafts and in practice fills during live ones, but the update
cadence during a live draft is not contractual the way Sleeper's is.  First
proving ground should be a Yahoo mock, not a league that matters.

Yahoo's JSON is XML translated literally - collections arrive as dicts of
numeric-string keys plus a "count", and one entity is a list of one-key
dicts.  The walkers below exist so the rest of the file can read plainly.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

from .sleeper import FANTASY_POS, DraftState, Pick

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API = "https://fantasysports.yahooapis.com/fantasy/v2"
TIMEOUT = 15.0

# Yahoo position strings -> ours.  Anything unmapped is ignored (IDP etc).
POS_MAP = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K",
           "DEF": "DEF", "W/R/T": "FLEX", "W/R": "FLEX", "Q/W/R/T":
           "SUPER_FLEX", "BN": "BN", "IR": "IR"}


class YahooError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("YAHOO_CLIENT_ID")
                and os.environ.get("YAHOO_CLIENT_SECRET"))


# ------------------------------------------------------------------ oauth


def authorize_url(redirect_uri: str, state: str) -> str:
    q = urllib.parse.urlencode({
        "client_id": os.environ["YAHOO_CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    })
    return f"{AUTH_URL}?{q}"


def _token_request(fields: dict[str, str]) -> dict:
    cid = os.environ["YAHOO_CLIENT_ID"]
    sec = os.environ["YAHOO_CLIENT_SECRET"]
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            tok = json.loads(r.read().decode())
    except Exception as e:
        raise YahooError(f"token exchange failed: {e}") from e
    if "access_token" not in tok:
        raise YahooError(f"token exchange refused: {tok.get('error', '?')}")
    tok["expires_at"] = time.time() + float(tok.get("expires_in", 3600)) - 60
    return tok


def exchange_code(code: str, redirect_uri: str) -> dict:
    return _token_request({"grant_type": "authorization_code",
                           "code": code, "redirect_uri": redirect_uri})


def refresh_token(tokens: dict) -> dict:
    fresh = _token_request({"grant_type": "refresh_token",
                            "refresh_token": tokens["refresh_token"]})
    fresh.setdefault("refresh_token", tokens["refresh_token"])
    return fresh


def _bearer(tokens: dict) -> dict:
    """Valid tokens, refreshing in place when they are near expiry."""
    if time.time() >= float(tokens.get("expires_at") or 0):
        tokens.update(refresh_token(tokens))
    return tokens


def _get(tokens: dict, path: str) -> dict:
    _bearer(tokens)
    req = urllib.request.Request(
        f"{API}{path}{'&' if '?' in path else '?'}format=json",
        headers={"Authorization": f"Bearer {tokens['access_token']}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        raise YahooError(f"Yahoo API {path}: {e}") from e


# --------------------------------------------------- yahoo-json unpacking


def _entity(blob: Any) -> dict:
    """Yahoo renders one entity as a list of one-key dicts; flatten it."""
    out: dict = {}
    items = blob if isinstance(blob, list) else [blob]
    for part in items:
        if isinstance(part, dict):
            out.update(part)
    return out


def _collection(blob: Any) -> list:
    """{"0": {...}, "1": {...}, "count": 2} -> [values in order]."""
    if not isinstance(blob, dict):
        return []
    out = []
    for i in range(int(blob.get("count") or 0)):
        item = blob.get(str(i))
        if item is not None:
            out.append(item)
    return out


# ------------------------------------------------------------- api reads


def user_leagues(tokens: dict, season_game: str = "nfl") -> list[dict]:
    data = _get(tokens,
                f"/users;use_login=1/games;game_keys={season_game}/leagues")
    out = []
    users = data.get("fantasy_content", {}).get("users", {})
    for user in _collection(users):
        games = _entity(user.get("user")).get("games", {})
        for game in _collection(games):
            leagues = _entity(game.get("game")).get("leagues", {})
            for lg in _collection(leagues):
                ent = _entity(lg.get("league"))
                if ent.get("league_key"):
                    out.append({
                        "league_key": ent["league_key"],
                        "name": ent.get("name", ""),
                        "num_teams": int(ent.get("num_teams") or 0),
                        "draft_status": ent.get("draft_status", ""),
                        "scoring_type": ent.get("scoring_type", ""),
                    })
    return out


def league_settings(tokens: dict, league_key: str) -> dict:
    data = _get(tokens, f"/league/{league_key}/settings")
    league = data.get("fantasy_content", {}).get("league", [])
    ent = _entity(league)
    settings = {}
    for part in (league if isinstance(league, list) else [league]):
        if isinstance(part, dict) and "settings" in part:
            settings = _entity(part["settings"])
    roster: dict[str, int] = {}
    for rp in _collection(settings.get("roster_positions", {})) or \
            (settings.get("roster_positions") or []):
        pos_ent = _entity(rp.get("roster_position", rp))
        pos = POS_MAP.get(str(pos_ent.get("position", "")))
        if pos:
            roster[pos] = roster.get(pos, 0) + int(pos_ent.get("count") or 1)
    return {
        "name": ent.get("name", ""),
        "num_teams": int(ent.get("num_teams") or 12),
        "scoring": ent.get("scoring_type", ""),
        "roster": roster,
        "is_auction": str(settings.get("is_auction_draft") or "0") == "1",
    }


def draft_results(tokens: dict, league_key: str) -> list[dict]:
    data = _get(tokens, f"/league/{league_key}/draftresults")
    league = data.get("fantasy_content", {}).get("league", [])
    results = {}
    for part in (league if isinstance(league, list) else [league]):
        if isinstance(part, dict) and "draft_results" in part:
            results = part["draft_results"]
    out = []
    for row in _collection(results):
        ent = _entity(row.get("draft_result", row))
        if ent.get("player_key"):
            out.append({
                "pick": int(ent.get("pick") or 0),
                "round": int(ent.get("round") or 0),
                "team_key": str(ent.get("team_key") or ""),
                "player_key": str(ent.get("player_key") or ""),
            })
    out.sort(key=lambda r: r["pick"])
    return out


def players_by_keys(tokens: dict, league_key: str,
                    keys: list[str]) -> dict[str, dict]:
    """player_key -> {name, pos, team} in batches of 25 (Yahoo's page size)."""
    found: dict[str, dict] = {}
    for i in range(0, len(keys), 25):
        chunk = ",".join(keys[i:i + 25])
        data = _get(tokens, f"/league/{league_key}/players;player_keys={chunk}")
        league = data.get("fantasy_content", {}).get("league", [])
        players = {}
        for part in (league if isinstance(league, list) else [league]):
            if isinstance(part, dict) and "players" in part:
                players = part["players"]
        for row in _collection(players):
            ent = _entity(row.get("player", row))
            pk = str(ent.get("player_key") or "")
            name = ent.get("name")
            full = name.get("full") if isinstance(name, dict) else str(name or "")
            pos = str(ent.get("display_position") or "").split(",")[0].strip()
            if pk and full:
                found[pk] = {
                    "name": full,
                    "pos": pos if pos in FANTASY_POS else "",
                    "team": str(ent.get("editorial_team_abbr") or "").upper()
                            or None,
                }
    return found


# ------------------------------------------------------------- provider


class YahooProvider:
    """Feeds a DraftState from Yahoo draftresults.  BETA.

    Slots are inferred from round one: Yahoo reports team_key per pick, and
    the round-one order IS the slot order in a snake draft.
    """

    def __init__(self, tokens: dict, league_key: str,
                 my_team_key: str | None = None) -> None:
        self.tokens = tokens
        self.league_key = league_key
        self.my_team_key = my_team_key
        self._names: dict[str, dict] = {}     # player_key -> meta cache
        self._slot_of: dict[str, int] = {}    # team_key -> slot

    def build_state(self) -> DraftState:
        cfg = league_settings(self.tokens, self.league_key)
        scoring = {"head": "ppr"}.get(cfg["scoring"], "half_ppr")
        rounds = max(1, sum(v for k, v in cfg["roster"].items()
                            if k != "IR"))
        state = DraftState(
            draft_id=f"yahoo:{self.league_key}",
            teams=cfg["num_teams"] or 12,
            rounds=rounds,
            draft_type="snake",
            scoring=scoring,
            status="drafting",
            roster_slots=cfg["roster"],
            name=cfg["name"] or f"Yahoo {self.league_key}",
            provider=self,
        )
        self.refresh(state)
        return state

    def refresh(self, state: DraftState) -> None:
        rows = draft_results(self.tokens, self.league_key)
        if not rows:
            return
        # Round-one order fixes team_key -> slot for the whole draft.
        for row in rows:
            if row["round"] == 1 and row["team_key"] not in self._slot_of:
                self._slot_of[row["team_key"]] = \
                    ((row["pick"] - 1) % state.teams) + 1
        missing = [r["player_key"] for r in rows
                   if r["player_key"] not in self._names]
        if missing:
            self._names.update(
                players_by_keys(self.tokens, self.league_key, missing))
        picks: list[Pick] = []
        for row in rows:
            meta = self._names.get(row["player_key"]) or {}
            picks.append(Pick(
                pick_no=row["pick"],
                round=row["round"] or ((row["pick"] - 1) // state.teams + 1),
                slot=self._slot_of.get(row["team_key"],
                                       state.slot_for_pick(row["pick"])),
                roster_id=None,
                player_id=row["player_key"],
                name=meta.get("name") or row["player_key"],
                pos=meta.get("pos") or "",
                team=meta.get("team"),
                picked_by=row["team_key"],
            ))
        state.picks = picks
        if self.my_team_key and self.my_team_key in self._slot_of:
            state.my_slot = self._slot_of[self.my_team_key]
        if len(picks) >= state.total_picks:
            state.status = "complete"
