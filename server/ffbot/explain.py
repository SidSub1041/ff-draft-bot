"""Turning scores into reasoning you can argue with.

Everything here is deterministic prose built from the engine's own numbers -
no model calls - so the CLI works offline and the MCP server has something
concrete to hand Claude when you chat about a pick.
"""

from __future__ import annotations

import textwrap

from .engine import Engine, Recommendation

WRAP = 78


def _wrap(text: str, indent: str = "     ") -> str:
    return textwrap.fill(text, WRAP, initial_indent=indent,
                         subsequent_indent=indent)


def pick_header(engine: Engine, pick_no: int | None = None) -> str:
    st = engine.state
    pick_no = pick_no or (st.my_upcoming_picks(1) or [st.next_pick_no])[0]
    rnd = (pick_no - 1) // st.teams + 1
    counts = engine.my_counts()
    have = " ".join(f"{p}{counts[p]}" for p in ("QB", "RB", "WR", "TE")
                    if counts.get(p))
    upcoming = st.my_upcoming_picks(3)
    nxt = f", then {upcoming[1]}" if len(upcoming) > 1 else ""
    target = engine.strategy.target_for_round(rnd)
    return (f"Pick {pick_no} (Round {rnd}, slot {st.my_slot}){nxt} | "
            f"roster: {have or 'empty'} | "
            f"{engine.strategy.name} script says {target}")


def explain(engine: Engine, rec: Recommendation, pick_no: int | None = None,
            verbose: bool = True) -> str:
    st = engine.state
    pick_no = pick_no or (st.my_upcoming_picks(1) or [st.next_pick_no])[0]
    p = rec.proj

    lines = []
    rankbit = (f"guide {p.pos}{p.pos_rank}"
               + (f", #{p.overall_rank} overall" if p.overall_rank else ""))
    if rec.off_guide:
        rankbit = f"not in the guide's top 150; market {p.pos}{p.pos_rank}"
    lines.append(f"{rec.name} ({rec.pos}{' - ' + rec.team if rec.team else ''}) "
                 f"- {rankbit}")
    urgency = rec.score - rec.value_now - rec.lookahead
    lines.append(f"  score {rec.score:.2f}  =  {rec.value_now:.2f} taking him now "
                 f"+ {rec.lookahead:.2f} expected board at your next pick"
                 + (f" + {urgency:.2f} cost of waiting" if abs(urgency) > 0.05 else ""))

    # --- market
    if rec.adp < 900:
        if rec.adp_delta > 4:
            lines.append(f"  Market: ADP {rec.adp:.0f}, so he has fallen "
                         f"{rec.adp_delta:.0f} picks past where he usually goes.")
        elif rec.adp_delta < -4:
            lines.append(f"  Market: ADP {rec.adp:.0f}, so this is a reach of "
                         f"{abs(rec.adp_delta):.0f} picks.")
        else:
            lines.append(f"  Market: ADP {rec.adp:.0f} - right on schedule.")

    # --- projection
    hist = (f", 2025 adjusted {p.hist_ppg:.1f}" if p.hist_ppg is not None
            else ", no 2025 baseline")
    lines.append(f"  Projection: {p.ppg:.1f} PPG (floor {p.floor:.1f} / "
                 f"ceiling {p.upside:.1f}{hist}); "
                 f"{p.vor:+.1f} vs {p.pos} replacement {p.replacement:.1f}.")

    # --- scarcity
    if rec.tier:
        lines.append(f"  Scarcity: {p.pos} tier {rec.tier[0]}, "
                     f"{rec.tier[1]} player(s) left in it.")
    if engine.last_sim and engine.last_sim.runs:
        nxt = [x for x in st.my_upcoming_picks(2) if x > pick_no]
        if nxt:
            lines.append(f"  Availability: {rec.survival:.0%} chance he lasts to "
                         f"your next pick at {nxt[0]} "
                         f"({engine.last_sim.runs} simulated drafts).")

    # --- scored reasons
    scored = [r for r in rec.reasons if abs(r.weight) > 0.05
              and r.kind in ("strategy", "risk", "roster", "scarcity")]
    if scored:
        lines.append("  Strategy fit:")
        for r in sorted(scored, key=lambda r: -abs(r.weight)):
            lines.append(_wrap(f"{r.weight:+.2f}  {r.text}", "     "))

    # --- guide colour
    if verbose and p.notes:
        lines.append("  From the guide:")
        for note in p.notes[:3]:
            lines.append(_wrap(note, "     "))

    return "\n".join(lines)


def brief(rec: Recommendation, idx: int) -> str:
    flag = " *" if rec.off_guide else ""
    adp = f"ADP {rec.adp:.0f}" if rec.adp < 900 else "no ADP"
    surv = f"{rec.survival:.0%} to last" if rec.survival < 0.999 else "-"
    return (f"{idx}. {rec.name:<24s} {rec.pos:<3s}{flag:<2s} "
            f"score {rec.score:6.2f}  {adp:<9s}  {surv}")


