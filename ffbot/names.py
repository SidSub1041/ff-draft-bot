"""Player-name normalisation and fuzzy resolution.

The draft guide, Sleeper, and any ADP file you import all spell names slightly
differently ("Kenneth Gainwell" vs "Kenny Gainwell", "R. Stevenson" vs
"Rhamondre Stevenson", "Deebo Samuel Sr." vs "Deebo Samuel").  Everything in
the bot keys off `normalize()`, and the handful of cases normalisation can't
reach live in ALIASES.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Guide spelling -> canonical (Sleeper) spelling.  Only for cases where
# normalisation alone cannot bridge the gap.
ALIASES = {
    "kennethgainwell": "kennygainwell",
    "billcroskeymerritt": "jacorycroskeymerritt",
    "rstevenson": "rhamondrestevenson",
    "thenderson": "treveyonhenderson",
    "cbrown": "chasebrown",
    "kwalker": "kennethwalker",
    "mikewashington": "mikewashington",
    "billcroskey": "jacorycroskeymerritt",
    "gabedavis": "gabrieldavis",
    "cameronskattebo": "camskattebo",
    "cameronward": "camward",
    "joshpalmer": "joshuapalmer",
    "tankbigsby": "tankbigsby",
    "chigokonkwo": "chigozieokonkwo",
    "hollywoodbrown": "marquisebrown",
    "scottymiller": "scottmiller",
    "nickchubb": "nickchubb",
    "demarcusrobinson": "demarcusrobinson",
}

# Common short forms that appear in ADP files / chat.
NICKNAMES = {
    "cmc": "christianmccaffrey",
    "arsb": "amonrastbrown",
    "jsn": "jaxonsmithnjigba",
    "btj": "brianthomas",
    "mhj": "marvinharrison",
    "kw3": "kennethwalker",
    "jt": "jonathantaylor",
    "ceedee": "ceedeelamb",
    "bijan": "bijanrobinson",
    "gibbs": "jahmyrgibbs",
    "jeanty": "ashtonjeanty",
    "achane": "devonachane",
    "puka": "pukanacua",
    "chase": "jamarrchase",
    "saquon": "saquonbarkley",
    "bowers": "brockbowers",
    "mcbride": "treymcbride",
    "lamar": "lamarjackson",
    "kelce": "traviskelce",
    "kittle": "georgekittle",
}


def normalize(name: str) -> str:
    """Lowercase, strip accents/punctuation/suffixes -> stable join key."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("ﬀ", "ff").replace("ﬁ", "fi").replace("ﬂ", "fl")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    parts = [p for p in s.split() if p]
    while len(parts) > 1 and parts[-1] in SUFFIXES:
        parts.pop()
    key = "".join(parts)
    key = NICKNAMES.get(key, key)
    return ALIASES.get(key, key)


def initial_key(name: str) -> str:
    """`first-initial + lastname` key, for "R. Stevenson"-style abbreviations."""
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    parts = [p for p in s.split() if p and p not in SUFFIXES]
    if len(parts) < 2:
        return ""
    return parts[0][0] + parts[-1]


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


class NameIndex:
    """Resolve arbitrary name text to a key in a known universe.

    Matching runs strictest-first: exact normalised key, then position-scoped
    first-initial+surname, then surname uniqueness, then fuzzy ratio.
    """

    def __init__(self) -> None:
        self._exact: dict[str, str] = {}
        self._initial: dict[str, list[str]] = {}
        self._surname: dict[str, list[str]] = {}
        self._pos: dict[str, str] = {}

    def add(self, key: str, display: str, pos: str | None = None) -> None:
        self._exact.setdefault(key, key)
        if pos:
            self._pos[key] = pos
        ik = initial_key(display)
        if ik:
            self._initial.setdefault(ik, []).append(key)
        parts = [p for p in re.sub(r"[^a-z0-9 ]", " ", display.lower()).split()
                 if p and p not in SUFFIXES]
        if parts:
            self._surname.setdefault(parts[-1], []).append(key)

    def _pos_ok(self, key: str, pos: str | None) -> bool:
        return pos is None or self._pos.get(key) in (None, pos)

    def resolve(self, name: str, pos: str | None = None,
                cutoff: float = 0.86) -> str | None:
        key = normalize(name)
        if key in self._exact and self._pos_ok(key, pos):
            return key

        for bucket in (self._initial.get(initial_key(name), []),):
            cands = [k for k in bucket if self._pos_ok(k, pos)]
            if len(cands) == 1:
                return cands[0]

        parts = [p for p in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
                 if p and p not in SUFFIXES]
        if parts:
            cands = [k for k in self._surname.get(parts[-1], [])
                     if self._pos_ok(k, pos)]
            if len(cands) == 1:
                return cands[0]
            if len(cands) > 1:
                scored = sorted(((similarity(key, c), c) for c in cands), reverse=True)
                if scored[0][0] >= cutoff and (
                    len(scored) == 1 or scored[0][0] - scored[1][0] > 0.05
                ):
                    return scored[0][1]

        pool = [k for k in self._exact if self._pos_ok(k, pos)]
        scored = sorted(((similarity(key, c), c) for c in pool), reverse=True)
        if scored and scored[0][0] >= cutoff:
            return scored[0][1]
        return None
