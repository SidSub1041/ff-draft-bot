"""Plain-English understanding for the draft bot.

The panel used to demand memorised commands ("rec 5 RB", "compare a vs b").
In a live draft you have ninety seconds, one hand on the phone and a room
talking at you, so this module lets you type the question the way you would
say it out loud.

There is deliberately no model in here.  This machine has no local LLM and no
API key, and an assistant that stops answering when the wifi drops is worse
than useless on draft night.  What follows is a small log-linear intent
classifier over lexical and structural features, plus a slot filler that
reuses the name resolution the rest of the bot already trusts.

    intent = parse("olave or nabers?", engine)
    text, data, chips = respond(intent, engine, last_recs)

How the classification works, in one paragraph.  The message is normalised,
then three passes run over it: position words are found and their character
spans recorded, numbers are found and recorded, and every intent's weighted
phrase patterns are matched and recorded.  Everything recorded is then masked
out of the string, and whatever survives is fed to the player resolver - so a
verb or a position word can never be mistaken for a name, because by the time
the name resolver runs, those characters are gone.  Each intent sums the
weights of the patterns it matched, structural evidence from the slot filler
is added on top (two resolved players push `compare`, a position pushes
`recommend_pos`, a recognised strategy edit pushes `strategy_set`), and the
argmax wins.  Confidence saturates with the winning score and is discounted
when the runner-up is close behind.  Nothing clears the floor -> `unknown`,
which the caller renders as "I did not get that" instead of guessing.

Every number in every answer comes from the engine.  Nothing here estimates,
rounds up, or narrates a figure the engine did not produce.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import explain
from .guide import get_guide
from .names import NICKNAMES, normalize
from .strategy import PRESETS, Strategy

# What the /api/chat contract should report when this path answered.
ENGINE_NAME = "builtin"

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

INTENTS = (
    "recommend", "recommend_pos", "why", "compare", "availability", "board",
    "roster", "room", "strategy_show", "strategy_set", "player_info", "bump",
    "ban", "note", "help", "greeting", "unknown",
)

# A message scoring below this has no real evidence behind it.
MIN_SCORE = 0.42
# A bare resolved name with no verb: we know who, not what.  Medium, honest.
LONE_NAME_CONF = 0.5
# Character written over anything already consumed by another pass.  It cannot
# survive normalisation, so it doubles as a hard boundary between name spans.
MASK = "|"


# --------------------------------------------------------------- data types


@dataclass
class Intent:
    """One parsed message: what was asked, how sure we are, and about whom."""

    name: str
    confidence: float
    slots: dict = field(default_factory=dict)
    raw: str = ""

    @property
    def understood(self) -> bool:
        return self.name != "unknown"

    @property
    def players(self) -> list[dict]:
        return self.slots.get("players") or []

    @property
    def position(self) -> str | None:
        return self.slots.get("position")

    def to_dict(self) -> dict:
        return {"name": self.name, "confidence": round(self.confidence, 3),
                "slots": self.slots, "raw": self.raw,
                "understood": self.understood}


@dataclass(frozen=True)
class Rule:
    """One weighted phrase pattern belonging to one intent.

    `requires` gates *scoring* on the slot filler's findings, but never gates
    masking: "who is" is masked out of the name search either way, and only
    counts toward `player_info` when a player actually resolved.
    """

    intent: str
    weight: float
    rx: re.Pattern
    requires: str = ""       # "" | "player" | "players2" | "position"


# ------------------------------------------------------------- normalisation


_CONTRACTIONS = {
    "s": " is", "re": " are", "m": " am", "ll": " will", "ve": " have",
    "d": " would", "t": "t",
}
_NEGATIONS = {
    "dont": "do not", "doesnt": "does not", "didnt": "did not",
    "cant": "can not", "wont": "will not", "shouldnt": "should not",
    "isnt": "is not", "arent": "are not", "wouldnt": "would not",
    "couldnt": "could not", "havent": "have not", "aint": "is not",
}
_LEADING_FILLER = {
    "uh", "um", "erm", "er", "hmm", "hm", "well", "so", "ok", "okay", "alright",
    "yo", "hi", "hey", "hello", "bot", "ffbot", "please", "pls", "plz", "just",
    "quick", "question", "q",
}
_TRAILING_FILLER = {"please", "pls", "plz", "thx", "man", "dude", "bro", "mate"}


def _normalise(text: str) -> str:
    """Lowercase, de-punctuate and de-fluff, keeping name characters intact.

    Contractions are expanded by suffix so that "what's" becomes "what is"
    while "Ja'Marr" survives untouched - the suffix after the apostrophe has
    to be an actual contraction ending.
    """
    s = (text or "").strip().lower()
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("&", " and ")

    def _expand(m: re.Match) -> str:
        stem, suffix = m.group(1), m.group(2)
        if suffix == "t":
            return m.group(0)          # handled by the negation table below
        return stem + _CONTRACTIONS[suffix]

    s = re.sub(r"\b(\w+)'(s|re|m|ll|ve|d)\b", _expand, s)
    s = re.sub(r"\b(\w+)n't\b", lambda m: m.group(1) + " not", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    words = [_NEGATIONS.get(w, w) for w in s.split()]
    s = " ".join(words)

    # Strip filler only while something meaningful is left behind, so that a
    # bare "hey" still reads as a greeting instead of an empty message.
    parts = s.split()
    while len(parts) > 1 and parts[0] in _LEADING_FILLER:
        parts.pop(0)
    while len(parts) > 1 and parts[-1] in _TRAILING_FILLER:
        parts.pop()
    return " ".join(parts)


# ------------------------------------------------------------------ stopwords

# Tokens that can never be part of a player name: function words plus the
# bot's own domain vocabulary.  Deliberately contains no plausible surname -
# "chase", "love", "hall" and friends stay out so those players still resolve.
STOP = {
    # function words
    "a", "an", "the", "i", "me", "my", "mine", "myself", "you", "your", "we",
    "us", "our", "he", "him", "his", "she", "her", "it", "its", "they", "them",
    "their", "this", "that", "these", "those", "there", "here", "who", "whom",
    "what", "which", "when", "where", "why", "how", "is", "are", "am", "was",
    "were", "be", "been", "being", "do", "does", "did", "done", "will",
    "would", "should", "could", "can", "cannot", "may", "might", "shall",
    "must", "have", "has", "had", "of", "for", "to", "in", "on", "at", "by",
    "with", "from", "about", "into", "than", "then", "if", "as", "and", "or",
    "but", "not", "no", "yes", "yeah", "yep", "nope", "ok", "okay", "please",
    "thanks", "thank", "let", "lets", "us", "some", "any", "all", "both",
    "each", "every", "more", "most", "less", "least", "other", "others",
    "else", "also", "too", "very", "much", "many", "now", "next", "still",
    "again", "ever", "never", "always", "just", "only", "even", "up", "down",
    "out", "off", "over", "under", "back", "away", "s", "t",
    # verbs and command vocabulary
    "get", "got", "give", "gives", "gimme", "take", "takes", "taking", "took",
    "draft", "drafts", "drafting", "drafted", "pick", "picks", "picking",
    "picked", "go", "goes", "going", "went", "grab", "show", "shows", "list",
    "tell", "tells", "say", "says", "said", "think", "thinks", "like",
    "likes", "love", "want", "wants", "need", "needs", "help", "helps",
    "make", "makes", "made", "see", "sees", "look", "looks", "looking",
    "find", "finds", "wait", "waiting", "waits", "know", "knows", "explain",
    "explains", "compare", "compares", "recommend", "recommends", "suggest",
    "suggests", "avoid", "ban", "bump", "note", "notes", "remember", "move",
    "keep", "hold", "start", "stop", "quit", "switch", "use", "try", "set",
    "vs", "versus", "v", "instead", "rather", "against", "between", "either",
    # domain vocabulary
    "board", "roster", "team", "teams", "squad", "lineup", "bench", "starter",
    "starters", "strategy", "plan", "script", "guide", "stat", "stats",
    "value", "adp", "rank", "ranks", "ranked", "ranking", "tier", "tiers",
    "upside", "floor", "ceiling", "risk", "risky", "reach", "reaching",
    "run", "runs", "pressure", "clock", "turn", "slot", "league", "mock",
    "sleeper", "points", "ppg", "vor", "projection", "projections", "case",
    "argument", "reasoning", "logic", "option", "options", "guy", "guys",
    "player", "players", "name", "names", "thought", "thoughts", "idea",
    "ideas", "choice", "choices", "call", "rec", "recs", "left", "leftover",
    "available", "gone", "thin", "deep", "best", "top", "good", "great",
    "better", "worse", "bad", "solid", "safe", "cheap", "high", "low",
    "early", "late", "hot", "cold", "first", "second", "third", "round",
    "rounds", "aggressive", "conservative", "patient", "careful",
}


# ------------------------------------------------------------------ positions

# Ordered longest-phrase-first: Python alternation is leftmost-first, not
# leftmost-longest, so "running backs" has to be offered before "backs".
_POS_ALTS: list[tuple[str, str]] = [
    ("QB", r"quarterbacks?"),
    ("QB", r"signal callers?"),
    ("RB", r"running ?backs?"),
    ("RB", r"rushers?"),
    ("WR", r"wide ?receivers?"),
    ("WR", r"pass catchers?"),
    ("WR", r"receivers?"),
    ("WR", r"wideouts?"),
    ("TE", r"tight ?ends?"),
    ("DEF", r"defen[cs]es?"),
    ("K", r"kickers?"),
    ("RB", r"backs"),
    ("QB", r"qbs?"),
    ("RB", r"rbs?"),
    ("WR", r"wrs?"),
    ("TE", r"tes?"),
    ("DEF", r"dsts?"),
    ("DEF", r"d ?st"),
    ("DEF", r"def"),
]
_POS_GROUPS = {f"p{i}": pos for i, (pos, _) in enumerate(_POS_ALTS)}
_POS_RX = re.compile(
    r"(?<!\w)(?:"
    + "|".join(f"(?P<p{i}>{pat})" for i, (_, pat) in enumerate(_POS_ALTS))
    + r")(?!\w)")

# A bare "k" is a kicker only next to a selection cue; otherwise it is far more
# likely to be an initial in a name ("K. Williams").
_K_RX = re.compile(r"(?<!\w)k(?!\w)")
_K_CUES = {"best", "top", "a", "any", "some", "my", "need", "want", "draft",
           "take", "get", "the", "last", "final", "and"}
_K_AFTER = {"left", "available", "now", "yet", "options"}


def _positions(norm: str) -> tuple[list[str], list[tuple[int, int]]]:
    """Position words in the message, with the spans they occupy."""
    found: list[str] = []
    spans: list[tuple[int, int]] = []
    for m in _POS_RX.finditer(norm):
        pos = _POS_GROUPS[m.lastgroup]
        found.append(pos)
        spans.append(m.span())
    for m in _K_RX.finditer(norm):
        before = norm[:m.start()].split()
        after = norm[m.end():].split()
        prev = before[-1] if before else ""
        nxt = after[0] if after else ""
        if (not before and not after) or prev in _K_CUES or nxt in _K_AFTER:
            found.append("K")
            spans.append(m.span())
    return found, spans


# -------------------------------------------------------------------- numbers


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a couple": 2, "a few": 3, "a handful": 5,
}
_NUM = r"(?:\d{1,3}|" + "|".join(_WORD_NUMBERS) + r")"
_ROUND_RX = re.compile(rf"(?<!\w)(?:round|rd)\s+({_NUM})(?!\w)")
_PICK_RX = re.compile(
    rf"(?<!\w)(?:at\s+)?pick\s*(?:no\.?|number|#)?\s*(\d{{1,3}})(?!\w)")
_AT_RX = re.compile(r"(?<!\w)at\s+(\d{2,3})(?!\w)")
_COUNT_RX = re.compile(
    rf"(?<!\w)(?:top|best|first|give me|gimme|show me|list|only)\s+({_NUM})"
    r"(?!\w)")
_COUNT_OF_RX = re.compile(
    rf"(?<!\w)({_NUM})\s+(?:options|names|guys|players|choices|ideas|picks)"
    r"(?!\w)")
_BARE_RX = re.compile(r"(?<!\w)(\d{1,2})(?!\w)")


def _as_int(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def _numbers(norm: str) -> tuple[dict[str, int], list[tuple[int, int]]]:
    """Round / pick / count slots, with the spans they occupy.

    Ordered most-specific-first so "round 5" is a round and never a count, and
    every consumed number is masked so it cannot resurface as part of a name.
    """
    out: dict[str, int] = {}
    spans: list[tuple[int, int]] = []

    for key, rx in (("round", _ROUND_RX), ("pick", _PICK_RX), ("pick", _AT_RX),
                    ("count", _COUNT_RX), ("count", _COUNT_OF_RX)):
        if key in out:
            continue
        m = rx.search(norm)
        if m:
            val = _as_int(m.group(1))
            if val is not None:
                out[key] = val
                spans.append(m.span())

    if "count" not in out:
        for m in _BARE_RX.finditer(norm):
            if any(s <= m.start() < e for s, e in spans):
                continue
            val = _as_int(m.group(1))
            if val is not None:
                out["count"] = val
                spans.append(m.span())
                break

    if "count" in out:
        out["count"] = max(1, min(25, out["count"]))
    return out, spans


# --------------------------------------------------------------- intent rules

# (intent, weight, pattern, requires).  Patterns are literal phrases and
# function-word alternations only - never wildcards - because every match is
# masked out before names are read, and a greedy pattern would eat a name.
_PHRASES: list[tuple[str, float, str, str]] = [
    # ---------------------------------------------------------- recommend
    ("recommend", 1.4, r"who should (?:i|we) (?:take|draft|pick|go with|grab)", ""),
    ("recommend", 1.4, r"what should (?:i|we) (?:do|take|draft|pick)", ""),
    ("recommend", 1.4, r"who do (?:i|we) (?:take|draft|want)", ""),
    ("recommend", 1.3, r"who do you like", ""),
    ("recommend", 1.3, r"who (?:you|ya) got", ""),
    ("recommend", 1.3, r"help me (?:pick|draft|choose|decide|out)", ""),
    ("recommend", 1.3, r"best available", ""),
    ("recommend", 1.2, r"on the clock", ""),
    ("recommend", 1.2, r"recommend(?:ations?|s)?", ""),
    ("recommend", 1.2, r"(?:make|give me|gimme) (?:the |a |my )?(?:pick|call)", ""),
    ("recommend", 1.1, r"suggest(?:ions?|s)?", ""),
    ("recommend", 1.1, r"who is (?:my|the) (?:pick|guy|play)", ""),
    ("recommend", 1.0, r"what (?:do i do |should be )?(?:now|next)", ""),
    ("recommend", 1.0, r"who is next", ""),
    ("recommend", 1.0, r"^(?:go|rec|recs|next|options|shortlist|board me)$", ""),
    ("recommend", 0.9, r"my turn", ""),
    ("recommend", 0.9, r"i am (?:on the clock|up)", ""),
    ("recommend", 0.8, r"available", ""),
    ("recommend", 0.7, r"any good", ""),
    ("recommend", 0.7, r"(?:best|top) (?:few|options|names|guys|players)", ""),
    ("recommend", 0.65, r"best|top", ""),
    ("recommend", 0.6, r"show me|give me|gimme|list", ""),
    ("recommend", 0.5, r"should i (?:take|draft|grab|go)", ""),
    ("recommend", 0.45, r"left", ""),
    # ---------------------------------------------------------------- why
    ("why", 1.4, r"why", ""),
    ("why", 1.4, r"explain", ""),
    ("why", 1.3, r"the case for|make the case|sell me on", ""),
    ("why", 1.3, r"what do you (?:see in|like about)", ""),
    ("why", 1.2, r"justify|reasoning|your logic|your thinking", ""),
    ("why", 1.2, r"how come", ""),
    ("why", 0.9, r"tell me why", ""),
    # ------------------------------------------------------------ compare
    ("compare", 1.4, r"compare|head to head|side by side", ""),
    ("compare", 1.3, r"which (?:one )?is better|who is better|which do you like",
     ""),
    ("compare", 1.3, r"(?:vs|versus)", ""),
    ("compare", 0.8, r"instead of|rather than|ahead of", ""),
    ("compare", 0.7, r"over", ""),
    ("compare", 0.35, r"or", ""),
    # ------------------------------------------------------- availability
    ("availability", 1.2, r"last(?:s)?|survive(?:s)?|make it back|come back", ""),
    ("availability", 1.2, r"(?:still )?be (?:there|around|available)", ""),
    ("availability", 1.2, r"can i wait|should i wait|worth waiting|safe to wait",
     ""),
    ("availability", 1.1, r"is (?:he|she|they) gone|already gone|off the board",
     ""),
    ("availability", 0.9, r"gone", ""),
    ("availability", 0.9, r"next pick", ""),
    ("availability", 0.5, r"will", ""),
    # -------------------------------------------------------------- board
    ("board", 1.3, r"how is the board|state of the board|read the board", ""),
    ("board", 1.3, r"positional outlook|outlook|tier cliffs?|scarcity", ""),
    ("board", 1.2, r"what is (?:left|out there|on the board)", ""),
    ("board", 1.2, r"who is left", ""),
    ("board", 1.1, r"tiers?", ""),
    ("board", 1.0, r"the board", ""),
    ("board", 0.6, r"left", ""),
    # ------------------------------------------------------------- roster
    ("roster", 1.4, r"my (?:team|roster|squad|guys|players|picks)", ""),
    ("roster", 1.4, r"what do i need|what am i missing|where am i thin", ""),
    ("roster", 1.3, r"am i thin|my needs|my gaps|my holes|my starters", ""),
    # Contracted first person: "im thin at wr" is the same question as "am i thin".
    ("roster", 1.4, r"i'?m (?:thin|light|weak|short|stacked|set|good) (?:at|on)", ""),
    ("roster", 1.3, r"thin at|light at|weak at|short at|stacked at", ""),
    ("roster", 1.2, r"how does my team look|who do i have|what do i have", ""),
    ("roster", 1.0, r"roster", ""),
    # --------------------------------------------------------------- room
    ("room", 1.4, r"read the room|the room|other teams|everyone else", ""),
    ("room", 1.4, r"what are the others doing|what is everyone doing", ""),
    ("room", 1.3, r"is there a run|any runs?|run on|positional run", ""),
    # "anyone running rbs" is a question about the ROOM, not a request for a
    # shortlist - it was landing on recommend_pos and answering the wrong thing.
    ("room", 1.4, r"any ?(?:one|body) (?:running|taking|grabbing|on)", ""),
    ("room", 1.4, r"(?:are|is) (?:people|everyone|anyone|they) (?:running|taking|grabbing)", ""),
    ("room", 1.3, r"run(?:ning)? on (?:the )?(?:rb|wr|te|qb|back|receiver|end)", ""),
    ("room", 1.3, r"opponents?|rivals?|league ?mates?|the field", ""),
    ("room", 1.1, r"who is drafting what|what are they doing", ""),
    # ------------------------------------------------------- strategy_show
    ("strategy_show", 1.5, r"what is (?:my|your|the) (?:strategy|plan|approach)",
     ""),
    ("strategy_show", 1.5, r"what are you (?:optimi[sz]ing|going) for", ""),
    ("strategy_show", 1.4, r"current strategy|show (?:me )?(?:the )?strategy", ""),
    ("strategy_show", 1.2, r"what are you doing|how are you drafting", ""),
    ("strategy_show", 1.0, r"strategy|settings|knobs", ""),
    # -------------------------------------------------------- strategy_set
    ("strategy_set", 1.3, r"stop|quit|no more|enough", ""),
    ("strategy_set", 1.2, r"switch to|change to|go with|lets go|use the", ""),
    ("strategy_set", 1.1, r"be more|be less|more of|fewer|dial", ""),
    ("strategy_set", 1.1, r"from now on|going forward", ""),
    # --------------------------------------------------------- player_info
    ("player_info", 1.4, r"tell me about|what about|how about|info on|look up",
     ""),
    ("player_info", 1.4, r"what does the guide say|guide say|the guide on", ""),
    ("player_info", 1.3, r"scouting report|profile|his numbers|the numbers on",
     ""),
    ("player_info", 1.2, r"who is", "player"),
    ("player_info", 1.0, r"stats on|talk to me about", ""),
    # ---------------------------------------------------------------- bump
    ("bump", 1.4, r"bump|move up|nudge up|push up|upgrade", "player"),
    ("bump", 1.3, r"i (?:like|love|want|am high on|really like)", "player"),
    ("bump", 1.2, r"high(?:er)? on|bullish on|my guy", "player"),
    ("bump", 0.6, r"move|up", "player"),
    # ----------------------------------------------------------------- ban
    ("ban", 1.5, r"never (?:draft|take|pick|recommend)", ""),
    ("ban", 1.4, r"(?:do not|no) want|not interested in|stay away from", "player"),
    ("ban", 1.4, r"blacklist|ban", "player"),
    ("ban", 1.3, r"avoid|fade|hate|do not draft|do not take", "player"),
    # ---------------------------------------------------------------- note
    ("note", 1.7, r"^note\b", ""),
    ("note", 1.6, r"remember (?:that|this)|make a note|for the record", ""),
    ("note", 1.5, r"log this|write this down|note to self", ""),
    # ---------------------------------------------------------------- help
    ("help", 1.6, r"what can you do|what can i ask|how do i use|what do you do",
     ""),
    ("help", 1.6, r"^(?:help|commands|\?|menu)$", ""),
    ("help", 1.2, r"commands", ""),
    # ------------------------------------------------------------ greeting
    ("greeting", 1.5,
     r"^(?:hi|hey|hello|yo|sup|howdy)(?: there| bot| again)?$", ""),
    ("greeting", 1.5,
     r"^(?:thanks|thank you|ty|cheers|nice|cool|awesome|great|perfect|"
     r"got it|sweet|lovely|good stuff)$", ""),
    ("greeting", 1.4, r"^good (?:morning|afternoon|evening)$", ""),
]


def _compile(phrases: Iterable[tuple[str, float, str, str]]) -> list[Rule]:
    rules = []
    for intent, weight, pat, requires in phrases:
        anchored = pat.startswith("^") or pat.endswith("$")
        body = pat if anchored else rf"(?<!\w)(?:{pat})(?!\w)"
        rules.append(Rule(intent, weight, re.compile(body), requires))
    return rules


_RULES = _compile(_PHRASES)


def _match_rules(norm: str) -> list[tuple[Rule, tuple[int, int]]]:
    """Every rule hit in the message, with the span it consumed."""
    hits = []
    for rule in _RULES:
        for m in rule.rx.finditer(norm):
            hits.append((rule, m.span()))
    return hits


# ------------------------------------------------------------ strategy edits


_AGGR_DOWN = re.compile(
    r"(?:less|not so|not too|too|way too) aggressive|be (?:more )?"
    r"(?:conservative|careful|patient)|play it safe|dial (?:it|things) back|"
    r"calm down|be safer|slow down")
_AGGR_UP = re.compile(
    r"(?:more|way more|much more|extra|very) aggressive|be aggressive|"
    r"get aggressive|take (?:more )?risks?|be bold|be ruthless|push harder")
_REACH_UP = re.compile(
    r"(?:quit|stop|no more|do not|less|hate|avoid) reach(?:ing|es)?|"
    r"do not reach|stop reaching|no reaches|stick to adp")
_REACH_DOWN = re.compile(
    r"(?:ok(?:ay)? to reach|reach more|ignore adp|forget adp|do not care "
    r"about adp)")
_UPSIDE_UP = re.compile(
    r"chase (?:the )?upside|more upside|more ceiling|swing for the fences|"
    r"go for (?:the )?ceiling|upside plays?|be spicy")
_UPSIDE_DOWN = re.compile(
    r"high(?:er)? floor|safer floor|floor over ceiling|more floor|"
    r"safe(?:r)? picks|less variance|lower variance|boring is fine")
_VALUE_UP = re.compile(
    r"(?:target|chase|prioriti[sz]e|take|follow) (?:the )?value|value only|"
    r"take the fallers|draft the value")
_QB_LATER = re.compile(
    r"wait (?:longer )?on (?:the |a )?qb|no early qb|qb can wait|"
    r"later (?:on )?qb|punt (?:the )?qb")
_QB_SOONER = re.compile(
    r"qb (?:sooner|earlier)|earlier (?:on )?qb|take (?:a |the )?qb "
    r"(?:sooner|earlier)|qb sooner")
_SET_RX = re.compile(
    r"(?<!\w)set\s+([a-z_][a-z0-9_]*)\s+(?:to\s+)?(-?\d+(?:\.\d+)?|true|false)"
    r"(?!\w)")

_NEG_CUE = re.compile(
    r"(?:stop|quit|no more|never|do not|enough|fewer|less|skip|punt|avoid|"
    r"hate|forget|lay off)\b[^.]{0,24}$")
_MORE_CUE = re.compile(
    r"(?:more|another|extra|additional|load up on|double up on|prioriti[sz]e|"
    r"target|want|need)\b[^.]{0,18}$")

_PRESET_WORDS = [
    (r"zero ?rb", "zero_rb"),
    (r"hero ?rb", "hero_rb"),
    (r"\bbpa\b|best player available|pure value", "bpa"),
    (r"super ?flex|2 ?qb|two ?qb", "superflex"),
    (r"dynasty|keeper", "dynasty"),
    (r"joel|rb heavy|the default|default strategy", "joel_rb_heavy"),
]
_PRESET_VERB = re.compile(
    r"(?:go|switch to|change to|use|try|run|play|do|lets go|set|make it|"
    r"back to|be)\s+(?:the\s+)?$")


def _strategy_edits(norm: str, pos_spans: list[tuple[int, int]],
                    positions: list[str], strat: Strategy
                    ) -> tuple[dict, str | None, list[str]]:
    """Turn plain-English tuning into concrete Strategy field values.

    Every new value is derived from the strategy's current value, so the knobs
    move relative to whatever you are already running rather than snapping to
    numbers invented here.
    """
    settings: dict[str, Any] = {}
    labels: list[str] = []
    preset: str | None = None

    def weights() -> dict[str, float]:
        return settings.setdefault("pos_weights", dict(strat.pos_weights))

    # --- preset switches
    for pat, name in _PRESET_WORDS:
        m = re.search(pat, norm)
        if not m:
            continue
        # A bare "bpa" is a switch; "best player available" needs a verb, or
        # every "best available" question would rewrite the strategy.
        explicit = bool(_PRESET_VERB.search(norm[:m.start()]))
        bare_token = m.group(0) in ("bpa", "zero rb", "zerorb", "hero rb",
                                    "herorb", "dynasty", "superflex")
        if explicit or bare_token:
            preset = name
            labels.append(f"strategy preset -> {name}")
            break

    # --- per-position pushes: read the words immediately before the position
    for pos, (start, _end) in zip(positions, pos_spans):
        ctx = norm[max(0, start - 34):start]
        if _NEG_CUE.search(ctx):
            w = weights()
            w[pos] = round(max(0.05, w.get(pos, 1.0) * 0.45), 3)
            labels.append(f"{pos} de-prioritised (pos_weights {pos}={w[pos]})")
            if pos == "RB":
                settings["rb_target_count"] = max(0, strat.rb_target_count - 1)
        elif _MORE_CUE.search(ctx):
            if pos == "RB":
                settings["rb_target_count"] = min(6, strat.rb_target_count + 1)
                labels.append(
                    f"RB goal -> {settings['rb_target_count']} inside the "
                    f"top {strat.rb_target_top_n}")
            else:
                w = weights()
                w[pos] = round(w.get(pos, 1.0) * 1.18, 3)
                labels.append(f"{pos} prioritised (pos_weights {pos}={w[pos]})")

    # --- risk appetite
    if _AGGR_DOWN.search(norm):
        settings["urgency_weight"] = round(strat.urgency_weight * 0.65, 3)
        settings["risk_penalty"] = round(strat.risk_penalty * 1.3, 3)
        labels.append("less aggressive: lower urgency, higher risk penalty")
    elif _AGGR_UP.search(norm):
        settings["urgency_weight"] = round(min(2.0, strat.urgency_weight * 1.4), 3)
        settings["risk_penalty"] = round(strat.risk_penalty * 0.8, 3)
        labels.append("more aggressive: higher urgency, lower risk penalty")

    # --- ADP discipline
    if _REACH_UP.search(norm):
        settings["reach_penalty_per_pick"] = round(
            strat.reach_penalty_per_pick * 1.6, 4)
        settings["reach_free_picks"] = max(0, strat.reach_free_picks - 2)
        labels.append("reaching costs more (higher reach_penalty_per_pick)")
    elif _REACH_DOWN.search(norm):
        settings["reach_penalty_per_pick"] = round(
            strat.reach_penalty_per_pick * 0.6, 4)
        labels.append("reaching costs less (lower reach_penalty_per_pick)")

    # --- ceiling vs floor
    if _UPSIDE_DOWN.search(norm):
        settings["upside_weight_late"] = round(strat.upside_weight_late * 0.75, 3)
        settings["risk_penalty"] = round(strat.risk_penalty * 1.25, 3)
        labels.append("floor over ceiling (lower late upside weight)")
    elif _UPSIDE_UP.search(norm):
        settings["upside_weight_late"] = round(
            min(0.9, strat.upside_weight_late * 1.3), 3)
        labels.append("chase upside (higher late upside weight)")

    if _VALUE_UP.search(norm):
        settings["value_bonus_per_pick"] = round(
            strat.value_bonus_per_pick * 1.3, 4)
        labels.append("fallers are worth more (higher value_bonus_per_pick)")

    # --- QB timing
    if _QB_LATER.search(norm):
        settings["qb_min_round"] = strat.qb_min_round + 2
        labels.append(f"no QB before round {settings['qb_min_round']}")
    elif _QB_SOONER.search(norm):
        settings["qb_min_round"] = max(1, strat.qb_min_round - 2)
        labels.append(f"QB allowed from round {settings['qb_min_round']}")

    # --- explicit "set field value", for when you know the knob by name
    m = _SET_RX.search(norm)
    if m:
        field_name, raw = m.group(1), m.group(2)
        if raw in ("true", "false"):
            value: Any = raw == "true"
        elif "." in raw:
            value = float(raw)
        else:
            value = int(raw)
        settings[field_name] = value
        labels.append(f"{field_name} -> {value}")

    return settings, preset, labels


# ------------------------------------------------------------ player filling


def _plausible_single(token: str) -> bool:
    """Is a one-word span worth trying as a name at all?"""
    return token in NICKNAMES or (len(token) >= 3 and not token.isdigit())


def _span_matches(tokens: list[str], key: str, display: str) -> bool:
    """Guard against the resolver's fuzzy paths inventing a match.

    "jeanty hampton" resolves to Omarion Hampton through the unique-surname
    path, quietly losing Jeanty.  Requiring every token of the span to appear
    in the matched name rejects that and lets the caller fall back to two
    one-word spans, which is what the user actually typed.
    """
    target = normalize(display) or key
    for tok in tokens:
        nt = normalize(tok)
        if not nt:
            continue
        if nt in target or target in nt or nt == key:
            continue
        return False
    return True


def _resolve_player(engine: Any, text: str) -> dict | None:
    """Resolve one candidate span to {key, name, pos, query}, or None."""
    text = text.strip()
    if not text:
        return None
    key = None
    if engine is not None:
        try:
            key = engine.resolve_key(text)
        except Exception:                                  # noqa: BLE001
            key = None
    name = pos = None
    if key and engine is not None:
        proj = engine.projection(key)
        if proj is not None:
            name, pos = proj.name, proj.pos
    if name is None:
        try:
            gp = get_guide().resolve(text)
        except Exception:                                  # noqa: BLE001
            gp = None
        if gp is not None and (key is None or gp.key == key):
            key, name, pos = gp.key, gp.name, gp.pos
    if not key or not name:
        return None
    if not _span_matches(text.split(), key, name):
        return None
    return {"key": key, "name": name, "pos": pos, "query": text}


def _name_runs(masked: str) -> list[list[str]]:
    """Maximal runs of tokens that could belong to a name.

    Anything already consumed by another pass is a MASK character, which both
    removes those words and breaks the surrounding text into segments - that
    is what keeps "olave or nabers" from becoming one four-token span.
    """
    runs: list[list[str]] = []
    for segment in masked.split(MASK):
        current: list[str] = []
        for tok in segment.split():
            tok = tok.strip("'")
            if not tok or tok in STOP or tok.isdigit():
                if current:
                    runs.append(current)
                    current = []
            else:
                current.append(tok)
        if current:
            runs.append(current)
    return runs


def _players_in(engine: Any, masked: str, limit: int = 3) -> list[dict]:
    """Longest-span-first name resolution over the surviving text."""
    found: list[dict] = []
    seen: set[str] = set()
    for run in _name_runs(masked):
        i, n = 0, len(run)
        while i < n and len(found) < limit:
            hit = None
            for j in range(min(n, i + 4), i, -1):
                span = run[i:j]
                if len(span) == 1 and not _plausible_single(span[0]):
                    continue
                hit = _resolve_player(engine, " ".join(span))
                if hit:
                    i = j
                    break
            if hit:
                if hit["key"] not in seen:
                    seen.add(hit["key"])
                    found.append(hit)
            else:
                i += 1
    return found


def _mask(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(max(0, start), min(len(chars), end)):
            chars[i] = MASK
    return "".join(chars)


# ------------------------------------------------------------------- parsing


_NOTE_RX = re.compile(
    r"^(?:note|remember that|remember|make a note that|make a note|"
    r"for the record|log this|write this down|note to self)\b[:,]?\s*")
_DELTA_RX = re.compile(r"(?<!\w)(?:by\s+)?([+-]?\d+(?:\.\d+)?)(?!\w)")


def _confidence(best: float, second: float) -> float:
    """Saturating score, discounted when the runner-up is breathing down its neck.

    A single exact keyword hit (~1.4) lands near 0.8; two corroborating hits
    push past 0.9; a lone weak signal sits near 0.4.  A near-tie costs about a
    fifth of the confidence, which is what "it could have been either" means.
    """
    conf = 1.0 - math.exp(-1.15 * max(0.0, best))
    if second > 0:
        separation = min(1.0, (best - second) / max(0.6, best))
        conf *= 0.78 + 0.22 * separation
    return round(max(0.05, min(0.98, conf)), 3)


def parse(text: str, engine: Any = None) -> Intent:
    """Classify one message and fill its slots.

    `engine` is optional only so the parser can be exercised on its own; with
    an engine it resolves names against the same guide/ADP indexes the rest of
    the bot uses, which is what makes "cmc" and "olave" work.
    """
    raw = text or ""
    norm = _normalise(raw)
    if not norm:
        return Intent("unknown", 0.05, {}, raw)

    positions, pos_spans = _positions(norm)
    numbers, num_spans = _numbers(norm)
    hits = _match_rules(norm)

    consumed = list(pos_spans) + list(num_spans) + [s for _, s in hits]
    players = _players_in(engine, _mask(norm, consumed))

    strat = getattr(engine, "strategy", None) or Strategy()
    settings, preset, labels = _strategy_edits(norm, pos_spans, positions, strat)

    slots: dict[str, Any] = {}
    if players:
        slots["players"] = players
    if positions:
        slots["position"] = positions[0]
        slots["positions"] = list(dict.fromkeys(positions))
    slots.update(numbers)
    if settings:
        slots["settings"] = settings
    if preset:
        slots["preset"] = preset
    if labels:
        slots["edits"] = labels

    # --- score every intent
    scores: dict[str, float] = {}

    def add(intent: str, weight: float) -> None:
        scores[intent] = scores.get(intent, 0.0) + weight

    for rule, _span in hits:
        if rule.requires == "player" and not players:
            continue
        if rule.requires == "players2" and len(players) < 2:
            continue
        if rule.requires == "position" and not positions:
            continue
        add(rule.intent, rule.weight)

    # --- structural evidence from the slot filler
    if len(players) >= 2:
        add("compare", 1.5)
    if players:
        add("why", 0.45)
        add("player_info", 0.5)
        add("availability", 0.2)
    if positions:
        add("recommend", 0.55)
    if settings or preset:
        add("strategy_set", 1.4)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best_name, best_score = ranked[0] if ranked else ("unknown", 0.0)
    second = ranked[1][1] if len(ranked) > 1 else 0.0

    # --- note keeps its payload as free text, so grab it before anything else
    if best_name == "note":
        body = _NOTE_RX.sub("", raw.strip()).strip()
        slots["text"] = body or raw.strip()

    if best_score < MIN_SCORE:
        if players:
            # We know who, not what.  Profiling him answers most of the
            # questions a bare name is standing in for.
            return Intent("player_info", LONE_NAME_CONF, slots, raw)
        return Intent("unknown", _confidence(best_score, 0.0) * 0.4
                      if best_score else 0.1, slots, raw)

    if best_name == "recommend" and positions:
        best_name = "recommend_pos"
    if best_name == "bump":
        m = _DELTA_RX.search(norm)
        slots["value"] = float(m.group(1)) if m else 1.0
    if best_name == "strategy_set" and "field" not in slots and settings:
        first = next(iter(settings))
        slots["field"] = first
        slots["value"] = settings[first]

    return Intent(best_name, _confidence(best_score, second), slots, raw)


# ------------------------------------------------------------------ responses


HELP = """You can just talk to me - no commands to memorise.

  who should I take            top recommendations for this pick
  best RB / top receivers      the same, narrowed to one position
  why him / why olave          the full case for a player
  olave or nabers              head-to-head, with the numbers
  will olave last              chance he survives to your next pick
  how is the board             positional outlook and tier cliffs
  my team / what do I need     your roster and starting-slot gaps
  read the room                what every other manager is doing
  what is my strategy          the knobs I am drafting by
  go zero RB / be aggressive   retune the strategy mid-draft
  tell me about olave          guide profile for any player
  I like tuten / never draft X nudge a player up, or bury him
  note: ...                    log a thought for post-draft review"""


@dataclass
class _Ctx:
    intent: Intent
    engine: Any
    last_recs: list
    sim_runs: int | None

    @property
    def slots(self) -> dict:
        return self.intent.slots

    @property
    def state(self):
        return self.engine.state

    def count(self, default: int = 5) -> int:
        return int(self.slots.get("count") or default)

    def pick_no(self) -> int:
        pick = self.slots.get("pick")
        if pick:
            return int(pick)
        st = self.state
        upcoming = st.my_upcoming_picks(1)
        return upcoming[0] if upcoming else st.next_pick_no

    def recommend(self, n: int, pos: str | None = None) -> list:
        return self.engine.recommend(n, pick_no=self.slots.get("pick"),
                                     pos_filter=pos, sim_runs=self.sim_runs)

    def player(self, index: int = 0) -> dict | None:
        players = self.intent.players
        if len(players) > index:
            return players[index]
        # "why him" after a shortlist means the player we just talked about.
        if index == 0 and self.last_recs:
            top = self.last_recs[0]
            return {"key": top.key, "name": top.name, "pos": top.pos,
                    "query": top.name}
        return None


def _find(recs: list, key: str):
    return next((r for r in recs if r.key == key), None)


def _brief_list(recs: list) -> list[str]:
    return [explain.brief(r, i + 1) for i, r in enumerate(recs)]


def _gaps(engine: Any) -> list[str]:
    """Unfilled starting slots, straight from the league's roster settings."""
    counts = engine.my_counts()
    out = []
    for pos, needed in engine.state.starters.items():
        if pos not in POSITIONS:
            continue
        have = counts.get(pos, 0)
        if have < needed:
            out.append(f"{needed - have} {pos}")
    return out


