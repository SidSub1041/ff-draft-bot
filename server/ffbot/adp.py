"""ADP model.

Joel's rule #1 is "don't draft off rankings without understanding ADP", so the
bot needs a market baseline that is *independent* of the guide's opinions.
Three layers, highest-confidence first:

  1. Learned    - positional consumption curves refit from every draft the bot
                  has watched (see store.observe_draft).  Best source once you
                  have a few mocks of the right league shape.
  2. Imported   - a CSV you drop in data/adp/ (name,adp[,pos]).
  3. Prior      - Sleeper's within-position `search_rank` (real market ordering)
                  pushed through default consumption curves anchored to the
                  draft-flow landmarks stated in the guide itself.

Layer 3 alone already reproduces the guide's own ADP examples closely
(e.g. Parker Washington -> ~pick 84; the guide cites 85).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

from .curves import LogLogCurve
from .names import NameIndex, normalize

ADP_DIR = Path(__file__).resolve().parents[1] / "data" / "adp"

# (positional rank -> overall pick) anchors for a 12-team league.
# QB/TE anchors are pinned to landmarks stated on the guide's strategy page:
#   "QB3 2026 - 55th overall", "QB7-11 ... mostly 2-3 still available in Round 8",
#   "Rd 7/8 grab one of the last mid-round TEs".
DEFAULT_ANCHORS: dict[str, list[tuple[float, float]]] = {
    "RB": [(1, 1), (3, 4), (6, 8), (12, 20), (18, 32), (24, 48), (30, 68),
           (40, 105), (50, 140), (60, 168)],
    "WR": [(1, 3), (3, 7), (6, 12), (12, 22), (18, 33), (24, 48), (30, 64),
           (36, 84), (48, 120), (60, 155)],
    "QB": [(1, 28), (2, 42), (3, 55), (6, 72), (9, 88), (12, 105), (16, 130),
           (20, 152), (24, 170)],
    "TE": [(1, 14), (2, 20), (3, 32), (4, 44), (6, 70), (8, 90), (10, 105),
           (12, 120), (16, 145), (20, 165)],
    "K": [(1, 150), (5, 165), (12, 180)],
    "DEF": [(1, 140), (5, 158), (12, 175)],
}

# Superflex pulls QBs violently forward; TE-premium nudges TEs up.
SUPERFLEX_QB_ANCHORS: list[tuple[float, float]] = [
    (1, 3), (2, 6), (3, 9), (6, 18), (9, 30), (12, 42), (16, 62), (20, 85),
    (24, 110), (30, 145),
]


@dataclass
class AdpEntry:
    key: str
    name: str
    pos: str
    adp: float
    market_pos_rank: int
    source: str


class AdpModel:
    def __init__(self, teams: int = 12, superflex: bool = False,
                 te_premium: bool = False,
                 learned_anchors: dict[str, list[tuple[float, float]]] | None = None,
                 ) -> None:
        self.teams = teams
        self.superflex = superflex
        anchors = {k: list(v) for k, v in DEFAULT_ANCHORS.items()}
        if superflex:
            anchors["QB"] = list(SUPERFLEX_QB_ANCHORS)
        if te_premium:
            anchors["TE"] = [(r, p * 0.72) for r, p in anchors["TE"]]
        if learned_anchors:
            for pos, pts in learned_anchors.items():
                if len(pts) >= 3:
                    anchors[pos] = pts
        # ADP lives in pick numbers, and the Nth player off the board is the
        # Nth-best player whatever the league size - so the curves are NOT
        # stretched by team count.  What league size does change is scarcity at
        # the one-per-roster positions: with 16 teams every QB and TE is spoken
        # for sooner in pick terms, with 8 teams later.
        scarce = (12.0 / teams) ** 0.35
        self.curves = {}
        for pos, pts in anchors.items():
            curve = LogLogCurve(pts)
            if pos in ("QB", "TE", "K", "DEF") and abs(scarce - 1.0) > 0.01:
                curve = curve.scaled(scarce)
            self.curves[pos] = curve
        self.learned_positions = set(learned_anchors or {})
        self.entries: dict[str, AdpEntry] = {}
        self.index = NameIndex()
        self._imported: dict[str, float] = {}

    # ------------------------------------------------------------- building

    def curve_adp(self, pos: str, market_pos_rank: int) -> float:
        curve = self.curves.get(pos)
        if curve is None:
            return 999.0
        return curve(market_pos_rank)

    def build_from_sleeper(self, players: dict[str, dict],
                           limit_per_pos: int = 90) -> None:
        """Rank each position by Sleeper `search_rank`, then map rank -> pick."""
        by_pos: dict[str, list[dict]] = {}
        defenses: list[dict] = []
        for p in players.values():
            pos = (p.get("position") or "").upper()
            if pos not in DEFAULT_ANCHORS:
                continue
            if pos == "DEF":
                # Team defences carry no search_rank and no full_name.
                if p.get("active") and p.get("team"):
                    defenses.append(p)
                continue
            sr = p.get("search_rank")
            if not sr or sr >= 9999999:
                continue
            # Drop retired/unsigned entries: Sleeper keeps stale search_ranks
            # for players with no team (e.g. Todd Gurley at rank 27).
            if not p.get("active") or not p.get("team"):
                continue
            by_pos.setdefault(pos, []).append(p)

        for pos, plist in by_pos.items():
            plist.sort(key=lambda p: (p["search_rank"], p.get("full_name") or ""))
            for i, p in enumerate(plist[:limit_per_pos]):
                name = p.get("full_name") or \
                    f"{p.get('first_name','')} {p.get('last_name','')}".strip()
                key = normalize(name)
                if not key:
                    continue
                self.entries[key] = AdpEntry(
                    key=key, name=name, pos=pos, market_pos_rank=i + 1,
                    adp=self.curve_adp(pos, i + 1),
                    source="learned" if pos in self.learned_positions else "prior",
                )
                self.index.add(key, name, pos)

        # Defences: no market signal available, so they all share one rank and
        # the engine treats them as interchangeable (see Engine.DST_CAVEAT).
        for p in sorted(defenses, key=lambda d: d.get("team") or ""):
            name = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
            key = normalize(name)
            if not key:
                continue
            self.entries[key] = AdpEntry(
                key=key, name=name, pos="DEF", market_pos_rank=8,
                adp=self.curve_adp("DEF", 8), source="unranked")
            self.index.add(key, name, "DEF")

        self._apply_imports()

    # ------------------------------------------------------------- imports

    def load_csv(self, path: Path) -> int:
        """Import name,adp[,pos] rows. Highest-priority ADP source."""
        n = 0
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fields = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
            name_f = next((fields[k] for k in ("name", "player", "player name",
                                               "full_name") if k in fields), None)
            adp_f = next((fields[k] for k in ("adp", "avg", "average", "rank",
                                              "overall") if k in fields), None)
            if not name_f or not adp_f:
                raise ValueError(
                    f"{path.name}: need a name column and an adp column; "
                    f"saw {reader.fieldnames}")
            for row in reader:
                try:
                    val = float(str(row[adp_f]).strip())
                except (TypeError, ValueError):
                    continue
                key = normalize(str(row[name_f]))
                if key:
                    self._imported[key] = val
                    n += 1
        self._apply_imports()
        return n

    def load_dir(self, directory: Path = ADP_DIR) -> int:
        if not directory.exists():
            return 0
        total = 0
        for p in sorted(directory.glob("*.csv")):
            try:
                total += self.load_csv(p)
            except ValueError:
                continue
        return total

    def _apply_imports(self) -> None:
        """Imported ADP is taken at face value - it is already in pick numbers."""
        for key, val in self._imported.items():
            e = self.entries.get(key)
            if e:
                e.adp = val
                e.source = "imported"

    # ------------------------------------------------------------- queries

    def get(self, name: str, pos: str | None = None) -> AdpEntry | None:
        key = normalize(name)
        e = self.entries.get(key)
        if e and (pos is None or e.pos == pos):
            return e
        rk = self.index.resolve(name, pos)
        return self.entries.get(rk) if rk else None

    def adp_of(self, name: str, pos: str | None = None,
               fallback: float = 400.0) -> float:
        e = self.get(name, pos)
        return e.adp if e else fallback

    def undrafted_prior(self, pos: str) -> float:
        """ADP for a player the market has no opinion on."""
        return self.curve_adp(pos, 200)


def fit_anchors_from_picks(
    observations: list[tuple[str, int, int]],
    min_per_pos: int = 12,
) -> dict[str, list[tuple[float, float]]]:
    """Refit consumption anchors from observed drafts.

    `observations` is a list of (pos, positional_rank_when_taken, pick_no)
    normalised to a 12-team scale.  We take the median pick per positional rank
    and thin it down to a monotone anchor set.
    """
    buckets: dict[str, dict[int, list[int]]] = {}
    for pos, prank, pick in observations:
        if pos not in DEFAULT_ANCHORS or prank <= 0 or pick <= 0:
            continue
        buckets.setdefault(pos, {}).setdefault(prank, []).append(pick)

    out: dict[str, list[tuple[float, float]]] = {}
    for pos, ranks in buckets.items():
        if len(ranks) < min_per_pos:
            continue
        pts: list[tuple[float, float]] = []
        for r in sorted(ranks):
            vals = sorted(ranks[r])
            med = vals[len(vals) // 2]
            pts.append((float(r), float(med)))
        # enforce monotonicity (pool-adjacent-violators, forward pass)
        mono: list[tuple[float, float]] = []
        last = 0.0
        for r, p in pts:
            p = max(p, last + 0.25)
            mono.append((r, p))
            last = p
        out[pos] = mono
    return out


def blend_anchors(prior: list[tuple[float, float]],
                  learned: list[tuple[float, float]],
                  weight: float) -> list[tuple[float, float]]:
    """Shrink learned anchors toward the prior by `weight` in [0,1]."""
    pc = LogLogCurve(prior)
    out = []
    for r, p in learned:
        pp = pc(r)
        out.append((r, math.exp((1 - weight) * math.log(pp) + weight * math.log(p))))
    return out
