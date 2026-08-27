"""Opponent modelling.

Each rival draft slot gets a profile that updates after every pick they make:

  * roster needs   - which starting slots they still have to fill
  * reach factor   - do they take players early or let value come to them
  * positional bias- are they drafting more RB/WR/QB/TE than the field
  * run detection  - is a positional run happening right now

These feed the Monte Carlo, so "who is likely gone before my next pick"
reflects who is actually picking, not a generic ADP curve.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


@dataclass
class OpponentProfile:
    slot: int
    label: str = ""
    picks: list[tuple[int, str, float]] = field(default_factory=list)  # (pick_no,pos,adp)
    pos_counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(POSITIONS, 0))
    reach_sum: float = 0.0     # sum of (adp - pick_no); positive = takes reaches
    n: int = 0

    @property
    def reach(self) -> float:
        """Average picks ahead of ADP this manager drafts (0 = disciplined)."""
        return self.reach_sum / self.n if self.n else 0.0

    def bias(self, pos: str, field_share: dict[str, float]) -> float:
        """How much this manager over-drafts `pos` vs the field, in [-1, 1]."""
        if self.n < 2:
            return 0.0
        mine = self.pos_counts.get(pos, 0) / self.n
        theirs = field_share.get(pos, 0.0)
        return max(-1.0, min(1.0, mine - theirs))

    def describe(self, field_share: dict[str, float]) -> str:
        if not self.n:
            return "no picks yet"
        counts = ", ".join(f"{p}{self.pos_counts[p]}" for p in ("QB", "RB", "WR", "TE")
                           if self.pos_counts[p])
        bits = [counts or "no skill picks"]
        r = self.reach
        if r > 6:
            bits.append(f"reaches ~{r:.0f} picks early")
        elif r < -6:
            bits.append(f"waits for value (~{-r:.0f} picks late)")
        else:
            bits.append("drafts near ADP")
        lean = max(("QB", "RB", "WR", "TE"), key=lambda p: self.bias(p, field_share))
        if self.bias(lean, field_share) > 0.16:
            bits.append(f"leaning {lean}")
        return "; ".join(bits)


class OpponentModel:
    def __init__(self, teams: int, starters: dict[str, int]) -> None:
        self.teams = teams
        self.starters = starters
        self.profiles: dict[int, OpponentProfile] = {
            s: OpponentProfile(slot=s) for s in range(1, teams + 1)
        }
        self.field_share: dict[str, float] = dict.fromkeys(POSITIONS, 0.0)
        self.recent: list[str] = []      # positions of the last N picks, oldest first

    # ------------------------------------------------------------- updates

    def observe(self, picks, adp_of) -> None:
        """Rebuild all profiles from the full pick list (idempotent)."""
        for p in self.profiles.values():
            p.picks.clear()
            p.pos_counts = dict.fromkeys(POSITIONS, 0)
            p.reach_sum = 0.0
            p.n = 0
        total = dict.fromkeys(POSITIONS, 0)

        for pk in picks:
            prof = self.profiles.get(pk.slot)
            if prof is None:
                continue
            adp = adp_of(pk.name, pk.pos)
            prof.picks.append((pk.pick_no, pk.pos, adp))
            if pk.pos in prof.pos_counts:
                prof.pos_counts[pk.pos] += 1
                total[pk.pos] += 1
            prof.reach_sum += (adp - pk.pick_no)
            prof.n += 1

        n_all = sum(total.values()) or 1
        self.field_share = {p: total[p] / n_all for p in POSITIONS}
        self.recent = [pk.pos for pk in picks[-8:]]

    # -------------------------------------------------------------- needs

    def needs(self, slot: int) -> dict[str, float]:
        """Urgency in [0, 1.6] per position for a given slot."""
        prof = self.profiles.get(slot)
        counts = prof.pos_counts if prof else dict.fromkeys(POSITIONS, 0)
        out: dict[str, float] = {}
        flex = self.starters.get("FLEX", 0) + self.starters.get("SUPER_FLEX", 0)
        for pos in POSITIONS:
            need = self.starters.get(pos, 0)
            have = counts.get(pos, 0)
            if have < need:
                out[pos] = 1.6 - 0.35 * have
            elif have < need + (flex if pos in ("RB", "WR", "TE") else 0):
                out[pos] = 0.7
            else:
                out[pos] = max(0.05, 0.5 - 0.14 * (have - need))
        # Nobody drafts K/DEF until the very end.
        for pos in ("K", "DEF"):
            if self.starters.get(pos, 0) and counts.get(pos, 0) == 0:
                out[pos] = 0.12
            else:
                out[pos] = 0.01
        return out

    def run_pressure(self, pos: str) -> float:
        """Extra pull toward a position currently being run on."""
        if len(self.recent) < 4:
            return 0.0
        recent4 = self.recent[-4:]
        share = recent4.count(pos) / len(recent4)
        return max(0.0, (share - 0.35)) * 1.5

    # ------------------------------------------------------------ scoring

    def pick_logit(self, slot: int, pos: str, adp: float, pick_no: int,
                   needs: dict[str, float] | None = None) -> float:
        """Log-preference for a rival at `slot` taking a player at `pick_no`."""
        prof = self.profiles.get(slot)
        needs = needs if needs is not None else self.needs(slot)
        reach = prof.reach if prof else 0.0
        # ADP pressure: value relative to the current pick, softened by the
        # manager's own tendency to reach.
        edge = (pick_no + max(0.0, reach) - adp) / 9.0
        score = edge
        score += 1.05 * math.log(max(0.05, needs.get(pos, 0.3)))
        score += 1.4 * (prof.bias(pos, self.field_share) if prof else 0.0)
        score += self.run_pressure(pos)
        return score

    def summary(self) -> list[tuple[int, str]]:
        return [(s, p.describe(self.field_share))
                for s, p in sorted(self.profiles.items())]