def _do_recommend(ctx: _Ctx):
    pos = ctx.intent.position if ctx.intent.name == "recommend_pos" else None
    recs = ctx.recommend(ctx.count(), pos)
    pick = ctx.pick_no()
    head = explain.pick_header(ctx.engine, pick)
    if not recs:
        label = f" at {pos}" if pos else ""
        return (f"{head}\n\nNothing left on the board{label}.",
                {"recommendations": [], "pick_no": pick},
                ["How is the board?", "What do I need?"])

    lines = [head, ""]
    if pos:
        lines[1] = f"Best {pos} for this pick:"
        lines.append("")
    lines += _brief_list(recs)
    lines += ["", explain.explain(ctx.engine, recs[0], pick)]

    top = recs[0]
    chips = [f"Why {top.name}?"]
    if len(recs) > 1:
        chips.append(f"{top.name} or {recs[1].name}?")
    chips += ["How is the board?", "What do I need?"]
    return ("\n".join(lines),
            {"recommendations": [r.to_dict() for r in recs], "pick_no": pick},
            chips[:4])


def _do_why(ctx: _Ctx):
    who = ctx.player()
    if who is None:
        return ("Tell me who - \"why olave\" or ask for a recommendation "
                "first and then \"why him\".", None,
                ["Who should I take?", "Best RB left?"])

    rec = _find(ctx.last_recs, who["key"])
    recs = ctx.last_recs
    if rec is None:
        recs = ctx.recommend(30)
        rec = _find(recs, who["key"])
    if rec is not None:
        chips = [f"Will {rec.name} last to my next pick?"]
        other = next((r for r in recs if r.key != rec.key), None)
        if other is not None:
            chips.append(f"{rec.name} or {other.name}?")
        chips.append("Who else should I look at?")
        return (explain.explain(ctx.engine, rec, ctx.pick_no()),
                {"player": rec.to_dict()}, chips[:4])

    # Not on this pick's shortlist: say so plainly rather than inventing a case.
    return _player_blurb(ctx, who, prefix="Not on this pick's shortlist. ")


