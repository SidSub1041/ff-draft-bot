"""The recommendation engine - where guide, market, roster and rivals meet.

Score for a candidate at your pick:

    score = marginal_value(player | your roster)      # VOR after diminishing returns
          + upside_term(round)                        # "don't beat ADP" -> chase ceiling late
          + adp_term(value or reach vs market)        # rules 1 and 2
          + strategy_terms(...)                       # the page-11 script, as knobs
          + risk/scarcity/handcuff adjustments
          + lookahead_discount * E[best available at your next pick | you take this]

The last term is the one that stops you from taking a player who will still be
there in 20 picks, and it comes from the Monte Carlo in simulate.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import sleeper
from .adp import AdpModel
from .guide import Guide, get_guide
from .model import BASE_SIGMA, Projection, ValueModel
from .names import normalize
from .opponents import OpponentModel
from .simulate import Candidate, DraftSimulator, SimResult
from .sleeper import DraftState
from .store import Store
from .strategy import Strategy, for_league

FLEX_POS = ("RB", "WR", "TE")


@dataclass
class Reason:
    kind: str          # value | scarcity | strategy | risk | roster | guide | market
    weight: float      # contribution to the score (0 for pure colour)
    text: str


@dataclass
class Recommendation:
    key: str
    name: str
    pos: str
    team: str | None
    score: float
    value_now: float
    lookahead: float
    proj: Projection
    adp: float
    adp_delta: float           # positive = falling to you
    survival: float            # P(still here at your next pick)
    tier: tuple[int, int] | None
    reasons: list[Reason] = field(default_factory=list)
    off_guide: bool = False
    injury: str | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "pos": self.pos, "team": self.team,
            "score": round(self.score, 3), "value_now": round(self.value_now, 3),
            "lookahead": round(self.lookahead, 3),
            "ppg": round(self.proj.ppg, 2), "vor": round(self.proj.vor, 2),
            "upside": round(self.proj.upside, 2), "floor": round(self.proj.floor, 2),
            "sigma": round(self.proj.sigma, 2),
            "guide_overall_rank": self.proj.overall_rank,
            "guide_pos_rank": self.proj.pos_rank,
            "adp": round(self.adp, 1), "adp_delta": round(self.adp_delta, 1),
            "survival_to_next_pick": round(self.survival, 3),
            "tier": list(self.tier) if self.tier else None,
            "off_guide": self.off_guide,
            "injury": self.injury,
            "reasons": [{"kind": r.kind, "weight": round(r.weight, 2), "text": r.text}
                        for r in self.reasons],
        }


class Engine:
    def __init__(self, state: DraftState, strategy: Strategy | None = None,
                 guide: Guide | None = None, store: Store | None = None,
                 players: dict[str, dict] | None = None) -> None:
        self.state = state
        self.guide = guide or get_guide()
        self.store = store or Store()
        self.players = players if players is not None else sleeper.load_players()
        self.fmt = state.fmt

        self.strategy = strategy or for_league(
            state.teams, state.superflex, state.is_dynasty)

        learned, n_drafts = self.store.learned_anchors(state.superflex)
        self.learned_draft_count = n_drafts
        self.adp = AdpModel(teams=state.teams, superflex=state.superflex,
                            te_premium=False, learned_anchors=learned)
        self.adp.build_from_sleeper(self.players)
        self.adp.load_dir()

        self.model = ValueModel(self.guide, self.fmt)
        self.replacement = self.model.apply_replacement(
            state.teams, state.starters, state.roster_slots.get("BN", 0))

        self.opponents = OpponentModel(state.teams, state.starters)
        self.sim = DraftSimulator(self.opponents)

        self._sleeper_meta: dict[str, dict] = {}
        self._off_guide: dict[str, Projection] = {}
        self._build_off_guide_pool()

        self.biases = self.store.player_biases()
        self.last_sim: SimResult | None = None
        self._seen_picks: set[int] = set()
        self.refresh(fetch=False)

    # ------------------------------------------------------------ universe

    def _build_off_guide_pool(self) -> None:
        """Deep leagues run past the guide's 219 players; back-fill from market.

        These get a projection from the value curve at their market positional
        rank and a discount, since the guide has not endorsed them.
        """
        for entry in self.adp.entries.values():
            if entry.key in self.model.projections:
                continue
            if entry.pos in ("K", "DEF"):
                proj = self._kdst_projection(entry)
                if entry.pos == "DEF":
                    proj.notes.append(self.DST_CAVEAT)
                self._off_guide[entry.key] = proj
                continue
            if entry.pos not in ("QB", "RB", "WR", "TE"):
                continue
            ppg = self.model.curve_ppg(entry.pos, entry.market_pos_rank) * 0.94
            # Wider than a ranked player, but anchored to the position's real
            # weekly spread rather than to the top of the curve.
            sigma = BASE_SIGMA.get(entry.pos, 6.0) * 1.35
            proj = Projection(
                key=entry.key, name=entry.name, pos=entry.pos,
                pos_rank=entry.market_pos_rank, overall_rank=None,
                ppg=ppg, curve_ppg=ppg, hist_ppg=None, sigma=sigma,
                replacement=self.replacement.get(entry.pos, 0.0),
                vor=ppg - self.replacement.get(entry.pos, 0.0),
                upside=ppg + 1.04 * sigma, floor=max(0.0, ppg - 1.04 * sigma),
            )
            self._off_guide[entry.key] = proj

        for pid, p in self.players.items():
            name = p.get("full_name") or ""
            if not name:
                continue
            key = normalize(name)
            if key and key not in self._sleeper_meta:
                self._sleeper_meta[key] = p

    # Kickers and defences are not in the guide (its K/DST pages are graphs).
    # They get a flat, shallow value curve - which is the point: the spread is
    # tiny, which is exactly why the guide says don't spend a pick until the end.
    KDST_CURVE = {"K": (9.0, 0.09), "DEF": (8.5, 0.12)}

    DST_CAVEAT = (
        "The guide's DST and kicker pages (9-10) are charts with no text layer, "
        "so the bot has no DST ranking of its own and treats defences as "
        "interchangeable - check page 9 of the PDF and take the matchup you like."
    )

    def _kdst_projection(self, entry) -> Projection:
        base, slope = self.KDST_CURVE[entry.pos]
        ppg = max(3.0, base - slope * entry.market_pos_rank)
        repl = max(3.0, base - slope * self.state.teams)
        return Projection(
            key=entry.key, name=entry.name, pos=entry.pos,
            pos_rank=entry.market_pos_rank, overall_rank=None, ppg=ppg,
            curve_ppg=ppg, hist_ppg=None, sigma=3.2, replacement=repl,
            vor=ppg - repl, upside=ppg + 3.3, floor=max(0.0, ppg - 3.3),
        )

    def injury_of(self, key: str) -> tuple[str, str] | None:
        """(status, detail) from Sleeper's live data, or None when healthy.

        This is the field the panel was ignoring while it recommended a
        player who reports said was out for two months."""
        meta = self._sleeper_meta.get(key) or {}
        status = (meta.get("injury_status") or "").strip()
        if not status:
            return None
        bits = [b for b in (meta.get("injury_body_part"),
                            meta.get("injury_notes")) if b and b != "None"]
        return status, ", ".join(str(b) for b in bits)

    def projection(self, key: str) -> Projection | None:
        return self.model.projections.get(key) or self._off_guide.get(key)

    def resolve_key(self, name: str, pos: str | None = None) -> str | None:
        gp = self.guide.resolve(name, pos)
        if gp:
            return gp.key
        e = self.adp.get(name, pos)
        if e:
            return e.key
        k = normalize(name)
        return k if k in self._sleeper_meta else None

    def _pos_rank_of(self, name: str, pos: str) -> tuple[str | None, int | None]:
        key = self.resolve_key(name, pos)
        proj = self.projection(key) if key else None
        return key, (proj.pos_rank if proj else None)

    # ------------------------------------------------------------- refresh

    def refresh(self, fetch: bool = True) -> list[str]:
        """Pull new picks and return human-readable events for them.

        Seen picks are tracked on the engine rather than diffed against
        `state.picks`: offline drivers append to that list before calling in,
        so a local diff would always come back empty.
        """
        if fetch:
            if getattr(self.state, "provider", None) is not None:
                self.state.provider.refresh(self.state)
            else:
                sleeper.refresh_picks(self.state, self.players)
        events = []
        for p in self.state.picks:
            if p.pick_no in self._seen_picks:
                continue
            self._seen_picks.add(p.pick_no)
            events.append(self._describe_pick(p))
            # Log what you actually took against what was recommended, so
            # `review` can show where you and the bot diverged.
            if p.slot == self.state.my_slot:
                self.store.record_choice(
                    self.state.draft_id, p.pick_no,
                    self.resolve_key(p.name, p.pos) or "", p.name)
        self.opponents.observe(
            self.state.picks,
            lambda n, ps: self.adp.adp_of(n, ps, fallback=self.adp.undrafted_prior(ps)))
        self.store.upsert_draft(self.state, self.strategy.name)
        self.store.record_picks(self.state, self._pos_rank_of)
        return events

    def _describe_pick(self, p) -> str:
        adp = self.adp.adp_of(p.name, p.pos, fallback=999)
        delta = adp - p.pick_no
        tag = ""
        if adp < 900:
            if delta > 12:
                tag = f" (reach, ADP {adp:.0f})"
            elif delta < -12:
                tag = f" (value, ADP {adp:.0f})"
        who = "YOU" if p.slot == self.state.my_slot else f"slot {p.slot}"
        return (f"{p.pick_no:>3}.{p.round:02d} {who}: {p.pos} {p.name}"
                f"{f' ({p.team})' if p.team else ''}{tag}")

    # ------------------------------------------------------------ my roster

    def my_counts(self) -> dict[str, int]:
        return self.state.pos_counts(self.state.my_slot) if self.state.my_slot else \
            dict.fromkeys(("QB", "RB", "WR", "TE", "K", "DEF"), 0)

    def my_rb_top_n(self, n: int | None = None) -> int:
        """How many RBs I already own inside the guide's top-N."""
        n = n or self.strategy.rb_target_top_n
        count = 0
        for p in self.state.my_roster():
            if p.pos != "RB":
                continue
            key = self.resolve_key(p.name, "RB")
            proj = self.projection(key) if key else None
            if proj and proj.pos_rank and proj.pos_rank <= n:
                count += 1
        return count

    def my_high_variance(self) -> int:
        c = 0
        for p in self.state.my_roster():
            key = self.resolve_key(p.name, p.pos)
            proj = self.projection(key) if key else None
            if proj and proj.sigma >= self.strategy.high_variance_sigma:
                c += 1
        return c

    # -------------------------------------------------------- availability

    def available(self) -> list[Projection]:
        taken: set[str] = set()
        for p in self.state.picks:
            k = self.resolve_key(p.name, p.pos)
            if k:
                taken.add(k)
        pool = []
        for src in (self.model.projections, self._off_guide):
            for key, proj in src.items():
                if key in taken:
                    continue
                if key in self.strategy.banned:
                    continue
                pool.append(proj)
        pool.sort(key=lambda p: self.adp.adp_of(p.name, p.pos, 999))
        return pool

    # --------------------------------------------------------- valuation

    def marginal_value(self, proj: Projection, counts: dict[str, int]) -> float:
        """VOR after roster-construction diminishing returns."""
        s = self.state.starters
        have = counts.get(proj.pos, 0)
        need = s.get(proj.pos, 0)
        flex = s.get("FLEX", 0) + (s.get("SUPER_FLEX", 0) if proj.pos != "K" else 0)

        vor = proj.vor
        if have < need:
            mult = 1.0
        elif proj.pos in FLEX_POS and have < need + flex:
            mult = 0.92
        else:
            extra = have - need - (flex if proj.pos in FLEX_POS else 0)
            mult = self.strategy.bench_discount * \
                (self.strategy.bench_decay ** max(0, extra))
        # A negative-VOR player is not made better by needing the slot.
        return vor * mult if vor > 0 else vor

    def _value_map(self, counts: dict[str, int],
                   pool: list[Projection]) -> dict[str, float]:
        return {p.key: self.marginal_value(p, counts) for p in pool}

    def score_player(self, proj: Projection, rnd: int, counts: dict[str, int],
                     pick_no: int, include_script: bool = True
                     ) -> tuple[float, list[Reason]]:
        """Everything except the lookahead: what this player is worth right now.

        `include_script=False` drops the round-by-round positional script.  The
        lookahead uses that variant deliberately: the script says what to do at
        *this* pick, and letting it swing between adjacent rounds makes the same
        player appear to lose several points of value one round later, which
        drowns out the real signal (who will actually still be on the board).
        """
        adp = self.adp.adp_of(proj.name, proj.pos,
                              self.adp.undrafted_prior(proj.pos))
        reasons: list[Reason] = []

        value_now = self.marginal_value(proj, counts)
        reasons.append(Reason(
            "value", value_now,
            f"{proj.ppg:.1f} projected PPG, {proj.vor:+.1f} over "
            f"{proj.pos} replacement ({proj.replacement:.1f})"))

        uw = self.strategy.upside_weight(rnd)
        up = uw * (proj.upside - proj.ppg)
        if up > 0.05:
            reasons.append(Reason(
                "value", up,
                f"Ceiling {proj.upside:.1f} PPG - round {rnd} weights upside "
                f"at {uw:.2f}"))

        reasons += self._market_terms(proj, adp, pick_no)
        reasons += self._strategy_terms(proj, rnd, counts, adp, pick_no,
                                        include_script=include_script)

        tier = self.model.tier_of(proj.key)
        if tier and tier[1] == 1 and proj.vor > 0:
            reasons.append(Reason("scarcity", 0.8,
                                  f"Last player in {proj.pos} tier {tier[0]}"))

        return sum(r.weight for r in reasons), reasons

    def _score_map(self, pool: list[Projection], rnd: int,
                   counts: dict[str, int], pick_no: int,
                   include_script: bool = True) -> dict[str, float]:
        return {p.key: self.score_player(p, rnd, counts, pick_no,
                                         include_script)[0]
                for p in pool}

    # ------------------------------------------------------ strategy terms

    def _strategy_terms(self, proj: Projection, rnd: int, counts: dict[str, int],
                        adp: float, pick_no: int,
                        include_script: bool = True) -> list[Reason]:
        st = self.strategy
        out: list[Reason] = []
        pos, prank = proj.pos, proj.pos_rank or 999

        w = st.pos_weights.get(pos, 1.0)
        if abs(w - 1.0) > 0.001 and proj.vor > 0:
            out.append(Reason("strategy", proj.vor * (w - 1.0),
                              f"{st.name} weights {pos} at {w:.2f}x"))

        target = st.target_for_round(rnd)
        if target == pos and include_script:
            out.append(Reason("strategy", st.round_target_bonus,
                              f"Round {rnd} is a {target} round in the "
                              f"{st.name} script"))

        # --- RB rules
        if pos == "RB":
            if st.rb_target_count:
                have_top = self.my_rb_top_n()
                if have_top < st.rb_target_count and prank <= st.rb_target_top_n:
                    out.append(Reason(
                        "strategy", st.rb_target_bonus,
                        f"Guide wants {st.rb_target_count} RBs from the top "
                        f"{st.rb_target_top_n}; you have {have_top}"))
            lo, hi = st.rb_dead_zone
            if lo <= prank <= hi and st.rb_dead_zone_penalty:
                out.append(Reason("strategy", -st.rb_dead_zone_penalty,
                                  f"RB{prank} sits in the guide's RB{lo}-{hi} dead "
                                  f"zone ('mostly a waste of time' vs QB/WR/TE here)"))
            if rnd <= st.rb_early_rounds and st.rb_early_bonus and prank <= 12:
                out.append(Reason("strategy", st.rb_early_bonus,
                                  "Elite league winners come from Rounds 1-2 90% "
                                  "of the time"))

        # --- WR rules
        if pos == "WR":
            if rnd in st.wr_sweet_rounds and st.wr_sweet_bonus and include_script:
                out.append(Reason("strategy", st.wr_sweet_bonus,
                                  f"Round {rnd} is a guide WR sweet spot"))
            lo, hi = st.wr_fade_pos_rank
            if lo <= prank <= hi and st.wr_fade_penalty:
                out.append(Reason("strategy", -st.wr_fade_penalty,
                                  f"WR{prank} is in the WR{lo}-{hi} band the guide "
                                  f"likes least"))
            # Late-WR upside is the guide's preference, but only while the WR
            # room is still a reasonable size.
            if (rnd >= st.upside_ramp_start_round and st.wr_late_upside_bonus
                    and counts.get("WR", 0) < st.bench_targets.get("WR", 5)):
                out.append(Reason("strategy", st.wr_late_upside_bonus,
                                  "WR is the guide's preferred position for late "
                                  "upside swings"))

        # --- QB rules
        if pos == "QB":
            lo, hi = st.qb_target_pos_rank
            if rnd < st.qb_min_round and not self.state.superflex:
                out.append(Reason("strategy", -st.qb_early_penalty,
                                  f"Round {rnd} is early for QB - the guide waits "
                                  f"for the QB{lo}-{hi} band"))
            if lo <= prank <= hi:
                out.append(Reason("strategy", st.qb_target_bonus,
                                  f"QB{prank} is inside the guide's main QB{lo}-{hi} "
                                  f"target band"))
            if prank < lo and self.fall(adp, pick_no) >= st.qb_faller_picks:
                out.append(Reason("strategy", st.qb_faller_bonus,
                                  f"QB{prank} has fallen "
                                  f"{self.fall(adp, pick_no):.0f} picks past ADP "
                                  f"- the guide takes big fallers"))
            if counts.get("QB", 0) >= (1 + (1 if self.state.superflex else 0)):
                out.append(Reason("strategy", -1.6, "QB slot already filled"))

        # --- TE rules
        if pos == "TE":
            if prank <= st.te_elite_pos_rank and st.te_elite_bonus:
                out.append(Reason("strategy", st.te_elite_bonus,
                                  f"TE{prank} is in the elite TE1-{st.te_elite_pos_rank} "
                                  f"group the guide will take on value"))
            if rnd in st.te_mid_rounds and st.te_mid_bonus and include_script:
                out.append(Reason("strategy", st.te_mid_bonus,
                                  "Guide grabs one of the last mid-round TEs in Rd 7/8"))
            if counts.get("TE", 0) >= max(1, self.state.starters.get("TE", 1)):
                out.append(Reason("strategy", -st.te2_penalty,
                                  "Guide would rather hunt backup RBs than roster a TE2"))

        # --- K / DEF
        if pos in ("K", "DEF"):
            if rnd < self.state.rounds - st.kdst_last_n_rounds + 1:
                out.append(Reason("strategy", -st.kdst_penalty,
                                  f"No K or D/ST until the last "
                                  f"{st.kdst_last_n_rounds} rounds"))

        # --- injuries: Sleeper's live status, weighted by strategy
        inj = self.injury_of(proj.key)
        if inj:
            status, detail = inj
            pen = st.injury_penalties.get(status, 0.0)
            label = f"{status}" + (f" ({detail})" if detail else "")
            if pen >= 1.0:
                out.append(Reason("risk", -pen,
                                  f"Injury: {label} - check the latest report "
                                  f"before spending this pick"))
            elif pen > 0:
                out.append(Reason("risk", -pen, f"Injury note: {label}"))

        # --- rookies: the guide's late process players
        if proj.is_rookie and pos in ("RB", "WR", "TE"):
            gp = self.guide.players.get(proj.key)
            rr = gp.rookie_rank if gp else None
            if (rnd >= st.rookie_bonus_start_round
                    and st.rookie_upside_bonus):
                out.append(Reason(
                    "strategy", st.rookie_upside_bonus,
                    f"Rookie upside swing - the guide's rule 4 names rookie "
                    f"{pos}s as the late-round process players"))
            if (rr is not None and rr <= st.rookie_capital_rank
                    and st.rookie_capital_bonus and rnd >= 4):
                out.append(Reason(
                    "strategy", st.rookie_capital_bonus,
                    f"Dynasty rookie #{rr} - top draft capital, the profile "
                    f"guide stat #1 says hits by year two"))

        # --- mandatory starting slots must actually get filled
        need_here = self.state.starters.get(pos, 0) - counts.get(pos, 0)
        if need_here > 0:
            picks_left = len([p for p in self.state.my_upcoming_picks(99)
                              if p >= pick_no])
            unfilled = sum(max(0, self.state.starters.get(q, 0) - counts.get(q, 0))
                           for q in ("QB", "RB", "WR", "TE", "K", "DEF"))
            if picks_left <= unfilled:
                out.append(Reason(
                    "roster", 12.0,
                    f"{picks_left} picks left and {unfilled} starting slots "
                    f"unfilled - you have to take a {pos}"))
            elif picks_left <= unfilled + 1:
                out.append(Reason("roster", 3.0,
                                  f"Running out of room to fill your {pos} slot"))

        # --- roster shape: caps and end-of-draft balance
        cap = st.max_pos.get(pos)
        if cap is not None and counts.get(pos, 0) >= cap:
            out.append(Reason("roster", -st.over_cap_penalty,
                              f"You already have {counts.get(pos, 0)} {pos}s - "
                              f"past the {cap} this roster can use"))
        if rnd >= st.bench_balance_start_round and st.bench_balance_bonus:
            want = st.bench_targets.get(pos, 0)
            short = want - counts.get(pos, 0)
            if short > 0:
                out.append(Reason(
                    "roster", st.bench_balance_bonus * min(2.0, short),
                    f"You have {counts.get(pos, 0)} {pos}s and want about "
                    f"{want} by the end of the draft"))

        # --- risk balance
        if proj.sigma >= st.high_variance_sigma:
            hv = self.my_high_variance()
            if hv >= st.max_high_variance:
                out.append(Reason("risk", -st.risk_penalty,
                                  f"You already roster {hv} high-variance players "
                                  f"- the guide says balance risk"))

        # --- handcuff
        if pos == "RB" and st.handcuff_bonus:
            my_teams = {p.team for p in self.state.my_roster() if p.pos == "RB"}
            meta = self._sleeper_meta.get(proj.key)
            if meta and meta.get("team") in my_teams and meta.get("team"):
                out.append(Reason("roster", st.handcuff_bonus,
                                  f"Handcuff to your {meta.get('team')} RB"))

        # --- dynasty age
        if st.dynasty_age_weight:
            meta = self._sleeper_meta.get(proj.key)
            age = (meta or {}).get("age")
            if age:
                delta = (26.5 - float(age)) * 0.28 * st.dynasty_age_weight
                out.append(Reason("strategy", delta,
                                  f"Dynasty: age {age:.0f}"))

        # --- explicit user bumps
        bump = self.strategy.player_bumps.get(proj.key, 0.0) + self.biases.get(proj.key, 0.0)
        if bump:
            out.append(Reason("strategy", bump, f"Your own adjustment ({bump:+.1f})"))

        return out

    @staticmethod
    def fall(adp: float, pick_no: int) -> float:
        """Picks a player has slid past his ADP. Positive = value, negative = reach.

        A player with ADP 20 still on the board at pick 40 has fallen 20 picks.
        A player with ADP 149 taken at pick 53 is a 96-pick reach.
        """
        return pick_no - adp

    def _market_terms(self, proj: Projection, adp: float, pick_no: int
                      ) -> list[Reason]:
        st = self.strategy
        out = []
        if adp >= 900:
            return out
        fall = self.fall(adp, pick_no)
        if fall > st.reach_free_picks:
            bonus = min(st.value_bonus_cap,
                        (fall - st.reach_free_picks) * st.value_bonus_per_pick)
            out.append(Reason("value", bonus,
                              f"Fell {fall:.0f} picks past ADP {adp:.0f}"))
        elif fall < -st.reach_free_picks:
            pen = (abs(fall) - st.reach_free_picks) * st.reach_penalty_per_pick
            out.append(Reason("value", -pen,
                              f"Reaching {abs(fall):.0f} picks ahead of ADP "
                              f"{adp:.0f}"))
        return out

    # ------------------------------------------------------- recommendation

    def recommend(self, n: int = 5, pick_no: int | None = None,
                  pos_filter: str | None = None,
                  sim_runs: int | None = None) -> list[Recommendation]:
        state = self.state
        pick_no = pick_no or (state.my_upcoming_picks(1) or [state.next_pick_no])[0]
        rnd = min(state.rounds, (pick_no - 1) // state.teams + 1)
        counts = self.my_counts()
        pool = self.available()
        if not pool:
            return []

        # Full "if I took him right now" score for every available player.  The
        # lookahead needs to be measured in these same units - scoring the pick
        # in front of you with strategy bonuses but scoring the future board on
        # bare VOR systematically favours whoever collects the most bonuses now.
        base_values = self._score_map(pool, rnd, counts, pick_no)
        board = [Candidate(p.key, p.name, p.pos,
                           self.adp.adp_of(p.name, p.pos,
                                           self.adp.undrafted_prior(p.pos)),
                           base_values[p.key])
                 for p in pool]

        ranked_pool = sorted(pool, key=lambda p: -base_values[p.key])
        if pos_filter:
            ranked_pool = [p for p in ranked_pool if p.pos == pos_filter.upper()]

        # Keep the shortlist position-diverse.  Raw marginal value is flat and
        # WR-friendly late, so a value-only shortlist stops surfacing RBs
        # exactly when roster balance says you need one.
        limit = max(self.strategy.sim_candidates, n * 2)
        shortlist: list[Projection] = []
        seen: set[str] = set()
        per_pos: dict[str, int] = {}
        for p in ranked_pool:
            if per_pos.get(p.pos, 0) < 4:
                per_pos[p.pos] = per_pos.get(p.pos, 0) + 1
                shortlist.append(p)
                seen.add(p.key)
        for p in ranked_pool:
            if len(shortlist) >= limit:
                break
            if p.key not in seen:
                shortlist.append(p)
                seen.add(p.key)
        if not shortlist:
            return []

        # Lookahead: what does the board look like at my next pick?
        upcoming = [p for p in state.my_upcoming_picks(3) if p > pick_no]
        next_pick = upcoming[0] if upcoming else None
        sim = SimResult()
        if next_pick and next_pick > pick_no + 1:
            next_rnd = min(state.rounds, (next_pick - 1) // state.teams + 1)
            value_maps = {}
            for pos in {p.pos for p in shortlist}:
                c2 = dict(counts)
                c2[pos] = c2.get(pos, 0) + 1
                value_maps[pos] = self._score_map(pool, next_rnd, c2, next_pick,
                                                  include_script=False)
            cands = [Candidate(p.key, p.name, p.pos,
                               self.adp.adp_of(p.name, p.pos, 999),
                               base_values[p.key]) for p in shortlist]
            sim = self.sim.run(
                board, pick_no + 1, next_pick, state.slot_for_pick,
                runs=sim_runs or self.strategy.sim_runs,
                my_candidates=cands, value_maps=value_maps)
        self.last_sim = sim

        recs: list[Recommendation] = []
        for proj in shortlist:
            adp = self.adp.adp_of(proj.name, proj.pos,
                                  self.adp.undrafted_prior(proj.pos))
            value_now, reasons = self.score_player(proj, rnd, counts, pick_no)
            tier = self.model.tier_of(proj.key)
            surv = sim.survival.get(proj.key, 1.0)
            lookahead = 0.0
            urgency = 0.0
            if next_pick and sim.runs:
                fallback = sim.next_best.get(proj.key, 0.0)
                lookahead = self.strategy.lookahead_discount * fallback
                edge = max(0.0, value_now - fallback)
                urgency = self.strategy.urgency_weight * (1.0 - surv) * edge
                if urgency > 0.05:
                    reasons.append(Reason(
                        "scarcity", urgency,
                        f"{1 - surv:.0%} likely gone by your pick at {next_pick}, "
                        f"and he is worth {edge:.1f} more than your expected "
                        f"fallback there"))
                elif surv > 0.80:
                    reasons.append(Reason(
                        "scarcity", 0.0,
                        f"{surv:.0%} likely to still be there at {next_pick} "
                        f"- you can probably wait"))

            score = value_now + lookahead + urgency
            recs.append(Recommendation(
                key=proj.key, name=proj.name, pos=proj.pos,
                team=(self._sleeper_meta.get(proj.key) or {}).get("team"),
                score=score, value_now=value_now, lookahead=lookahead,
                proj=proj, adp=adp, adp_delta=self.fall(adp, pick_no),
                survival=surv, tier=tier, reasons=reasons,
                off_guide=proj.key in self._off_guide,
                injury=(self.injury_of(proj.key) or (None,))[0]))

        recs.sort(key=lambda r: -r.score)
        top = recs[:n]
        if state.my_slot:
            self.store.record_recommendations(
                state.draft_id, pick_no, [r.to_dict() for r in top])
        return top

    # ---------------------------------------------------------- inspection

    def find(self, name: str) -> Projection | None:
        key = self.resolve_key(name)
        return self.projection(key) if key else None

    def is_available(self, key: str) -> bool:
        return any(p.key == key for p in self.available())

    def positional_outlook(self, pick_no: int | None = None) -> dict[str, dict]:
        """Per-position: how many of the top are expected to go before I pick."""
        state = self.state
        pick_no = pick_no or (state.my_upcoming_picks(1) or [state.next_pick_no])[0]
        out: dict[str, dict] = {}
        sim = self.last_sim
        pool = self.available()
        for pos in ("QB", "RB", "WR", "TE"):
            at_pos = [p for p in pool if p.pos == pos]
            drain = 0.0
            if sim and sim.runs:
                drain = sum(1.0 - sim.survival.get(p.key, 1.0) for p in at_pos[:12])
            tiers = self.model.tiers(pos)
            live_tier = None
            for i, t in enumerate(tiers):
                live = [p for p in t if any(a.key == p.key for a in at_pos)]
                if live:
                    live_tier = (i + 1, len(live))
                    break
            out[pos] = {
                "available": len(at_pos),
                "best": at_pos[0].name if at_pos else None,
                "expected_gone_of_top12": round(drain, 1),
                "current_tier": live_tier,
                "replacement_ppg": round(self.replacement.get(pos, 0.0), 2),
            }
        return out
