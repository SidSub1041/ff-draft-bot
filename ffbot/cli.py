"""Command line interface.

  ffbot connect <draft-url-or-id> --user <sleeper-username>
  ffbot mock --teams 12 --slot 5          # offline rehearsal, no Sleeper needed
  ffbot serve --connect <draft-url-or-id> # same bot, as a local web panel
  ffbot drafts --user <sleeper-username>  # find your draft ids
  ffbot review [draft_id]                 # post-draft: where you and the bot disagreed
  ffbot strategy list|show|set

Inside a live session you get a prompt: press Enter to refresh, or type a
question ("why tuten", "compare tuten hampton", "what if I go WR here").
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

from . import explain, sleeper
from .engine import Engine
from .guide import get_guide
from .mockdraft import autopick, synthetic_state
from .store import Store
from .strategy import PRESETS, Strategy, for_league

BANNER = r"""
  ff-draft-bot  -  live draft assistant
  built on Joel Smyth's 2026 Draft Guide + Sleeper
"""


# --------------------------------------------------------------- chat router


HELP = """
Commands
  <enter>                 refresh picks and re-rank
  rec [n] [POS]           top n recommendations (optionally one position)
  why <player>            full reasoning for a candidate
  compare <a> vs <b>      head-to-head
  board                   positional outlook / tier cliffs
  room                    what each opponent is doing
  roster                  your team so far
  strategy                current strategy settings
  set <field> <value>     change a strategy knob (e.g. set rb_target_count 2)
  preset <name>           switch strategy (joel_rb_heavy, zero_rb, hero_rb, bpa,
                          superflex, dynasty)
  bump <player> <delta>   nudge a player up/down permanently (e.g. bump tuten 1.5)
  ban <player>            never recommend this player
  note <text>             record feedback for post-draft review
  stats <term>            search the guide's Top 50 stats
  player <name>           guide profile for any player
  help / quit