def _do_compare(ctx: _Ctx):
    players = ctx.intent.players
    if len(players) < 2 and ctx.last_recs:
        top = ctx.last_recs[0]
        if not players or players[0]["key"] != top.key:
            players = players + [{"key": top.key, "name": top.name,
                                  "pos": top.pos, "query": top.name}]
    if len(players) < 2:
        return ("Give me two names - \"olave or nabers\".", None,
                ["Who should I take?", "Best WR left?"])

    a, b = players[0], players[1]
    recs = list(ctx.last_recs)
    if not {a["key"], b["key"]} <= {r.key for r in recs}:
        recs = ctx.recommend(40)
    text = explain.compare(ctx.engine, recs, a["name"], b["name"])
    ra, rb = _find(recs, a["key"]), _find(recs, b["key"])
    data = {"a": ra.to_dict(), "b": rb.to_dict()} if ra and rb else None
    lead = ra if (ra and rb and ra.score >= rb.score) else rb
    chips = [f"Why {lead.name}?"] if lead else []
    chips += [f"Will {b['name']} last?", "Best available?"]
    return (text, data, chips[:4])


def _do_availability(ctx: _Ctx):
    who = ctx.player()
    if who is None:
        return ("Which player - \"will olave last?\"", None,
                ["Who should I take?", "How is the board?"])

    st = ctx.state
    engine = ctx.engine
    name = who["name"]
    if not engine.is_available(who["key"]):
        taken = next((p for p in st.picks
                      if engine.resolve_key(p.name, p.pos) == who["key"]), None)
        where = (f" - taken at pick {taken.pick_no} by slot {taken.slot}"
                 if taken else "")
        return (f"{name} is already off the board{where}.",
                {"player": name, "survival": 0.0, "next_pick": 0},
                ["Who should I take?", "How is the board?"])

    pick = ctx.pick_no()
    ctx.recommend(max(5, ctx.count()))      # populates engine.last_sim
    sim = engine.last_sim
    upcoming = [p for p in st.my_upcoming_picks(3) if p > pick]
    next_pick = upcoming[0] if upcoming else 0
    survival = sim.survival.get(who["key"], 1.0) if sim else 1.0

    if not sim or not sim.runs or not next_pick:
        text = (f"No simulation to run for {name} - you have no pick after "
                f"{pick}, so there is nothing to wait for.")
    else:
        text = (f"{name}: {survival:.0%} chance he is still there at your next "
                f"pick ({next_pick}), across {sim.runs} simulated drafts.")
        gap = next_pick - pick
        text += f"\n  That is {gap} picks away."
        if survival < 0.35:
            text += "\n  Waiting probably costs you him."
        elif survival > 0.75:
            text += "\n  You can most likely take someone else first."
    return (text,
            {"player": name, "survival": round(survival, 3),
             "next_pick": next_pick},
            [f"Why {name}?", "Who should I take?", "How is the board?"])


