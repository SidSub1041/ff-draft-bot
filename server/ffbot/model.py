"""Player valuation model.

Pipeline, all fitted from data actually contained in the guide:

  1. Value curves - fit  ppg(rank) = A*rank^-b + C  per position against the
     guide's "'25 Adjusted PPG" tables (46 RB / 48 WR / 32 QB / 28 TE).  These
     are real observations of what each positional rank is worth, adjusted by
     the author for injuries, snap counts and 2026 situation.
  2. Projection  - convert a player's 2026 guide positional rank through that
     curve, then shrink toward their own 2025 adjusted PPG where we have it.
  3. Risk        - per-player sigma from position, rank, rookie status, sample
     size flags, and disagreement between the curve and the player's own 2025.
  4. Replacement - league-aware replacement level (handles FLEX and SUPERFLEX)
     turning projected PPG into VOR, the currency the engine actually spends.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .curves import PowerLawFit
from .guide import POSITIONS, Guide, GuidePlayer

# How much a player's own 2025 adjusted PPG moves them off the rank curve.
# The guide's rank already prices in 2025, so this is deliberately modest -
# it mostly captures "the ranker is being aggressive/conservative here".
SELF_WEIGHT = 0.28
ROOKIE_SELF_WEIGHT = 0.0

# Season-long PPG uncertainty, as  sigma = floor + slope * projected_ppg.
# Uncertainty scales with the projection: a 25-PPG RB has a far wider absolute
# range of outcomes than a 10-PPG one.  (Modelling sigma as *growing* with
# positional rank instead is backwards - it hands deep bench players enormous
# fake ceilings and makes the bot draft WR48 in round 5.)
SIGMA_FLOOR = {"QB": 1.8, "RB": 2.4, "WR": 2.4, "TE": 2.0, "K": 1.2, "DEF": 1.6}
SIGMA_SLOPE = {"QB": 0.13, "RB": 0.17, "WR": 0.18, "TE": 0.17, "K": 0.10, "DEF": 0.12}

# Rough weekly spread, used only for players with no ranking at all.
BASE_SIGMA = {"QB": 5.6, "RB": 6.4, "WR": 6.8, "TE": 5.2, "K": 4.0, "DEF": 4.5}

# Positions that can fill each lineup slot.
FLEX_ELIGIBLE = ("RB", "WR", "TE")
SUPERFLEX_ELIGIBLE = ("QB", "RB", "WR", "TE")


@dataclass
class Projection:
    key: str
    name: str
    pos: str
    pos_rank: int | None
    overall_rank: int | None
    ppg: float
    curve_ppg: float
    hist_ppg: float | None
    sigma: float
    vor: float = 0.0
    replacement: float = 0.0
    upside: float = 0.0        # ~85th percentile outcome
    floor: float = 0.0         # ~15th percentile outcome
    is_rookie: bool = False
    notes: list[str] = field(default_factory=list)


class ValueModel:
    def __init__(self, guide: Guide, fmt: str = "ppr") -> None:
        self.guide = guide
        self.fmt = fmt
        self.curves: dict[str, PowerLawFit] = {}
        for pos in POSITIONS:
            self.curves[pos] = PowerLawFit(guide.adj_ppg_table(pos))
        self.projections: dict[str, Projection] = {}
        self._project_all()

    # ----------------------------------------------------------- projection

    def curve_ppg(self, pos: str, pos_rank: float) -> float:
        curve = self.curves.get(pos)
        if curve is None:
            return 0.0
        return max(0.0, curve(pos_rank))

    def _sigma(self, p: GuidePlayer, ppg: float, curve: float,
               hist: float | None) -> float:
        s = (SIGMA_FLOOR.get(p.pos, 2.2)
             + SIGMA_SLOPE.get(p.pos, 0.17) * max(0.0, ppg))
        if p.is_rookie:
            # Rookies are bimodal - bust or league-winner - not a veteran
            # with a wider error bar.  Underselling this is why the engine
            # used to skip them: a mid veteran's blended PPG always edged a
            # rookie's curve value, so no rookie ever made a shortlist.
            s *= 1.45
        elif hist is None:
            s *= 1.12                     # no usable 2025 baseline
        if hist is not None and curve > 0:
            # Where the ranker and the player's own 2025 disagree, widen.
            disagree = abs(hist - curve) / max(curve, 1.0)
            s *= 1.0 + min(0.35, 0.5 * disagree)
        return s

    def _project_all(self) -> None:
        for key, gp in self.guide.players.items():
            prank = gp.prank(self.fmt)
            if prank is None:
                continue
            curve = self.curve_ppg(gp.pos, prank)
            hist = gp.adj_ppg_2025
            w = ROOKIE_SELF_WEIGHT if gp.is_rookie else SELF_WEIGHT
            if hist is None:
                ppg = curve
            else:
                ppg = (1 - w) * curve + w * hist
            sigma = self._sigma(gp, ppg, curve, hist)
            self.projections[key] = Projection(
                key=key, name=gp.name, pos=gp.pos, pos_rank=prank,
                overall_rank=gp.rank(self.fmt), ppg=ppg, curve_ppg=curve,
                hist_ppg=hist, sigma=sigma, is_rookie=gp.is_rookie,
                notes=list(gp.notes),
                # The rookie right tail is fatter than the left: the upside
                # quantile sits further out, the floor no lower than a
                # veteran's.  This is what lets late rookies compete with
                # mid veterans in the upside rounds without inflating their
                # median projection.
                upside=ppg + (1.30 if gp.is_rookie else 1.04) * sigma,
                floor=max(0.0, ppg - 1.04 * sigma),
            )

    # ---------------------------------------------------------- replacement

    def replacement_levels(self, teams: int, starters: dict[str, int],
                           bench: int = 0) -> dict[str, float]:
        """Replacement PPG per position for this league shape.

        Dedicated slots are allocated first, then FLEX/SUPERFLEX slots are
        filled greedily by whichever position offers the best next player -
        which is what actually determines where each position's cliff sits.
        """
        counts = {pos: teams * starters.get(pos, 0) for pos in POSITIONS}

        flex = teams * starters.get("FLEX", 0)
        sflex = teams * starters.get("SUPER_FLEX", 0)

        def next_val(pos: str) -> float:
            return self.curve_ppg(pos, counts[pos] + 1)

        for _ in range(flex):
            best = max(FLEX_ELIGIBLE, key=next_val)
            counts[best] += 1
        for _ in range(sflex):
            best = max(SUPERFLEX_ELIGIBLE, key=next_val)
            counts[best] += 1

        # A slice of the bench is really "startable depth" and pushes the
        # replacement line down a little further.
        if bench:
            depth = int(round(teams * bench * 0.34))
            for _ in range(depth):
                best = max(FLEX_ELIGIBLE, key=next_val)
                counts[best] += 1

        return {pos: self.curve_ppg(pos, max(1, counts[pos]))
                for pos in POSITIONS}

    def apply_replacement(self, teams: int, starters: dict[str, int],
                          bench: int = 0) -> dict[str, float]:
        levels = self.replacement_levels(teams, starters, bench)
        for proj in self.projections.values():
            proj.replacement = levels.get(proj.pos, 0.0)
            proj.vor = proj.ppg - proj.replacement
        return levels

    # ---------------------------------------------------------------- query

    def get(self, key: str) -> Projection | None:
        return self.projections.get(key)

    def by_pos(self, pos: str) -> list[Projection]:
        return sorted((p for p in self.projections.values() if p.pos == pos),
                      key=lambda p: p.pos_rank or 999)

    def tiers(self, pos: str, gap: float = 0.9) -> list[list[Projection]]:
        """Split a position into tiers wherever PPG drops by more than `gap`."""
        players = self.by_pos(pos)
        out: list[list[Projection]] = []
        cur: list[Projection] = []
        for i, p in enumerate(players):
            if cur and (cur[-1].ppg - p.ppg) > gap:
                out.append(cur)
                cur = []
            cur.append(p)
        if cur:
            out.append(cur)
        return out

    def tier_of(self, key: str) -> tuple[int, int] | None:
        """(tier number, players left in that tier) for a player."""
        proj = self.projections.get(key)
        if not proj:
            return None
        for i, tier in enumerate(self.tiers(proj.pos)):
            for j, p in enumerate(tier):
                if p.key == key:
                    return i + 1, len(tier) - j
        return None

    def summary(self) -> str:
        lines = ["Value curves fit to the guide's '25 Adjusted PPG tables:"]
        for pos in POSITIONS:
            lines.append(f"  {pos}: {self.curves[pos]!r}")
        return "\n".join(lines)
