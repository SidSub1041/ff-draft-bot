"""Load Joel Smyth's 2026 guide into a queryable object.

Everything downstream (projections, strategy, explanations) reads from here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from .names import NameIndex, normalize

DATA = Path(__file__).resolve().parents[1] / "data" / "guide_2026.json"

POSITIONS = ("QB", "RB", "WR", "TE")


@dataclass
class GuidePlayer:
    key: str
    name: str
    pos: str
    overall_rank: dict[str, int] = field(default_factory=dict)   # fmt -> rank
    pos_rank: dict[str, int] = field(default_factory=dict)       # fmt -> rank
    adj_ppg_2025: float | None = None
    adj_ppg_rank_2025: int | None = None
    rookie_rank: int | None = None
    rookie_team: str | None = None
    notes: list[str] = field(default_factory=list)

    def rank(self, fmt: str) -> int | None:
        return self.overall_rank.get(fmt)

    def prank(self, fmt: str) -> int | None:
        return self.pos_rank.get(fmt)

    @property
    def is_rookie(self) -> bool:
        return self.rookie_rank is not None


class Guide:
    def __init__(self, path: Path = DATA) -> None:
        self.raw = json.loads(path.read_text(encoding="utf-8"))
        self.players: dict[str, GuidePlayer] = {}
        self.index = NameIndex()
        self._build()

    # ---------------------------------------------------------------- build

    def _get(self, name: str, pos: str) -> GuidePlayer:
        key = normalize(name)
        p = self.players.get(key)
        if p is None:
            p = GuidePlayer(key=key, name=name, pos=pos)
            self.players[key] = p
        elif len(name) > len(p.name):
            p.name = name  # prefer the fuller spelling ("Deebo Samuel Sr.")
        return p

    def _build(self) -> None:
        for fmt in ("ppr", "half_ppr"):
            for row in self.raw["big_board"][fmt]:
                self._get(row["name"], row["pos"]).overall_rank[fmt] = row["rank"]
            for pos, names in self.raw["positional"][fmt].items():
                for i, nm in enumerate(names):
                    self._get(nm, pos).pos_rank[fmt] = i + 1

        for pos, rows in self.raw["adj_ppg_2025"].items():
            for row in rows:
                p = self._get(row["name"], pos)
                p.adj_ppg_2025 = row["adj_ppg"]
                p.adj_ppg_rank_2025 = row["rank"]

        for row in self.raw["dynasty_rookies"]:
            if not row.get("pos"):
                continue
            p = self._get(row["name"], row["pos"])
            p.rookie_rank = row["rank"]
            p.rookie_team = row.get("team")

        for p in self.players.values():
            self.index.add(p.key, p.name, p.pos)

        self._tag_stats()
        self._attach_rookie_profiles()

    def _attach_rookie_profiles(self) -> None:
        """Fold data/rookies_2026.json into player notes.

        The guide ranks rookies but says little about scheme or coaching
        fit, and the engine has no 2025 baseline for them at all - so this
        curated file (guide signals plus web consensus, source noted inside)
        is what lets a rookie's explanation talk about the landing spot
        instead of just a rank.
        """
        path = DATA.parent / "rookies_2026.json"
        if not path.exists():
            return
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for row in blob.get("rookies") or []:
            gp = self.resolve(str(row.get("name") or ""),
                              str(row.get("pos") or "") or None)
            if gp is None:
                continue
            bits = [f"[Rookie profile] {row.get('capital', '')}".strip()]
            for key in ("college", "situation", "playstyle"):
                if row.get(key):
                    bits.append(str(row[key]))
            gp.notes.insert(0, ". ".join(b.rstrip(".") for b in bits if b) + ".")

    def _tag_stats(self) -> None:
        """Attach each Top-50 stat to every guide player it names."""
        for stat in self.raw["top_50_stats"]:
            text = stat["text"]
            note = f"[Guide stat #{stat['n']}] {text}"
            for key in self._names_in(text):
                self.players[key].notes.append(note)

    def _names_in(self, text: str) -> set[str]:
        """Find guide players mentioned in a blob of prose."""
        hits: set[str] = set()
        # Candidate spans: 2-3 capitalised tokens, allowing ' . - and suffixes.
        pattern = re.compile(
            r"\b([A-Z][A-Za-z'\.\-]+(?:\s+[A-Z][A-Za-z'\.\-]+){1,2}"
            r"(?:\s+(?:Jr\.?|Sr\.?|II|III|IV))?)"
        )
        for m in pattern.finditer(text):
            key = self.index.resolve(m.group(1))
            if key:
                hits.add(key)
        # Nicknames / single-token references used in the guide's prose.
        for token, key in (("CMC", "christianmccaffrey"), ("KW3", "kennethwalker"),
                           ("BTJ", "brianthomas"), ("Puka", "pukanacua"),
                           ("Hubbard", "chubahubbard"), ("Bijan", "bijanrobinson"),
                           ("Jeanty", "ashtonjeanty"), ("Hampton", "omarionhampton")):
            if re.search(rf"\b{re.escape(token)}\b", text) and key in self.players:
                hits.add(key)
        return hits

    # ---------------------------------------------------------------- query

    def resolve(self, name: str, pos: str | None = None) -> GuidePlayer | None:
        key = self.index.resolve(name, pos)
        return self.players.get(key) if key else None

    def ranked(self, fmt: str, pos: str | None = None) -> list[GuidePlayer]:
        ps = [p for p in self.players.values() if p.rank(fmt) is not None]
        if pos:
            ps = [p for p in ps if p.pos == pos]
        return sorted(ps, key=lambda p: p.rank(fmt))

    def positional(self, fmt: str, pos: str) -> list[GuidePlayer]:
        ps = [p for p in self.players.values()
              if p.pos == pos and p.prank(fmt) is not None]
        return sorted(ps, key=lambda p: p.prank(fmt))

    def adj_ppg_table(self, pos: str) -> list[tuple[int, float]]:
        """(2025 adjusted-PPG rank, PPG) pairs used to fit the value curves."""
        return [(r["rank"], r["adj_ppg"]) for r in self.raw["adj_ppg_2025"][pos]]

    @cached_property
    def strategy(self) -> dict:
        return self.raw["strategy"]

    @cached_property
    def stats(self) -> list[dict]:
        return self.raw["top_50_stats"]

    def stat_search(self, term: str, limit: int = 5) -> list[dict]:
        t = term.lower()
        return [s for s in self.stats if t in s["text"].lower()][:limit]

    @property
    def rookies(self) -> list[dict]:
        return self.raw["dynasty_rookies"]


_GUIDE: Guide | None = None


def get_guide() -> Guide:
    global _GUIDE
    if _GUIDE is None:
        _GUIDE = Guide()
    return _GUIDE