def _do_board(ctx: _Ctx):
    pick = ctx.pick_no()
    ctx.recommend(max(5, ctx.count()))      # the outlook reads the last sim
    text = explain.board_summary(ctx.engine, pick)
    return (text, {"outlook": ctx.engine.positional_outlook(pick)},
            ["Who should I take?", "Best RB left?", "What do I need?"])


def _do_roster(ctx: _Ctx):
    engine = ctx.engine
    gaps = _gaps(engine)
    text = explain.roster_summary(engine)
    return (text, {"counts": engine.my_counts(), "gaps": gaps},
            ["Who should I take?", "How is the board?",
             "What is my strategy?"])


def _do_room(ctx: _Ctx):
    engine, st = ctx.engine, ctx.state
    slots = []
    for slot, desc in engine.opponents.summary():
        slots.append({
            "slot": slot,
            "label": st.slot_names.get(slot) or f"slot {slot}",
            "is_me": slot == st.my_slot,
            "summary": desc,
            "counts": st.pos_counts(slot),
        })
    return (explain.opponent_summary(engine), {"slots": slots},
            ["Who should I take?", "How is the board?", "What do I need?"])


def _do_strategy_show(ctx: _Ctx):
    return (explain.strategy_summary(ctx.engine), None,
            ["Go zero RB", "Be more aggressive", "Who should I take?"])