def compare(engine: Engine, recs: list[Recommendation], a: str, b: str) -> str:
    """Head-to-head between two candidates already scored this pick."""
    idx = {r.key: r for r in recs}
    ka, kb = engine.resolve_key(a), engine.resolve_key(b)
    ra, rb = idx.get(ka or ""), idx.get(kb or "")

    for label, key, rec in ((a, ka, ra), (b, kb, rb)):
        if rec is not None:
            continue
        if not key:
            return f"No player matching {label!r}."
        proj = engine.projection(key)
        if proj is None:
            return f"No projection for {label!r}."
        if not engine.is_available(key):
            taken = next((p for p in engine.state.picks
                          if engine.resolve_key(p.name, p.pos) == key), None)
            where = (f" - taken at pick {taken.pick_no} by slot {taken.slot}"
                     if taken else "")
            return (f"{proj.name} is already off the board{where}. "
                    f"Compare against someone still available.")
        return (f"{proj.name} ({proj.pos}{proj.pos_rank}, {proj.ppg:.1f} PPG, "
                f"ADP {engine.adp.adp_of(proj.name, proj.pos):.0f}) is available "
                f"but did not make this pick's shortlist - the engine does not "
                f"rate him here.")

    out = [f"{ra.name} vs {rb.name}", ""]
    rows = [
        ("score", f"{ra.score:.2f}", f"{rb.score:.2f}"),
        ("projected PPG", f"{ra.proj.ppg:.1f}", f"{rb.proj.ppg:.1f}"),
        ("VOR", f"{ra.proj.vor:+.1f}", f"{rb.proj.vor:+.1f}"),
        ("ceiling", f"{ra.proj.upside:.1f}", f"{rb.proj.upside:.1f}"),
        ("floor", f"{ra.proj.floor:.1f}", f"{rb.proj.floor:.1f}"),
        ("ADP", f"{ra.adp:.0f}", f"{rb.adp:.0f}"),
        ("lasts to next pick", f"{ra.survival:.0%}", f"{rb.survival:.0%}"),
    ]
    w = max(len(r[0]) for r in rows)
    for label, x, y in rows:
        out.append(f"  {label:<{w}s}   {x:>8s}   {y:>8s}")

    out.append("")
    diff = ra.score - rb.score
    lead, trail = (ra, rb) if diff > 0 else (rb, ra)
    out.append(f"  {lead.name} by {abs(diff):.2f}.")

    # Which terms actually created the gap?
    def terms(r):
        return {t.text: t.weight for t in r.reasons if abs(t.weight) > 0.05}
    for r, other in ((lead, trail), (trail, lead)):
        uniq = [t for t in terms(r) if t not in terms(other)]
        if uniq:
            out.append(f"  Only {r.name}: " + "; ".join(uniq[:3]))

    if trail.survival > lead.survival + 0.25:
        out.append(f"  Note: {trail.name} is {trail.survival:.0%} to last to your "
                   f"next pick vs {lead.survival:.0%} - taking {lead.name} now "
                   f"may get you both.")
    return "\n".join(out)


def board_summary(engine: Engine, pick_no: int | None = None) -> str:
    out = ["Positional outlook before your pick:"]
    for pos, d in engine.positional_outlook(pick_no).items():
        tier = f"tier {d['current_tier'][0]} ({d['current_tier'][1]} left)" \
            if d["current_tier"] else "-"
        out.append(f"  {pos}: best {d['best'] or '-':<22s} {tier:<20s} "
                   f"~{d['expected_gone_of_top12']:.1f} of top 12 gone before you pick")
    return "\n".join(out)


def opponent_summary(engine: Engine) -> str:
    st = engine.state
    out = ["Room read:"]
    for slot, desc in engine.opponents.summary():
        tag = " (you)" if slot == st.my_slot else ""
        out.append(f"  slot {slot:>2d}{tag}: {desc}")
    runs = [(p, engine.opponents.run_pressure(p)) for p in ("QB", "RB", "WR", "TE")]
    hot = [f"{p} ({v:.2f})" for p, v in runs if v > 0.05]
    if hot:
        out.append("  Run pressure right now: " + ", ".join(hot))
    return "\n".join(out)


def roster_summary(engine: Engine) -> str:
    st = engine.state
    mine = st.my_roster()
    if not mine:
        return "You have not drafted anyone yet."
    out = [f"Your roster (slot {st.my_slot}):"]
    total = 0.0
    for p in sorted(mine, key=lambda x: x.pick_no):
        key = engine.resolve_key(p.name, p.pos)
        proj = engine.projection(key) if key else None
        ppg = f"{proj.ppg:5.1f} PPG" if proj else "     -    "
        total += proj.ppg if proj else 0.0
        out.append(f"  R{p.round:02d} p{p.pick_no:>3}  {p.pos:<3s} "
                   f"{p.name:<24s} {ppg}")
    need = []
    for pos, cnt in st.starters.items():
        have = engine.my_counts().get(pos, 0)
        if pos in ("QB", "RB", "WR", "TE", "K", "DEF") and have < cnt:
            need.append(f"{cnt - have} {pos}")
    out.append(f"  Starting-slot gaps: {', '.join(need) if need else 'none'}")
    return "\n".join(out)


def strategy_summary(engine: Engine) -> str:
    s = engine.strategy
    st = engine.state
    out = [f"Strategy: {s.name}", f"  {s.description}"]
    out.append("  Round script: " + " ".join(
        f"{r}:{s.target_for_round(r)}" for r in range(1, min(st.rounds, 15) + 1)))
    out.append(f"  RB goal: {s.rb_target_count} inside the top "
               f"{s.rb_target_top_n} (you have {engine.my_rb_top_n()})")
    out.append(f"  QB band: QB{s.qb_target_pos_rank[0]}-{s.qb_target_pos_rank[1]}, "
               f"not before round {s.qb_min_round}")
    out.append(f"  RB dead zone: RB{s.rb_dead_zone[0]}-{s.rb_dead_zone[1]} "
               f"(penalty {s.rb_dead_zone_penalty})")
    if s.notes:
        for n in s.notes:
            out.append(_wrap(n, "  - "))
    src = ("learned from "
           f"{engine.learned_draft_count} observed draft(s)"
           if engine.learned_draft_count >= 2 else "default priors")
    out.append(f"  ADP source: {src}")
    if s.player_bumps:
        out.append("  Your player adjustments: " + ", ".join(
            f"{k} {v:+.1f}" for k, v in s.player_bumps.items()))
    return "\n".join(out)
