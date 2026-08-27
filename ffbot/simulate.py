"""Monte Carlo draft simulation: availability probabilities and VONA.

Two questions decide almost every fantasy pick:

  1. "Will this guy still be here at my next pick?"      -> survival()
  2. "What does my board look like after each choice?"   -> vona()

Both are answered by simulating the rival picks between now and your next turn
using the opponent model, many times, and averaging.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .opponents import OpponentModel


@dataclass
class Candidate:
    key: str
    name: str
    pos: str
    adp: float
    value: float          # roster-aware marginal value to *you*


@dataclass
class SimResult:
    survival: dict[str, float] = field(default_factory=dict)
    # key -> expected best value still on the board at your next pick
    next_best: dict[str, float] = field(default_factory=dict)
    next_best_names: dict[str, list[str]] = field(default_factory=dict)
    runs: int = 0
    picks_between: int = 0


class DraftSimulator:
    def __init__(self, opponents: OpponentModel, rng: random.Random | None = None,
                 temperature: float = 1.0, pool_size: int = 34) -> None:
        self.opp = opponents
        self.rng = rng or random.Random(20260817)
        self.temperature = temperature
        self.pool_size = pool_size

    # -------------------------------------------------------- single pick

    def _sample_pick(self, slot: int, board: list[Candidate], pick_no: int,
                     needs_cache: dict[int, dict[str, float]]) -> int:
        """Index into `board` of the player this rival takes."""
        pool = board[: self.pool_size]
        if not pool:
            return -1
        needs = needs_cache.get(slot)
        if needs is None:
            needs = self.opp.needs(slot)
            needs_cache[slot] = needs

        logits = [self.opp.pick_logit(slot, c.pos, c.adp, pick_no, needs)
                  for c in pool]
        m = max(logits)
        weights = [math.exp((l - m) / self.temperature) for l in logits]
        total = sum(weights)
        if total <= 0:
            return 0
        r = self.rng.random() * total
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w
            if acc >= r:
                return i
        return len(pool) - 1

    # ------------------------------------------------------------ full run

    def run(self, board: list[Candidate], from_pick: int, to_pick: int,
            slot_for_pick, runs: int = 400,
            my_candidates: list[Candidate] | None = None,
            value_maps: dict[str, dict[str, float]] | None = None) -> SimResult:
        """Simulate picks in [from_pick, to_pick) and measure the board at to_pick.

        `board` must be sorted best-first by ADP.  If `my_candidates` is given,
        each run also computes what would be left at `to_pick` had you taken
        that candidate now (the VONA lookahead).

        `value_maps` maps a position to {player_key: value} valuations computed
        for the roster you would have *after* adding a player of that position.
        Without it the lookahead ignores diminishing returns (taking RB now
        would not reduce the value of the next RB).
        """
        res = SimResult(runs=runs, picks_between=max(0, to_pick - from_pick))
        if not board:
            return res

        survived = dict.fromkeys((c.key for c in board), 0)
        cands = my_candidates or []
        next_best_sum = {c.key: 0.0 for c in cands}
        next_best_hits: dict[str, dict[str, int]] = {c.key: {} for c in cands}

        seq = [(p, slot_for_pick(p)) for p in range(from_pick, to_pick)]

        for _ in range(runs):
            needs_cache: dict[int, dict[str, float]] = {}
            taken: set[str] = set()
            live = list(board)
            for pick_no, slot in seq:
                idx = self._sample_pick(slot, live, pick_no, needs_cache)
                if idx < 0:
                    break
                gone = live.pop(idx)
                taken.add(gone.key)
                prof = self.opp.profiles.get(slot)
                if prof and gone.pos in prof.pos_counts:
                    # Reflect the pick in that manager's needs for later picks
                    # inside this same run.
                    prof.pos_counts[gone.pos] += 1
                    needs_cache[slot] = self.opp.needs(slot)
                    prof.pos_counts[gone.pos] -= 1

            for c in board:
                if c.key not in taken:
                    survived[c.key] += 1

            for cand in cands:
                vmap = (value_maps or {}).get(cand.pos)
                best = 0.0
                best_name = ""
                for c in live:
                    if c.key == cand.key:
                        continue
                    v = vmap.get(c.key, c.value) if vmap else c.value
                    if v > best:
                        best, best_name = v, c.name
                next_best_sum[cand.key] += best
                if best_name:
                    d = next_best_hits[cand.key]
                    d[best_name] = d.get(best_name, 0) + 1

        res.survival = {k: v / runs for k, v in survived.items()}
        res.next_best = {k: v / runs for k, v in next_best_sum.items()}
        res.next_best_names = {
            k: [n for n, _ in sorted(d.items(), key=lambda kv: -kv[1])[:3]]
            for k, d in next_best_hits.items()
        }
        return res


def expected_positional_drain(sim: SimResult, board: list[Candidate],
                              pos: str, top_n: int = 12) -> float:
    """How many of the top `top_n` players at `pos` are expected to disappear."""
    at_pos = [c for c in board if c.pos == pos][:top_n]
    if not at_pos:
        return 0.0
    return sum(1.0 - sim.survival.get(c.key, 1.0) for c in at_pos)