def _do_strategy_set(ctx: _Ctx):
    engine = ctx.engine
    changes: list[str] = []
    preset = ctx.slots.get("preset")
    if preset and preset in PRESETS:
        engine.strategy = PRESETS[preset]()
        changes.append(f"strategy -> {preset}")
    settings = ctx.slots.get("settings") or {}
    if settings:
        changes += engine.strategy.adjust(**settings)
    if not changes:
        return ("I did not catch a change to make. Try \"go zero RB\", "
                "\"be more aggressive\", or \"stop recommending tight ends\".",
                {"changes": []},
                ["What is my strategy?", "Who should I take?"])

    engine.store.add_feedback(ctx.state.draft_id, "tune", "; ".join(changes))
    lines = ["Done:"] + [f"  {c}" for c in changes]
    for label in ctx.slots.get("edits") or []:
        lines.append(f"  ({label})")
    lines += ["", explain.strategy_summary(engine)]
    return ("\n".join(lines), {"changes": changes},
            ["Who should I take now?", "What is my strategy?",
             "How is the board?"])


def _player_blurb(ctx: _Ctx, who: dict, prefix: str = ""):
    """Guide profile plus live market state - every figure from the engine."""
    engine = ctx.engine
    key = who["key"]
    proj = engine.projection(key)
    gp = None
    try:
        gp = get_guide().resolve(who["name"], who.get("pos"))
    except Exception:                                      # noqa: BLE001
        gp = None
    if proj is None and gp is None:
        return (f"I have nothing on {who['query']!r}.", None,
                ["Who should I take?", "How is the board?"])

    available = engine.is_available(key)
    adp = engine.adp.adp_of(who["name"], who.get("pos"), fallback=999.0)
    lines = [f"{who['name']} ({who.get('pos') or '-'})"
             + ("" if available else " - ALREADY DRAFTED")]
    if gp is not None:
        for fmt in ("ppr", "half_ppr"):
            if gp.rank(fmt):
                lines.append(f"  guide {fmt}: #{gp.rank(fmt)} overall, "
                             f"{gp.pos}{gp.prank(fmt)}")
        if gp.adj_ppg_2025 is not None:
            lines.append(f"  2025 adjusted PPG: {gp.adj_ppg_2025} "
                         f"({gp.pos}{gp.adj_ppg_rank_2025})")
        if gp.is_rookie:
            lines.append(f"  dynasty rookie #{gp.rookie_rank} "
                         f"({gp.rookie_team})")
    if proj is not None:
        lines.append(f"  projection: {proj.ppg:.1f} PPG "
                     f"(floor {proj.floor:.1f} / ceiling {proj.upside:.1f}), "
                     f"{proj.vor:+.1f} vs {proj.pos} replacement "
                     f"{proj.replacement:.1f}")
        tier = engine.model.tier_of(key)
        if tier:
            lines.append(f"  {proj.pos} tier {tier[0]} ({tier[1]} in it)")
    if adp < 900:
        lines.append(f"  market ADP: {adp:.0f}")
    notes = (proj.notes if proj is not None else None) or (
        gp.notes if gp is not None else [])
    for note in notes[:3]:
        lines.append(explain._wrap(note, "  - "))

    data = {"player": {
        "key": key, "name": who["name"], "pos": who.get("pos"),
        "available": available,
        "guide_overall_rank": (gp.rank(engine.fmt) if gp else None),
        "guide_pos_rank": (gp.prank(engine.fmt) if gp else None),
        "adj_ppg_2025": (gp.adj_ppg_2025 if gp else None),
        "ppg": round(proj.ppg, 2) if proj else None,
        "vor": round(proj.vor, 2) if proj else None,
        "upside": round(proj.upside, 2) if proj else None,
        "floor": round(proj.floor, 2) if proj else None,
        "adp": round(adp, 1) if adp < 900 else None,
        "notes": list(notes[:3]),
    }}
    chips = [f"Why {who['name']}?", f"Will {who['name']} last?",
             "Who should I take?"]
    return (prefix + "\n".join(lines), data, chips)