"""


def chat(engine: Engine, line: str, last_recs: list) -> tuple[str, list]:
    """Handle one line of input. Returns (output, updated recommendations)."""
    parts = line.strip().split()
    if not parts:
        return "", last_recs
    cmd, args = parts[0].lower(), parts[1:]
    rest = " ".join(args)

    if cmd in ("help", "?"):
        return HELP, last_recs

    if cmd == "rec":
        n = 5
        pos = None
        for a in args:
            if a.isdigit():
                n = int(a)
            elif a.upper() in ("QB", "RB", "WR", "TE", "K", "DEF"):
                pos = a.upper()
        recs = engine.recommend(n, pos_filter=pos)
        out = [explain.pick_header(engine), ""]
        out += [explain.brief(r, i + 1) for i, r in enumerate(recs)]
        if recs:
            out += ["", explain.explain(engine, recs[0])]
        return "\n".join(out), recs

    if cmd in ("why", "explain"):
        if not rest:
            return "usage: why <player>", last_recs
        key = engine.resolve_key(rest)
        if not key:
            return f"no player matching {rest!r}", last_recs
        for r in last_recs:
            if r.key == key:
                return explain.explain(engine, r), last_recs
        recs = engine.recommend(30)
        for r in recs:
            if r.key == key:
                return explain.explain(engine, r), recs
        proj = engine.projection(key)
        if proj:
            avail = "available" if engine.is_available(key) else "ALREADY DRAFTED"
            return (f"{proj.name} ({proj.pos}{proj.pos_rank}) - {avail}. "
                    f"{proj.ppg:.1f} PPG, {proj.vor:+.1f} VOR, ADP "
                    f"{engine.adp.adp_of(proj.name, proj.pos):.0f}. Not in this "
                    f"pick's shortlist - the bot does not rate him here."), last_recs
        return f"no player matching {rest!r}", last_recs

    if cmd in ("compare", "cmp", "vs"):
        text = rest.replace(" vs ", "|").replace(" v ", "|")
        if "|" not in text:
            return "usage: compare <player a> vs <player b>", last_recs
        a, b = [x.strip() for x in text.split("|", 1)]
        recs = last_recs or engine.recommend(30)
        if not any(r.key == engine.resolve_key(a) for r in recs) or \
           not any(r.key == engine.resolve_key(b) for r in recs):
            recs = engine.recommend(40)
        return explain.compare(engine, recs, a, b), recs

    if cmd == "board":
        return explain.board_summary(engine), last_recs
    if cmd == "room":
        return explain.opponent_summary(engine), last_recs
    if cmd == "roster":
        return explain.roster_summary(engine), last_recs
    if cmd == "strategy":
        return explain.strategy_summary(engine), last_recs

    if cmd == "preset":
        if rest not in PRESETS:
            return f"presets: {', '.join(PRESETS)}", last_recs
        engine.strategy = PRESETS[rest]()
        engine.store.add_feedback(engine.state.draft_id, "preset",
                                  f"switched to {rest}")
        return f"strategy -> {rest}\n{explain.strategy_summary(engine)}", []

    if cmd == "set":
        if len(args) < 2:
            return "usage: set <field> <value>", last_recs
        field, raw = args[0], " ".join(args[1:])
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        log = engine.strategy.adjust(**{field: value})
        engine.store.add_feedback(engine.state.draft_id, "tune", "; ".join(log))
        return "\n".join(log), []

    if cmd == "bump":
        if len(args) < 2:
            return "usage: bump <player> <delta>", last_recs
        try:
            delta = float(args[-1])
        except ValueError:
            return "usage: bump <player> <delta>", last_recs
        key = engine.resolve_key(" ".join(args[:-1]))
        if not key:
            return f"no player matching {' '.join(args[:-1])!r}", last_recs
        engine.strategy.player_bumps[key] = delta
        engine.store.set_player_bias(key, delta, "manual bump")
        engine.biases = engine.store.player_biases()
        proj = engine.projection(key)
        return f"{proj.name if proj else key} adjusted {delta:+.1f}", []

    if cmd == "ban":
        key = engine.resolve_key(rest)
        if not key:
            return f"no player matching {rest!r}", last_recs
        engine.strategy.banned.append(key)
        return f"{rest} will not be recommended", []

    if cmd == "note":
        engine.store.add_feedback(engine.state.draft_id, "note", rest)
        return "noted for post-draft review", last_recs

    if cmd == "stats":
        hits = get_guide().stat_search(rest, limit=6)
        if not hits:
            return f"no guide stat mentions {rest!r}", last_recs
        return "\n".join(f"  #{h['n']}. {h['text']}" for h in hits), last_recs

    if cmd == "player":
        gp = get_guide().resolve(rest)
        if not gp:
            return f"no guide entry for {rest!r}", last_recs
        out = [f"{gp.name} ({gp.pos})"]
        for fmt in ("ppr", "half_ppr"):
            if gp.rank(fmt):
                out.append(f"  {fmt}: #{gp.rank(fmt)} overall, "
                           f"{gp.pos}{gp.prank(fmt)}")
        if gp.adj_ppg_2025 is not None:
            out.append(f"  2025 adjusted PPG: {gp.adj_ppg_2025} "
                       f"({gp.pos}{gp.adj_ppg_rank_2025})")
        if gp.is_rookie:
            out.append(f"  Dynasty rookie #{gp.rookie_rank} ({gp.rookie_team})")
        for n in gp.notes:
            out.append(explain._wrap(n, "  - "))
        return "\n".join(out), last_recs

    if cmd in ("quit", "exit", "q"):
        return "__QUIT__", last_recs

    return f"unknown command {cmd!r} - type help", last_recs


# ------------------------------------------------------------------ sessions


def run_session(engine: Engine, poll: float = 3.0, auto: bool = True) -> None:
    st = engine.state
    print(BANNER)
    print(f"Draft {st.draft_id}: {st.name}")
    print(f"  {st.teams} teams, {st.rounds} rounds, {st.draft_type}, "
          f"{st.scoring} scoring"
          + (", SUPERFLEX" if st.superflex else "")
          + (", dynasty" if st.is_dynasty else ""))
    print(f"  your slot: {st.my_slot or 'UNKNOWN - pass --slot'}")
    print(explain.strategy_summary(engine))
    print("\nType help for commands. Enter refreshes.\n")

    last_recs: list = []
    last_seen = len(st.picks)
    while True:
        try:
            if auto:
                events = engine.refresh()
                for e in events:
                    print("  " + e)
                if len(st.picks) != last_seen:
                    last_seen = len(st.picks)
                    last_recs = []
                    if st.is_my_turn:
                        print("\n>>> YOU ARE ON THE CLOCK <<<")
                        recs = engine.recommend(5)
                        print(explain.pick_header(engine))
                        for i, r in enumerate(recs, 1):
                            print(explain.brief(r, i))
                        if recs:
                            print()
                            print(explain.explain(engine, recs[0]))
                        last_recs = recs
                    else:
                        until = st.picks_until_my_turn()
                        if until is not None:
                            print(f"  ({until} picks until your turn)")

            line = input("\nffbot> ")
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return

        if not line.strip():
            continue
        out, last_recs = chat(engine, line, last_recs)
        if out == "__QUIT__":
            print("bye")
            return
        if out:
            print(out)


# ------------------------------------------------------------------ commands


def cmd_connect(args) -> int:
    draft_id = sleeper.extract_draft_id(args.draft)
    uid = None
    if args.user:
        uid = sleeper.user_id(args.user)
        if not uid:
            print(f"could not find Sleeper user {args.user!r}", file=sys.stderr)
    players = sleeper.load_players(refresh=args.refresh_players)
    state = sleeper.build_state(draft_id, my_user_id=uid, my_slot=args.slot,
                                players=players)
    if state.my_slot is None:
        print("Warning: your draft slot is unknown. Pass --slot N (1-based) or "
              "--user <sleeper-username>.", file=sys.stderr)
    strat = Strategy.load(args.strategy) if args.strategy else \
        for_league(state.teams, state.superflex, state.is_dynasty)
    engine = Engine(state, strategy=strat, players=players)
    run_session(engine, poll=args.poll)
    return 0


def cmd_mock(args) -> int:
    state = synthetic_state(teams=args.teams, rounds=args.rounds,
                            my_slot=args.slot, scoring=args.scoring,
                            superflex=args.superflex, dynasty=args.dynasty)
    strat = Strategy.load(args.strategy) if args.strategy else \
        for_league(state.teams, state.superflex, state.is_dynasty)
    engine = Engine(state, strategy=strat)
    if args.upto > 1:
        autopick(engine, args.upto)
        print(f"(simulated {len(state.picks)} picks)")
    run_session(engine, auto=False)
    return 0


def cmd_serve(args) -> int:
    """Run the local web panel instead of the terminal REPL."""
    # Imported here, not at module scope: server.py reuses chat() from this
    # module, so a top-level import in either direction would be circular.
    from . import server as panel

    srv = panel.serve(host=args.host, port=args.port,
                      public=args.public or None)
    try:
        if args.mock:
            blob = panel.start_offline_mock(
                teams=args.teams, rounds=args.rounds, slot=args.slot or 5,
                scoring=args.scoring, superflex=args.superflex,
                dynasty=args.dynasty, upto=args.upto,
                strategy=args.strategy or "")
            st = blob["state"]
            print(f"offline mock: {st['name']}, {st['picks_made']} picks in, "
                  f"your slot {st['my_slot']}")
        elif args.connect:
            blob = panel.connect_draft(args.connect, user=args.user or "",
                                       slot=args.slot or 0,
                                       strategy=args.strategy or "")
            st = blob["state"]
            print(f"connected: {st['name']} - {st['teams']} teams, "
                  f"{st['scoring']}, slot {st['my_slot'] or 'UNKNOWN'}, "
                  f"{st['picks_made']} picks in")
            if blob.get("warning"):
                print("  " + blob["warning"], file=sys.stderr)
    except (sleeper.SleeperError, ValueError) as e:
        # A failed pre-connect is not fatal: the panel can connect itself.
        print(f"could not pre-connect: {e}", file=sys.stderr)

    print(BANNER)
    print(f"  panel:  {srv.url}")
    if args.public:
        print("  PUBLIC multi-session mode: anonymous sessions via "
              "/api/connect; deep chat off unless FFBOT_ALLOW_PUBLIC_LLM=1.")
    else:
        print("  loopback only; the page is handed its API token server-side.")
    print("  advisory only - the bot never submits a pick.")
    print("  Ctrl-C to stop.\n")
    if not args.no_open:
        webbrowser.open(srv.url)
    srv.wait()
    print("bye")
    return 0


def cmd_drafts(args) -> int:
    uid = sleeper.user_id(args.user)
    if not uid:
        print(f"no such Sleeper user: {args.user}", file=sys.stderr)
        return 1
    drafts = sleeper.user_drafts(uid, args.season)
    if not drafts:
        print(f"no {args.season} drafts found for {args.user}. "
              "Mock drafts often are not listed - paste the draft URL instead.")
        return 0
    for d in drafts:
        s = d.get("settings") or {}
        m = d.get("metadata") or {}
        print(f"  {d['draft_id']}  {d.get('status','?'):<10s} "
              f"{s.get('teams','?')}tm {s.get('rounds','?')}rd "
              f"{m.get('scoring_type','?'):<9s} {m.get('name') or ''}")
    return 0


def cmd_review(args) -> int:
    store = Store()
    drafts = store.list_drafts()
    if not drafts:
        print("no drafts recorded yet")
        return 0
    draft_id = args.draft_id or drafts[0].draft_id
    rep = store.agreement_report(draft_id)
    print(f"Draft {draft_id}")
    print(f"  picks with recommendations: {rep['picks_with_recs']}")
    print(f"  you took the top suggestion: {rep['took_top_rec']} "
          f"({rep['top1_rate']:.0%})")
    print(f"  you took a top-3 suggestion: {rep['took_top3_rec']} "
          f"({rep['top3_rate']:.0%})")
    if rep["divergences"]:
        print("\n  Where you overruled the bot:")
        for d in rep["divergences"]:
            print(f"    pick {d['pick']}: you took {d['you_took']}, "
                  f"bot wanted {d['bot_wanted']}")
        print("\n  If the bot was wrong in a pattern, tune it, e.g.:")
        print("    ffbot strategy set rb_dead_zone_penalty 0.5")
        print("    ffbot strategy set wr_sweet_rounds '[3,4,5]'")
    fb = [f for f in store.list_feedback(20) if f["draft_id"] == draft_id]
    if fb:
        print("\n  Your notes:")
        for f in fb:
            print(f"    [{f['kind']}] {f['text']}")
    return 0


def cmd_strategy(args) -> int:
    if args.action == "list":
        for n in Strategy.available():
            print(" ", n)
        return 0
    name = args.name or "joel_rb_heavy"
    s = Strategy.load(name)
    if args.action == "show":
        print(json.dumps(s.to_dict(), indent=2))
        return 0
    if args.action == "set":
        if not args.field:
            print("usage: ffbot strategy set --name X <field> <value>",
                  file=sys.stderr)
            return 1
        try:
            value = json.loads(args.value)
        except (json.JSONDecodeError, TypeError):
            value = args.value
        for line in s.adjust(**{args.field: value}):
            print(" ", line)
        path = s.save(name)
        print(f"saved {path}")
        return 0
    return 1


def cmd_guide(args) -> int:
    g = get_guide()
    if args.term:
        hits = g.stat_search(args.term, limit=10)
        for h in hits:
            print(f"  #{h['n']}. {h['text']}")
        gp = g.resolve(args.term)
        if gp:
            print(f"\n{gp.name} ({gp.pos}) - PPR #{gp.rank('ppr')} "
                  f"({gp.pos}{gp.prank('ppr')}), 2025 adj PPG {gp.adj_ppg_2025}")
        return 0
    print(f"{g.raw['title']} (updated {g.raw['last_update']})")
    print(f"  {len(g.players)} ranked players, {len(g.stats)} stats")
    print("\n" + json.dumps(g.strategy["positional"], indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ffbot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("connect", help="attach to a live or mock Sleeper draft")
    c.add_argument("draft", help="draft id or sleeper.com draft URL")
    c.add_argument("--user", help="your Sleeper username (to find your slot)")
    c.add_argument("--slot", type=int, help="your draft slot, 1-based")
    c.add_argument("--strategy", help="strategy name (default: auto)")
    c.add_argument("--poll", type=float, default=3.0)
    c.add_argument("--refresh-players", action="store_true")
    c.set_defaults(func=cmd_connect)

    m = sub.add_parser("mock", help="offline rehearsal, no Sleeper needed")
    m.add_argument("--teams", type=int, default=12)
    m.add_argument("--rounds", type=int, default=15)
    m.add_argument("--slot", type=int, default=5)
    m.add_argument("--scoring", default="ppr", choices=["ppr", "half_ppr", "std"])
    m.add_argument("--superflex", action="store_true")
    m.add_argument("--dynasty", action="store_true")
    m.add_argument("--strategy")
    m.add_argument("--upto", type=int, default=1,
                   help="auto-simulate up to this overall pick first")
    m.set_defaults(func=cmd_mock)

    v = sub.add_parser("serve", help="local web panel in your browser")
    v.add_argument("--port", type=int, default=8770)
    v.add_argument("--host", default="127.0.0.1",
                   help="bind address; anything but loopback needs --public")
    v.add_argument("--public", action="store_true",
                   help="anonymous multi-session mode, for hosting")
    v.add_argument("--no-open", action="store_true",
                   help="do not open a browser window")
    v.add_argument("--connect", metavar="DRAFT",
                   help="pre-connect this draft id or sleeper.com URL")
    v.add_argument("--user", help="your Sleeper username (to find your slot)")
    v.add_argument("--slot", type=int, help="your draft slot, 1-based")
    v.add_argument("--mock", action="store_true",
                   help="start an offline rehearsal instead of connecting")
    v.add_argument("--teams", type=int, default=12)
    v.add_argument("--rounds", type=int, default=15)
    v.add_argument("--scoring", default="ppr", choices=["ppr", "half_ppr", "std"])
    v.add_argument("--superflex", action="store_true")
    v.add_argument("--dynasty", action="store_true")
    v.add_argument("--upto", type=int, default=1,
                   help="auto-simulate a mock up to this overall pick first")
    v.add_argument("--strategy", help="strategy name (default: auto)")
    v.set_defaults(func=cmd_serve)

    d = sub.add_parser("drafts", help="list your Sleeper drafts")
    d.add_argument("--user", required=True)
    d.add_argument("--season", default="2026")
    d.set_defaults(func=cmd_drafts)

    r = sub.add_parser("review", help="post-draft agreement report")
    r.add_argument("draft_id", nargs="?")
    r.set_defaults(func=cmd_review)

    s = sub.add_parser("strategy", help="inspect or tune strategies")
    s.add_argument("action", choices=["list", "show", "set"])
    s.add_argument("--name")
    s.add_argument("field", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_strategy)

    g = sub.add_parser("guide", help="query the draft guide")
    g.add_argument("term", nargs="?")
    g.set_defaults(func=cmd_guide)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except sleeper.SleeperError as e:
        print(f"Sleeper error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
