"""MCP server - exposes the draft engine as a local Claude Code plugin.

Run directly (`python -m ffbot.mcp_server`) or register it in .mcp.json.
One draft session lives in module state, so tool calls share context across a
conversation: connect once, then keep asking.

The bot never submits a pick to Sleeper.  Every tool here is read-only against
Sleeper; the only writes are to the local SQLite store.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import explain, sleeper
from .engine import Engine
from .guide import get_guide
from .mockdraft import autopick, synthetic_state
from .store import Store
from .strategy import PRESETS, Strategy, for_league

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover
    print("The 'mcp' package is required for the plugin server:\n"
          "    pip install mcp", file=sys.stderr)
    raise

INSTRUCTIONS = """
Live fantasy football draft assistant, built on Joel Smyth's 2026 Draft Guide
and the Sleeper API.

Typical flow:
  1. connect_draft (live/mock Sleeper draft) or start_offline_mock (rehearsal)
  2. refresh whenever picks may have happened - it reports new picks and
     whether the user is on the clock
  3. recommend to get ranked suggestions with scored reasons
  4. explain_player / compare_players / board / room to dig in
  5. set_strategy / bump_player / record_feedback to adjust after a mock

The bot advises only - it never makes a pick. When the user asks "who should I
take", call refresh then recommend, and present the reasoning in your own words
rather than dumping raw JSON. Every recommendation carries a `reasons` list with
signed weights showing exactly what drove the score.
""".strip()

server = MCPServer(
    name="ff-draft-bot",
    version="1.0.0",
    instructions=INSTRUCTIONS,
)

_engine: Engine | None = None


def _need() -> Engine:
    if _engine is None:
        raise RuntimeError(
            "No draft connected. Call connect_draft with a Sleeper draft id or "
            "URL, or start_offline_mock for a rehearsal.")
    return _engine


def _state_blob(e: Engine) -> dict:
    st = e.state
    return {
        "draft_id": st.draft_id,
        "name": st.name,
        "teams": st.teams,
        "rounds": st.rounds,
        "scoring": st.scoring,
        "superflex": st.superflex,
        "dynasty": st.is_dynasty,
        "guide_board": st.fmt,
        "my_slot": st.my_slot,
        "status": st.status,
        "picks_made": len(st.picks),
        "current_round": st.current_round,
        "on_the_clock_slot": st.on_the_clock_slot,
        "is_my_turn": st.is_my_turn,
        "picks_until_my_turn": st.picks_until_my_turn(),
        "my_next_picks": st.my_upcoming_picks(4),
        "roster_slots": st.roster_slots,
        "strategy": e.strategy.name,
        "my_roster": [
            {"pick": p.pick_no, "round": p.round, "pos": p.pos, "name": p.name,
             "team": p.team}
            for p in sorted(e.state.my_roster(), key=lambda x: x.pick_no)
        ],
    }


# --------------------------------------------------------------- connection


@server.tool(description="Connect to a live or mock Sleeper draft by id or URL. "
                         "Read-only: the bot observes and advises, never picks.")
def connect_draft(draft: str, sleeper_username: str = "", slot: int = 0,
                  strategy: str = "") -> dict[str, Any]:
    global _engine
    draft_id = sleeper.extract_draft_id(draft)
    uid = sleeper.user_id(sleeper_username) if sleeper_username else None
    players = sleeper.load_players()
    state = sleeper.build_state(draft_id, my_user_id=uid,
                                my_slot=slot or None, players=players)
    strat = Strategy.load(strategy) if strategy else \
        for_league(state.teams, state.superflex, state.is_dynasty)
    _engine = Engine(state, strategy=strat, players=players)
    blob = _state_blob(_engine)
    if state.my_slot is None:
        blob["warning"] = ("Your draft slot is unknown - pass sleeper_username "
                           "or slot so recommendations can be roster-aware.")
    blob["strategy_detail"] = explain.strategy_summary(_engine)
    return blob


@server.tool(description="Start an offline rehearsal draft with no Sleeper room. "
                         "Rival picks are simulated. Good for testing strategies.")
def start_offline_mock(teams: int = 12, rounds: int = 15, slot: int = 5,
                       scoring: str = "ppr", superflex: bool = False,
                       dynasty: bool = False, simulate_to_pick: int = 0,
                       strategy: str = "") -> dict[str, Any]:
    global _engine
    state = synthetic_state(teams=teams, rounds=rounds, my_slot=slot,
                            scoring=scoring, superflex=superflex,
                            dynasty=dynasty)
    strat = Strategy.load(strategy) if strategy else \
        for_league(teams, superflex, dynasty)
    _engine = Engine(state, strategy=strat)
    if simulate_to_pick > 1:
        autopick(_engine, simulate_to_pick)
    return _state_blob(_engine)


@server.tool(description="Find a Sleeper user's drafts for a season, to get a draft id.")
def list_my_drafts(sleeper_username: str, season: str = "2026") -> dict[str, Any]:
    uid = sleeper.user_id(sleeper_username)
    if not uid:
        return {"error": f"no Sleeper user named {sleeper_username!r}"}
    drafts = sleeper.user_drafts(uid, season)
    return {
        "user_id": uid,
        "drafts": [{
            "draft_id": d["draft_id"], "status": d.get("status"),
            "type": d.get("type"),
            "teams": (d.get("settings") or {}).get("teams"),
            "rounds": (d.get("settings") or {}).get("rounds"),
            "scoring": (d.get("metadata") or {}).get("scoring_type"),
            "name": (d.get("metadata") or {}).get("name"),
        } for d in drafts],
        "note": ("Sleeper mock drafts often do not appear here - paste the "
                 "draft URL from the browser instead."),
    }


# -------------------------------------------------------------- live state


@server.tool(description="Poll Sleeper for new picks. Returns what changed, who "
                         "is on the clock, and whether it is the user's turn.")
def refresh() -> dict[str, Any]:
    e = _need()
    events = e.refresh(fetch=e.state.draft_id != "offline")
    blob = _state_blob(e)
    blob["new_picks"] = events
    blob["recent_picks"] = [e._describe_pick(p) for p in e.state.picks[-8:]]
    return blob


@server.tool(description="Ranked pick suggestions for the user's next pick, each "
                         "with a scored breakdown of why. Optionally filter to one "
                         "position, or ask about a future pick number.")
def recommend(count: int = 5, position: str = "", pick_no: int = 0
              ) -> dict[str, Any]:
    e = _need()
    recs = e.recommend(count, pick_no=pick_no or None,
                       pos_filter=position or None)
    target = pick_no or (e.state.my_upcoming_picks(1) or [e.state.next_pick_no])[0]
    return {
        "pick_no": target,
        "round": (target - 1) // e.state.teams + 1,
        "header": explain.pick_header(e, target),
        "strategy": e.strategy.name,
        "script_says": e.strategy.target_for_round(
            (target - 1) // e.state.teams + 1),
        "recommendations": [r.to_dict() for r in recs],
        "top_pick_explanation": explain.explain(e, recs[0], target) if recs else "",
        "note": ("`off_guide: true` means the player is outside the guide's top "
                 "150 and is valued from market rank only."),
    }


@server.tool(description="Full reasoning for one player: projection, ADP, guide "
                         "notes, availability, and whether he is still on the board.")
def explain_player(name: str) -> dict[str, Any]:
    e = _need()
    key = e.resolve_key(name)
    if not key:
        return {"error": f"no player matching {name!r}"}
    proj = e.projection(key)
    if not proj:
        return {"error": f"no projection for {name!r}"}
    gp = e.guide.players.get(key)
    recs = e.recommend(40)
    match = next((r for r in recs if r.key == key), None)
    out = {
        "name": proj.name, "pos": proj.pos,
        "available": e.is_available(key),
        "projected_ppg": round(proj.ppg, 2),
        "floor": round(proj.floor, 2), "ceiling": round(proj.upside, 2),
        "sigma": round(proj.sigma, 2),
        "vor": round(proj.vor, 2), "replacement": round(proj.replacement, 2),
        "guide_pos_rank": proj.pos_rank, "guide_overall_rank": proj.overall_rank,
        "adj_ppg_2025": gp.adj_ppg_2025 if gp else None,
        "dynasty_rookie_rank": gp.rookie_rank if gp else None,
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
            f"{proj.name} is not in the shortlist for this pick - the engine "
            f"does not rate him here.")
    return out


@server.tool(description="Head-to-head comparison of two players at the current pick.")
def compare_players(player_a: str, player_b: str) -> dict[str, Any]:
    e = _need()
    recs = e.recommend(45)
    return {"comparison": explain.compare(e, recs, player_a, player_b)}


@server.tool(description="Positional outlook: best available, tier cliffs, and how "
                         "many of each position are expected to go before the "
                         "user's next pick.")
def board() -> dict[str, Any]:
    e = _need()
    if e.last_sim is None or not e.last_sim.runs:
        e.recommend(5)
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


@server.tool(description="Read the room: each rival's roster, whether they reach or "
                         "wait for value, positional lean, and any run in progress.")
def room() -> dict[str, Any]:
    e = _need()
    return {
        "summary": explain.opponent_summary(e),
        "field_positional_share": {k: round(v, 3)
                                   for k, v in e.opponents.field_share.items()},
        "run_pressure": {p: round(e.opponents.run_pressure(p), 3)
                         for p in ("QB", "RB", "WR", "TE")},
        "slots": [
            {"slot": s,
             "is_me": s == e.state.my_slot,
             "read": desc,
             "roster": [{"round": pk.round, "pos": pk.pos, "name": pk.name}
                        for pk in e.state.roster_of_slot(s)]}
            for s, desc in e.opponents.summary()
        ],
    }


@server.tool(description="The user's roster so far, with projections and unfilled "
                         "starting slots.")
def roster() -> dict[str, Any]:
    e = _need()
    return {"summary": explain.roster_summary(e),
            "counts": e.my_counts(),
            "rb_inside_guide_top_n": e.my_rb_top_n(),
            "state": _state_blob(e)}


# ---------------------------------------------------------------- strategy


@server.tool(description="Show the active strategy and every tunable knob.")
def get_strategy() -> dict[str, Any]:
    e = _need()
    return {"summary": explain.strategy_summary(e),
            "presets": list(PRESETS),
            "config": e.strategy.to_dict()}


@server.tool(description="Change strategy: switch preset and/or set individual "
                         "fields. `settings` is a JSON object of field->value, "
                         "e.g. {\"rb_target_count\": 2, \"wr_sweet_rounds\": [3,4,5]}.")
def set_strategy(preset: str = "", settings: dict[str, Any] | None = None,
                 describe: str = "", save_as: str = "") -> dict[str, Any]:
    e = _need()
    log = []
    if preset:
        if preset not in PRESETS and preset not in Strategy.available():
            return {"error": f"unknown preset {preset!r}",
                    "available": Strategy.available()}
        e.strategy = Strategy.load(preset)
        log.append(f"preset -> {preset}")
    if settings:
        log += e.strategy.adjust(**settings)
    if describe:
        e.strategy.notes.append(describe)
        log.append(f"note: {describe}")
    if save_as:
        path = e.strategy.save(save_as)
        log.append(f"saved to {path}")
    e.store.add_feedback(e.state.draft_id, "strategy", "; ".join(log),
                         {"preset": preset, "settings": settings or {}})
    return {"changes": log, "summary": explain.strategy_summary(e)}


@server.tool(description="Permanently nudge a player up or down in the user's "
                         "rankings (in projected-points units). Persists across drafts.")
def bump_player(name: str, delta: float, reason: str = "") -> dict[str, Any]:
    e = _need()
    key = e.resolve_key(name)
    if not key:
        return {"error": f"no player matching {name!r}"}
    e.strategy.player_bumps[key] = delta
    e.store.set_player_bias(key, delta, reason or "manual")
    e.biases = e.store.player_biases()
    proj = e.projection(key)
    return {"player": proj.name if proj else key, "delta": delta,
            "reason": reason}


@server.tool(description="Exclude a player from all recommendations for this draft.")
def ban_player(name: str) -> dict[str, Any]:
    e = _need()
    key = e.resolve_key(name)
    if not key:
        return {"error": f"no player matching {name!r}"}
    if key not in e.strategy.banned:
        e.strategy.banned.append(key)
    return {"banned": key, "count": len(e.strategy.banned)}


# ---------------------------------------------------------------- feedback


@server.tool(description="Record the user's feedback or a concern during/after a "
                         "draft, so it can be reviewed and turned into tuning later.")
def record_feedback(text: str, kind: str = "note") -> dict[str, Any]:
    e = _engine
    fid = Store().add_feedback(e.state.draft_id if e else None, kind, text)
    return {"recorded": fid, "text": text}


@server.tool(description="Post-draft review: where the user overruled the bot, plus "
                         "their recorded notes. Use this to propose strategy tuning.")
def review(draft_id: str = "") -> dict[str, Any]:
    store = Store()
    drafts = store.list_drafts()
    if not drafts:
        return {"error": "no drafts recorded yet"}
    did = draft_id or drafts[0].draft_id
    rep = store.agreement_report(did)
    rep["draft_id"] = did
    rep["notes"] = [f for f in store.list_feedback(40) if f["draft_id"] == did]
    rep["recent_drafts"] = [
        {"draft_id": d.draft_id, "name": d.name, "teams": d.teams,
         "scoring": d.scoring, "picks": d.picks, "status": d.status}
        for d in drafts[:10]]
    rep["tuning_hint"] = (
        "If a pattern shows up (e.g. the user keeps taking RBs the bot fades), "
        "propose a set_strategy call - rb_dead_zone_penalty, wr_sweet_rounds, "
        "qb_min_round, pos_weights and bench_targets are the usual levers.")
    return rep


# ------------------------------------------------------------------- guide


@server.tool(description="Query the draft guide itself: a player's rankings and "
                         "notes, or a keyword search across its Top 50 stats.")
def guide_lookup(query: str) -> dict[str, Any]:
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
        out["error"] = f"nothing in the guide matches {query!r}"
    return out


@server.tool(description="The guide's own written draft strategy: round-by-round "
                         "script, positional targets and fades, and its six rules.")
def guide_strategy() -> dict[str, Any]:
    return get_guide().strategy


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