def _do_player_info(ctx: _Ctx):
    who = ctx.player()
    if who is None:
        return ("Which player did you mean?", None,
                ["Who should I take?", "How is the board?"])
    return _player_blurb(ctx, who)


def _do_bump(ctx: _Ctx):
    who = ctx.player()
    if who is None:
        return ("Who should I move up? Try \"bump tuten 1.5\".", None,
                ["Who should I take?", "What is my strategy?"])
    delta = float(ctx.slots.get("value") or 1.0)
    engine = ctx.engine
    engine.strategy.player_bumps[who["key"]] = delta
    engine.store.set_player_bias(who["key"], delta, "chat bump")
    engine.biases = engine.store.player_biases()
    return (f"{who['name']} adjusted {delta:+.1f} VOR from here on.", None,
            ["Who should I take now?", f"Why {who['name']}?"])


def _do_ban(ctx: _Ctx):
    who = ctx.player()
    if who is None:
        return ("Who should I bury? Try \"never draft X\".", None,
                ["Who should I take?", "What is my strategy?"])
    engine = ctx.engine
    if who["key"] not in engine.strategy.banned:
        engine.strategy.banned.append(who["key"])
    engine.store.add_feedback(ctx.state.draft_id, "ban",
                              f"banned {who['name']}")
    return (f"{who['name']} is off my list - I will not suggest him again.",
            None, ["Who should I take now?", "How is the board?"])


