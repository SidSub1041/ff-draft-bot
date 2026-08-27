# ff-draft-bot

A local fantasy football draft assistant trained on **Joel Smyth's 2026 Draft
Guide** and wired into the **Sleeper API**. It watches your live or mock draft,
reacts to every pick, and tells you who to take and why. It **never makes a
pick for you** — it advises, and you argue with it.

Works in any league shape: 8-team redraft, 12-team PPR, 16-team half-PPR,
superflex, dynasty. Scoring and roster settings are read from Sleeper.

---

## Quick start

```bash
cd ~/ff-draft-bot && python3 -m venv .venv && .venv/bin/pip install -e ".[plugin,dev]"
```

Rehearse offline with no Sleeper room at all:

```bash
.venv/bin/ffbot mock --teams 12 --slot 5 --upto 53
```

Attach to a real draft (live or a Sleeper mock — paste the URL from the browser):

```bash
.venv/bin/ffbot connect https://sleeper.com/draft/nfl/1234567890 --user YOUR_SLEEPER_NAME
```

## Use it as a Claude Code plugin

`.mcp.json` in this directory registers an MCP server exposing 18 tools, so you
can just talk to Claude during your draft instead of learning a CLI:

> *"Connect to my draft at sleeper.com/draft/nfl/123456, I'm sidharth"*
> *"Who should I take?"* · *"Why not Bhayshul Tuten?"* · *"Compare Tuten and Hampton"*
> *"What's slot 8 doing?"* · *"I want to go zero-RB from here"*

Claude keeps the draft session open across the whole conversation, so it always
has your roster, the board, and the room read in context.

---

## What it learned from the guide

`scripts/parse_guide.py` turns the PDF into `data/guide_2026.json`. The
extraction was cross-checked: the big boards and the positional lists agree with
each other on 7 of 8 lists with zero mismatches.

| Extracted | Detail |
|---|---|
| Big boards | PPR + half-PPR, 150 players each |
| Positional ranks | 60 RB, 60 WR, 32 QB, 32 TE per format |
| 2025 Adjusted PPG | 46 RB, 48 WR, 32 QB, 28 TE — the author's injury/snap/situation-adjusted numbers |
| Top 50 stats | All 50, auto-tagged to the players they mention |
| Dynasty rookies | 36 |
| Strategy | Page 11 round-by-round script, positional targets/fades, six rules |

**Not extracted:** the DST and kicker pages (9–10), OL rankings (14), playcaller
table (15), research charts (16–18) and player cards (22–25) are images with no
text layer. The bot therefore has **no DST or kicker rankings of its own** and
says so — it treats defences as interchangeable and tells you to check page 9.

---

## The model

Four fitted components, all from data actually in the guide or observed on
Sleeper. Nothing is a black box; every number surfaces in the reasoning.

**1. Value curves.** Fit `ppg(rank) = A·(rank + r₀)^−b + C` to the guide's 2025
Adjusted PPG tables, per position. The rank offset `r₀` matters: real value
curves are flat across the top few players, and an unshifted power law predicts
37 PPG for RB1 against an observed 24.8.

```
RB  r² = 0.983      WR  r² = 0.981
TE  r² = 0.935      QB  r² = 0.847
```

**2. Projections.** A player's 2026 guide positional rank goes through that
curve, then shrinks 28% toward their own 2025 adjusted PPG. Uncertainty is
`sigma = floor + slope · ppg`, widened for rookies, for players with no 2025
baseline, and where the ranker and the player's own 2025 disagree.

**3. ADP, independent of the guide.** Rule 1 is "don't draft off rankings
without understanding ADP", so the market baseline must not be derived from
Joel's opinions. Sleeper's `search_rank` gives genuine *within-position* market
ordering; that rank is pushed through positional consumption curves anchored to
landmarks the guide states outright. It reproduces the guide's own examples:

| | guide says | model says |
|---|---|---|
| QB3 overall pick | 55th | 55.0 |
| Parker Washington | "taken ~85th, I have him 62nd" | ADP 84, guide #62 |

**4. Monte Carlo + VONA.** 400 simulated futures per pick, using an opponent
model that tracks each rival's roster needs, whether they reach or wait for
value, positional lean, and any run in progress. That yields survival
probabilities and the expected board at your next pick.

The final score:

```
score = value now (VOR, roster-aware, + strategy terms)
      + 0.85 × expected board at your next pick
      + urgency: P(gone) × (his value − your expected fallback)
```

