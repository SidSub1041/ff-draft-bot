"""Offline draft simulation, for testing and for dry runs without Sleeper.

`synthetic_state()` fabricates a DraftState with any league shape, and
`autopick()` fills rival picks using the same opponent model the live engine
uses - so you can rehearse a full draft, or regression-test the engine, with no
network and no Sleeper room.
"""

from __future__ import annotations

import random

from .sleeper import DraftState, Pick


def synthetic_state(teams: int = 12, rounds: int = 15, my_slot: int = 5,
                    scoring: str = "ppr", superflex: bool = False,
                    dynasty: bool = False, draft_id: str = "offline",
                    starters: dict[str, int] | None = None) -> DraftState:
    slots = starters or {
        "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1,
    }
    slots = dict(slots)
    if superflex:
        slots["SUPER_FLEX"] = 1
    slots.setdefault("BN", max(0, rounds - sum(
        v for k, v in slots.items() if k != "BN")))
    return DraftState(
        draft_id=draft_id, teams=teams, rounds=rounds, draft_type="snake",
        scoring=scoring, status="drafting", roster_slots=slots,
        my_slot=my_slot, name=f"Offline {teams}-team {scoring}",
        is_dynasty=dynasty,
    )


def autopick(engine, upto: int, rng: random.Random | None = None,
             my_strategy=None) -> list[Pick]:
    """Advance the draft to just before overall pick `upto`.

    Rival picks are sampled from the opponent model.  Your own picks are taken
    from `my_strategy(engine) -> key` if given, else the engine's top pick.
    """
    rng = rng or random.Random(7)
    state = engine.state
    while state.next_pick_no < upto:
        pick_no = state.next_pick_no
        slot = state.slot_for_pick(pick_no)
        pool = engine.available()
        if not pool:
            break

        if slot == state.my_slot:
            recs = engine.recommend(1, pick_no=pick_no, sim_runs=120)
            if not recs:
                break
            chosen = engine.projection(recs[0].key)
        else:
            counts = engine.state.pos_counts(slot)
            needs = engine.opponents.needs(slot)
            import math
            cands = pool[:30]
            logits = [
                engine.opponents.pick_logit(
                    slot, c.pos,
                    engine.adp.adp_of(c.name, c.pos,
                                      engine.adp.undrafted_prior(c.pos)),
                    pick_no, needs)
                for c in cands
            ]
            m = max(logits)
            w = [math.exp(l - m) for l in logits]
            chosen = rng.choices(cands, weights=w, k=1)[0]

        meta = engine._sleeper_meta.get(chosen.key) or {}
        state.picks.append(Pick(
            pick_no=pick_no, round=(pick_no - 1) // state.teams + 1, slot=slot,
            roster_id=slot, player_id=meta.get("player_id", chosen.key),
            name=chosen.name, pos=chosen.pos, team=meta.get("team"),
            picked_by=None,
        ))
        engine.refresh(fetch=False)
    return state.picks
