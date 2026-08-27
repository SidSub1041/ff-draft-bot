"""Strategy configuration.

Joel's page-11 strategy is the default, expressed as *tunable knobs* rather
than hard-coded behaviour so you can argue with the bot after a mock and have
it actually change.  Every field is JSON-serialisable; `Strategy.save()` /
`Strategy.load()` persist your edits per league.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[1] / "data" / "strategies"


@dataclass
class Strategy:
    name: str = "joel_rb_heavy"
    description: str = (
        "Joel Smyth 2026 default: RB/RB start, three RBs from the top ~30, "
        "WR in rounds 3 and 5, QB at the QB7-11 ADP band (or a late rusher), "
        "TE only at genuine best-player-available."
    )

    # --- Round-by-round soft targets (guide page 11, 12-team PPR baseline) ---
    round_targets: dict[int, str] = field(default_factory=lambda: {
        1: "RB", 2: "RB", 3: "WR", 4: "BPA", 5: "WR", 6: "BPA", 7: "BPA",
        8: "QB", 9: "WR", 10: "TE", 11: "RB", 12: "QB", 13: "BPA",
        14: "DEF", 15: "K",
    })
    round_target_bonus: float = 1.15   # VOR added when a candidate matches

    # --- Positional preference multipliers on VOR ---
    pos_weights: dict[str, float] = field(default_factory=lambda: {
        "RB": 1.06, "WR": 1.0, "QB": 0.97, "TE": 0.97, "K": 0.5, "DEF": 0.5,
    })

    # --- RB rules ---
    rb_target_top_n: int = 30       # "getting 3 RB of top ~25-30"
    rb_target_count: int = 3
    rb_target_bonus: float = 1.5    # while still short of the target count
    rb_dead_zone: tuple[int, int] = (30, 40)   # "mostly a waste of time"
    rb_dead_zone_penalty: float = 2.0
    rb_early_rounds: int = 2        # rounds where RB gets the elite-anchor nudge
    rb_early_bonus: float = 1.2

    # --- WR rules ---
    wr_sweet_rounds: tuple[int, ...] = (3, 5)
    wr_sweet_bonus: float = 1.6
    wr_fade_pos_rank: tuple[int, int] = (5, 12)   # "WR5-12 range is not my favourite"
    wr_fade_penalty: float = 1.1
    wr_late_upside_bonus: float = 1.3             # WRs are the best late upside swing

    # --- QB rules ---
    qb_target_pos_rank: tuple[int, int] = (7, 11)
    qb_target_bonus: float = 1.8
    qb_early_penalty: float = 3.0      # taking QB1-2 early costs you
    qb_min_round: int = 7              # before this, QB is heavily discounted
    qb_faller_bonus: float = 1.5       # a top QB sliding well past ADP is worth it
    qb_faller_picks: int = 12          # ...by at least this many picks

    # --- TE rules ---
    te_elite_pos_rank: int = 4         # "I like TE2-4 if you have no favourite RB/WR"
    te_elite_bonus: float = 0.8
    te_mid_rounds: tuple[int, ...] = (7, 8)
    te_mid_bonus: float = 1.0
    te_punt_ok: bool = True
    te2_penalty: float = 2.5           # "rather go backup-RB hunting than a TE2"

    # --- K / DEF ---
    kdst_last_n_rounds: int = 2
    kdst_penalty: float = 25.0         # effectively a ban until the last 2 rounds

    # --- Value / ADP discipline (rules 1 and 2) ---
    reach_penalty_per_pick: float = 0.055   # cost per pick drafted ahead of ADP
    reach_free_picks: int = 8               # no penalty inside this cushion
    value_bonus_per_pick: float = 0.045     # reward for a faller
    value_bonus_cap: float = 3.5
    upside_weight_early: float = 0.10       # weight on the 85th-pct outcome
    upside_weight_late: float = 0.42
    upside_ramp_start_round: int = 8

    # --- Risk balance (rule 5) ---
    max_high_variance: int = 2
    high_variance_sigma: float = 8.0
    risk_penalty: float = 1.2

    # --- Roster construction ---
    bench_discount: float = 0.55       # value of a non-starter
    bench_decay: float = 0.62          # per extra body beyond starters+flex
    # Hard ceilings - VOR alone will happily build you ten WRs, because the WR
    # curve is flat late and every one of them clears replacement.
    max_pos: dict[str, int] = field(default_factory=lambda: {
        "QB": 2, "RB": 6, "WR": 6, "TE": 2, "K": 1, "DEF": 1,
    })
    over_cap_penalty: float = 4.5
    # Bench shape worth ending the draft with (guide rule 4: cemented RB2s and
    # handcuffs at the end of drafts).
    bench_targets: dict[str, int] = field(default_factory=lambda: {
        "QB": 2, "RB": 5, "WR": 5, "TE": 1,
    })
    bench_balance_bonus: float = 1.15
    bench_balance_start_round: int = 9
    handcuff_bonus: float = 0.9        # backup to an RB you already own
    stack_bonus: float = 0.0           # QB/pass-catcher same team (off by default)
    dynasty_age_weight: float = 0.0    # >0 favours youth (set by dynasty preset)

    # --- Simulation ---
    sim_runs: int = 400
    sim_candidates: int = 14
    lookahead_discount: float = 0.85
    # Two-pick lookahead cannot see that taking the player who will be gone
    # lets you collect the one who will not, three picks from now.  This term
    # prices the expected cost of waiting directly:
    #   urgency = weight * P(gone) * (his value now - your expected fallback)
    urgency_weight: float = 0.6

    # --- Rookies (guide rule 4: "Good process players late - Rookie WRs") ---
    # The engine has no 2025 baseline for a rookie, so left alone it always
    # prefers the mid veteran.  These knobs encode the guide's counterweight:
    # rookies ARE the late-round process players, and top draft capital hits
    # (stat #1: 11/11 top-25 NFL-drafted RBs reached RB1 by year two).
    rookie_upside_bonus: float = 1.1      # any guide-ranked rookie, late rounds
    rookie_bonus_start_round: int = 7
    rookie_capital_rank: int = 8          # dynasty rookie rank counting as elite
    rookie_capital_bonus: float = 1.2     # applies from round 4, not just late

    # --- Injuries (Sleeper's live injury_status, refreshed with the player
    # file).  Draft-day severity, not season-long verdicts: IR/PUP/Out means
    # you are burning a pick on an absence; Questionable in August is noise
    # worth a note, not a penalty.
    injury_penalties: dict[str, float] = field(default_factory=lambda: {
        "IR": 6.0, "PUP": 6.0, "Out": 6.0, "NA": 6.0, "DNR": 6.0,
        "Doubtful": 3.0, "Sus": 2.0, "Questionable": 0.4,
    })

    # --- User overrides learned from feedback ---
    player_bumps: dict[str, float] = field(default_factory=dict)   # key -> VOR delta
    banned: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- helpers

    def upside_weight(self, rnd: int) -> float:
        """Ramp from `upside_weight_early` to `_late`, starting to climb at
        `upside_ramp_start_round` (that round is already above baseline)."""
        base = self.upside_ramp_start_round - 1
        span = max(1, 15 - base)
        t = max(0.0, min(1.0, (rnd - base) / span))
        return self.upside_weight_early + t * (
            self.upside_weight_late - self.upside_weight_early)

    def target_for_round(self, rnd: int) -> str:
        return self.round_targets.get(rnd, "BPA")

    # ------------------------------------------------------------- storage

    def to_dict(self) -> dict:
        d = asdict(self)
        d["round_targets"] = {str(k): v for k, v in self.round_targets.items()}
        for k in ("rb_dead_zone", "wr_sweet_rounds", "wr_fade_pos_rank",
                  "qb_target_pos_rank", "te_mid_rounds"):
            d[k] = list(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Strategy":
        d = dict(d)
        if "round_targets" in d:
            d["round_targets"] = {int(k): v for k, v in d["round_targets"].items()}
        for k in ("rb_dead_zone", "wr_fade_pos_rank", "qb_target_pos_rank"):
            if k in d:
                d[k] = tuple(d[k])[:2]
        for k in ("wr_sweet_rounds", "te_mid_rounds"):
            if k in d:
                d[k] = tuple(d[k])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, name: str | None = None) -> Path:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIG_DIR / f"{name or self.name}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, name: str) -> "Strategy":
        path = CONFIG_DIR / f"{name}.json"
        if path.exists():
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if name in PRESETS:
            return PRESETS[name]()
        raise FileNotFoundError(f"no strategy named {name!r}")

    @staticmethod
    def available() -> list[str]:
        saved = [p.stem for p in CONFIG_DIR.glob("*.json")] if CONFIG_DIR.exists() else []
        return sorted(set(saved) | set(PRESETS))

    def adjust(self, **kwargs) -> list[str]:
        """Apply field updates, returning a log of what changed."""
        log = []
        for k, v in kwargs.items():
            if k not in self.__dataclass_fields__:
                log.append(f"! unknown setting {k!r} (ignored)")
                continue
            old = getattr(self, k)
            setattr(self, k, v)
            log.append(f"{k}: {old!r} -> {v!r}")
        return log


# ------------------------------------------------------------------ presets


def joel_rb_heavy() -> Strategy:
    return Strategy()


def zero_rb() -> Strategy:
    s = Strategy(
        name="zero_rb",
        description="Fade early RB; load WR/TE through round 5, attack RB value later.",
    )
    s.round_targets = {1: "WR", 2: "WR", 3: "WR", 4: "TE", 5: "WR", 6: "RB",
                       7: "RB", 8: "QB", 9: "RB", 10: "RB", 11: "RB", 12: "QB",
                       13: "BPA", 14: "DEF", 15: "K"}
    s.pos_weights = {"RB": 0.86, "WR": 1.12, "QB": 0.97, "TE": 1.05,
                     "K": 0.5, "DEF": 0.5}
    s.rb_target_count = 0
    s.rb_early_bonus = 0.0
    s.rb_dead_zone_penalty = 0.0
    s.wr_fade_penalty = 0.0
    s.wr_sweet_rounds = (1, 2, 3, 5)
    return s


def hero_rb() -> Strategy:
    s = Strategy(
        name="hero_rb",
        description="One elite RB in round 1, then WR/TE until RB value returns.",
    )
    s.round_targets = {1: "RB", 2: "WR", 3: "WR", 4: "WR", 5: "TE", 6: "RB",
                       7: "BPA", 8: "QB", 9: "WR", 10: "RB", 11: "RB",
                       12: "QB", 13: "BPA", 14: "DEF", 15: "K"}
    s.rb_target_count = 2
    s.rb_early_rounds = 1
    s.pos_weights = {"RB": 0.98, "WR": 1.06, "QB": 0.97, "TE": 1.0,
                     "K": 0.5, "DEF": 0.5}
    return s


def bpa() -> Strategy:
    s = Strategy(name="bpa", description="Pure value: VOR plus ADP discipline, no positional scripting.")
    s.round_targets = {i: "BPA" for i in range(1, 16)}
    s.round_targets.update({14: "DEF", 15: "K"})
    s.pos_weights = {"RB": 1.0, "WR": 1.0, "QB": 1.0, "TE": 1.0, "K": 0.5, "DEF": 0.5}
    s.rb_target_count = 0
    s.rb_early_bonus = 0.0
    s.rb_dead_zone_penalty = 0.0
    s.wr_sweet_bonus = 0.0
    s.wr_fade_penalty = 0.0
    s.qb_target_bonus = 0.0
    s.qb_early_penalty = 0.8
    s.te_elite_bonus = 0.0
    s.te2_penalty = 0.8
    return s


def superflex() -> Strategy:
    s = Strategy(
        name="superflex",
        description="Two-QB / superflex: QBs carry real positional value, take them early.",
    )
    s.round_targets = {1: "RB", 2: "QB", 3: "RB", 4: "QB", 5: "WR", 6: "WR",
                       7: "BPA", 8: "BPA", 9: "WR", 10: "TE", 11: "RB",
                       12: "QB", 13: "BPA", 14: "DEF", 15: "K"}
    s.pos_weights = {"RB": 1.02, "WR": 1.0, "QB": 1.22, "TE": 0.95,
                     "K": 0.5, "DEF": 0.5}
    s.qb_target_pos_rank = (1, 14)
    s.qb_early_penalty = 0.0
    s.qb_min_round = 1
    s.qb_target_bonus = 1.2
    return s


def dynasty() -> Strategy:
    s = Strategy(
        name="dynasty",
        description="Dynasty: weight youth and multi-year outlook, discount ageing veterans.",
    )
    s.dynasty_age_weight = 1.0
    # A two-month absence barely dents a multi-year asset.
    s.injury_penalties = {"IR": 2.5, "PUP": 2.0, "Out": 2.5, "NA": 2.5,
                          "DNR": 3.0, "Doubtful": 1.0, "Sus": 0.8,
                          "Questionable": 0.2}
    s.rookie_upside_bonus = 2.0
    s.rookie_bonus_start_round = 3
    s.rookie_capital_bonus = 2.2
    s.upside_weight_early = 0.28
    s.upside_weight_late = 0.55
    s.upside_ramp_start_round = 4
    s.kdst_penalty = 60.0
    s.round_targets = {i: "BPA" for i in range(1, 16)}
    s.rb_target_count = 1
    s.rb_dead_zone_penalty = 0.5
    s.pos_weights = {"RB": 0.92, "WR": 1.10, "QB": 1.02, "TE": 1.02,
                     "K": 0.2, "DEF": 0.2}
    return s


PRESETS = {
    "joel_rb_heavy": joel_rb_heavy,
    "zero_rb": zero_rb,
    "hero_rb": hero_rb,
    "bpa": bpa,
    "superflex": superflex,
    "dynasty": dynasty,
}


def for_league(teams: int, superflex_league: bool, is_dynasty: bool,
               base: str = "joel_rb_heavy") -> Strategy:
    """Pick a sensible default strategy for a league shape."""
    if superflex_league:
        s = PRESETS["superflex"]()
    elif is_dynasty:
        s = PRESETS["dynasty"]()
    else:
        s = PRESETS.get(base, joel_rb_heavy)()

    # Shallow leagues make everyone startable, so scarcity bonuses matter less;
    # deep leagues sharpen them.
    if teams <= 8:
        s.rb_target_bonus *= 0.6
        s.rb_dead_zone_penalty *= 0.6
        s.qb_target_bonus *= 0.7
        s.bench_discount = 0.45
        s.notes.append(
            f"{teams}-team league: scarcity is low, so positional runs matter "
            "less and best-player-available is weighted higher.")
    elif teams >= 14:
        s.rb_target_bonus *= 1.25
        s.rb_dead_zone_penalty *= 0.75   # the dead zone is startable when deep
        s.qb_target_bonus *= 1.15
        s.bench_discount = 0.65
        s.notes.append(
            f"{teams}-team league: replacement level is much lower, so "
            "starters and handcuffs gain value and the RB dead zone is softer.")
    return s