def _do_note(ctx: _Ctx):
    body = (ctx.slots.get("text") or ctx.intent.raw).strip()
    ctx.engine.store.add_feedback(ctx.state.draft_id, "note", body)
    return (f"Noted for post-draft review: {body}", None,
            ["Who should I take?", "What do I need?"])


def _do_help(ctx: _Ctx):
    return (HELP, None,
            ["Who should I take?", "Best RB left?", "How is the board?",
             "What do I need?"])


def _do_greeting(ctx: _Ctx):
    st = ctx.state
    where = f"Pick {st.next_pick_no}, round {st.current_round}"
    if st.my_slot:
        until = st.picks_until_my_turn()
        if until == 0:
            where += " - you are on the clock"
        elif until is not None:
            where += f" - {until} picks until your turn"
    return (f"{where}. Ask me anything, or say help.", None,
            ["Who should I take?", "How is the board?", "What do I need?"])


def _do_unknown(ctx: _Ctx):
    return ("I did not get that. I can recommend a pick, explain one, compare "
            "two players, read the board or the room, check who will last, or "
            "retune the strategy. Say help for the full list.", None,
            ["Who should I take?", "Best WR left?", "How is the board?",
             "What do I need?"])


_HANDLERS: dict[str, Callable[[_Ctx], tuple]] = {
    "recommend": _do_recommend,
    "recommend_pos": _do_recommend,
    "why": _do_why,
    "compare": _do_compare,
    "availability": _do_availability,
    "board": _do_board,
    "roster": _do_roster,
    "room": _do_room,
    "strategy_show": _do_strategy_show,
    "strategy_set": _do_strategy_set,
    "player_info": _do_player_info,
    "bump": _do_bump,
    "ban": _do_ban,
    "note": _do_note,
    "help": _do_help,
    "greeting": _do_greeting,
    "unknown": _do_unknown,
}


def respond(intent: Intent, engine: Any, last_recs: list | None = None,
            sim_runs: int | None = None
            ) -> tuple[str, dict | None, list[str]]:
    """Execute a parsed intent against the engine.

    Returns (text, data, suggestions) where `data` is the structured payload
    the /api/chat contract defines for that intent, and `suggestions` are
    follow-up questions worth showing as chips.  `sim_runs` is a test and
    latency escape hatch; None means the strategy's own setting.
    """
    ctx = _Ctx(intent=intent, engine=engine, last_recs=list(last_recs or []),
               sim_runs=sim_runs)
    handler = _HANDLERS.get(intent.name, _do_unknown)
    text, data, chips = handler(ctx)
    return text, data, list(chips)[:4]


def chat(engine: Any, message: str, last_recs: list | None = None,
         sim_runs: int | None = None) -> dict:
    """Parse and answer in one call, shaped like the /api/chat response body."""
    intent = parse(message, engine)
    text, data, chips = respond(intent, engine, last_recs, sim_runs=sim_runs)
    return {
        "output": text,
        "intent": intent.name,
        "confidence": intent.confidence,
        "understood": intent.understood,
        "engine": ENGINE_NAME,
        "data": data,
        "suggestions": chips,
    }