The urgency term exists because a two-pick lookahead can't see that taking the
player who *will* be gone lets you collect the one who *won't* three picks
later. Without it the bot took a 60%-to-survive player over a 0%-to-survive one
of equal value.

---

## Joel's strategy, as knobs

The default is his RB-heavy script — RB/RB, three RBs from the top ~30, WR in
rounds 3 and 5, QB in the QB7–11 band, TE only at true best-available, no K/DST
until the last two rounds. Every rule is a *tunable field*, not hard-coded
behaviour, so you can overrule it after a mock:

```
rb_target_top_n 30      rb_dead_zone (30,40)     wr_sweet_rounds (3,5)
rb_target_count 3       rb_dead_zone_penalty     wr_fade_pos_rank (5,12)
qb_target_pos_rank      qb_min_round 7           te2_penalty
bench_targets           max_pos                  urgency_weight
reach_penalty_per_pick  value_bonus_per_pick     upside_weight_late
```

Presets: `joel_rb_heavy` (default), `zero_rb`, `hero_rb`, `bpa`, `superflex`,
`dynasty`. Superflex and dynasty leagues auto-select their preset; 8-team and
16-team leagues get their scarcity weights rescaled.

```bash
ffbot strategy set --name joel_rb_heavy rb_target_count 2
ffbot strategy set --name joel_rb_heavy wr_sweet_rounds '[3,4,5]'
```

Or in chat: *"stop fading the RB dead zone so hard"* → Claude calls
`set_strategy`, and the change persists.

---

## Getting better after each mock

Every draft it watches is recorded to `data/ffbot.db`:

- **Learned ADP.** Each draft contributes (position, positional rank when
  taken, pick number) observations that refit the consumption curves, shrunk
  toward the prior until there's a real sample. After ~8 drafts of a given
  shape the ADP is yours, not a default.
- **Agreement tracking.** `ffbot review` shows every pick where you overruled
  the bot, so patterns are visible rather than anecdotal.
- **Player bumps.** `bump tuten 1.5` permanently nudges a player; persists
  across drafts.
- **Feedback log.** `note <text>` during a draft, reviewed afterwards.

```bash
ffbot review              # where you and the bot disagreed, plus your notes
```

---

## CLI reference

```
ffbot connect <url|id> [--user NAME] [--slot N] [--strategy NAME]
ffbot mock [--teams 12] [--rounds 15] [--slot 5] [--scoring ppr]
           [--superflex] [--dynasty] [--upto N]
ffbot drafts --user NAME [--season 2026]
ffbot review [draft_id]
ffbot strategy list|show|set [--name NAME] FIELD VALUE
ffbot guide [term]
```

In a session: `rec`, `why <player>`, `compare <a> vs <b>`, `board`, `room`,
`roster`, `strategy`, `set`, `preset`, `bump`, `ban`, `note`, `stats`, `player`.

## Bringing your own ADP

Drop a CSV in `data/adp/` with `name` and `adp` columns (a FantasyPros or
Sleeper export works). Imported ADP overrides both the learned and prior layers.

## Layout

```
ffbot/
  guide.py       parsed guide: rankings, adjusted PPG, stats, strategy
  names.py       name normalisation + fuzzy resolution across sources
  sleeper.py     read-only Sleeper client, draft state, snake/reversal ordering
  adp.py         layered ADP: learned -> imported -> prior
  curves.py      power-law and log-log fitting (pure stdlib)
  model.py       projections, sigma, replacement level, tiers
  opponents.py   per-rival tendencies, needs, run detection
  simulate.py    Monte Carlo survival + VONA lookahead
  strategy.py    Joel's rules as tunable knobs, plus presets
  engine.py      scoring and recommendations
  explain.py     deterministic reasoning prose
  cli.py         terminal session
  mcp_server.py  Claude Code plugin (18 tools)
  mockdraft.py   offline rehearsal / test harness
```

## Notes and limits

- The bot is **advisory only** and read-only against Sleeper. It cannot and will
  not submit a pick.
- Sleeper's `search_rank` is a superflex-blended autocomplete rank with heavy
  ties and some stale entries; it's used only for *within-position* ordering,
  and retired/unsigned players are filtered out.
- The guide's per-player "reason" annotations in the Adjusted PPG tables
  (e.g. "in complete games") sit in a separate PDF column with no recoverable
  row alignment, so they're kept as page-level context, not per-player.
- Prior ADP curves are anchored to landmarks the guide states, but they are
  priors. Run a few mocks and they get replaced by what your leagues actually do.

```bash
.venv/bin/python -m pytest tests -q
```
