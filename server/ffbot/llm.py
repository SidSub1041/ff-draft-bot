"""Optional conversational layer - it rephrases, it never decides.

Every number in this program comes from ffbot.engine: the projections, the
Monte Carlo survival curves, the strategy weights, the pick itself.  This
module exists only to smooth the edges of the conversation, and it has
exactly two jobs:

  * assist_parse - when the built-in NLU cannot tell what a question meant,
    ask a model to choose one intent from a fixed list and pull out slots.
    The answer is validated against that list before anything acts on it.
  * phrase - restate an answer the engine already computed in friendlier
    prose, under a prompt that forbids adding any player, number or piece of
    advice that was not in the facts handed over.
  * deep_answer - answer an open-ended question ("should I trade back?",
    "how do I attack the next three rounds?") from a fact sheet the engine
    just produced.  The model reasons over those facts and only those facts;
    it still cannot mint a projection, a survival probability or a rank.

None of these jobs can change which player the engine recommends.  If the
model is missing, slow, or answers with something malformed, every function
here returns None and the caller falls back to the built-in text - which is
why this file can drop out of the request path entirely and nothing
downstream notices.

Anthropic calls go through the official SDK when it is installed
(pip install anthropic - the `llm` extra); the raw-HTTP fallback keeps the
module import-safe on a machine with nothing but the stdlib.

Detection order is Ollama first (local, free, private), then
ANTHROPIC_API_KEY, then OPENAI_API_KEY.  FFBOT_LLM=off disables the module;
FFBOT_LLM=<name> forces one backend.  With nothing installed - the normal
case on this machine - detect() returns None after a single refused
connection to localhost and the panel runs entirely on the built-in path.

Environment:
  FFBOT_LLM          off | auto | ollama | anthropic | openai   (default auto)
  FFBOT_OLLAMA_URL   base URL of the Ollama server
  FFBOT_LLM_MODEL    override the model name for whichever backend wins
  FFBOT_LLM_TIMEOUT  seconds allowed for one generation call
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from dataclasses import dataclass
from typing import Any

try:                                    # optional: pip install anthropic
    import anthropic as _sdk
except ImportError:                     # stdlib-only installs still work
    _sdk = None

# Detection has to be fast enough that a request thread can pay for it once
# without the panel feeling it.  Generation gets longer, but still bounded:
# a draft clock does not wait for a language model.
PROBE_TIMEOUT = 1.5
PARSE_TIMEOUT = 6.0
PHRASE_TIMEOUT = 8.0
DEEP_TIMEOUT = 30.0
DEEP_MAX_TOKENS = 1200
MAX_HISTORY_TURNS = 12

DEFAULT_OLLAMA = "http://127.0.0.1:11434"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Both jobs here are one-sentence work, so the smallest local model that can
# follow a JSON instruction wins.  Order is preference, not quality.
OLLAMA_PREFERRED = ("llama3.2", "qwen", "phi", "mistral")

OFF_VALUES = ("off", "no", "none", "0", "false", "disabled")
AUTO_VALUES = ("", "auto", "on", "yes", "1", "true")
KINDS = ("ollama", "anthropic", "openai")

# The intent vocabulary is pinned by the panel's chat contract.  A model that
# answers with anything outside this list is discarded rather than guessed at,
# because a wrong intent silently answers the wrong question.
INTENTS = (
    "recommend",
    "recommend_pos",
    "why",
    "compare",
    "availability",
    "board",
    "roster",
    "room",
    "strategy_set",
    "player_info",
    "help",
    "unknown",
)

# Slots worth extracting.  Anything else the model volunteers is dropped.
SLOT_NAMES = ("player", "player_b", "pos", "n", "pick_no", "field", "value",
              "preset")
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF", "FLEX")

MAX_SLOT_CHARS = 60
MAX_PHRASE_CHARS = 2000


# ----------------------------------------------------------------- transport


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _timeout(default: float) -> float:
    """Generation deadline, overridable but never unbounded."""
    raw = _env("FFBOT_LLM_TIMEOUT")
    if not raw:
        return default
    try:
        return max(0.5, min(60.0, float(raw)))
    except ValueError:
        return default


def _request(url: str, headers: dict[str, str], timeout: float,
             payload: dict | None = None) -> dict | None:
    """One JSON round trip that cannot fail loudly.

    Every caller in this module treats None as "no model available", so a
    refused connection, a timeout, an HTTP error and a garbage body all have
    to arrive at the same place.
    """
    data = None
    head = dict(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        head["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=head)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(1_000_000)
        parsed = json.loads(body.decode("utf-8", "replace"))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


# ------------------------------------------------------------------ backend


@dataclass(frozen=True)
class Backend:
    """A model we can reach.  Holds no secret - keys are read at call time."""

    kind: str
    model: str
    url: str = ""       # base URL, Ollama only

    @property
    def label(self) -> str:
        """What the chat contract reports in its `engine` field."""
        return f"{self.kind}:{self.model}"

    def complete(self, system: str, user: str, timeout: float,
                 max_tokens: int = 400) -> str | None:
        """One single-turn completion, or None on any failure."""
        return self.chat(system, [{"role": "user", "content": user}],
                         timeout, max_tokens)

    def chat(self, system: str, messages: list[dict], timeout: float,
             max_tokens: int = 400) -> str | None:
        """A multi-turn completion, or None on any failure.

        `messages` is a plain [{"role": "user"|"assistant", "content": str}]
        history ending with the current user turn.
        """
        try:
            if self.kind == "ollama":
                return self._ollama(system, messages, timeout, max_tokens)
            if self.kind == "anthropic":
                return self._anthropic(system, messages, timeout, max_tokens)
            if self.kind == "openai":
                return self._openai(system, messages, timeout, max_tokens)
        except Exception:
            return None
        return None

    def _ollama(self, system: str, messages: list[dict], timeout: float,
                max_tokens: int) -> str | None:
        data = _request(
            f"{self.url}/api/chat", {}, timeout,
            {"model": self.model, "stream": False,
             "messages": [{"role": "system", "content": system}] + messages,
             "options": {"temperature": 0, "num_predict": max_tokens}})
        if not data:
            return None
        msg = data.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
        # /api/generate shape, in case a proxy rewrote the call.
        return data["response"] if isinstance(data.get("response"), str) else None

    def _anthropic(self, system: str, messages: list[dict], timeout: float,
                   max_tokens: int) -> str | None:
        """Claude via the official SDK, raw HTTP when it is not installed.

        No `temperature`: the parameter was removed on the Claude 5 family
        and sending it is a 400, so the request stays valid whichever model
        FFBOT_LLM_MODEL points at.
        """
        if _sdk is not None:
            return self._anthropic_sdk(system, messages, timeout, max_tokens)
        key = _env("ANTHROPIC_API_KEY")
        if not key:
            return None
        data = _request(
            ANTHROPIC_URL,
            {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
            timeout,
            {"model": self.model, "max_tokens": max_tokens,
             "system": system, "messages": messages})
        if not data or data.get("stop_reason") == "refusal":
            return None
        parts = [b.get("text", "") for b in data.get("content") or []
                 if isinstance(b, dict) and b.get("type") == "text"]
        text = "".join(parts)
        return text or None

    def _anthropic_sdk(self, system: str, messages: list[dict],
                       timeout: float, max_tokens: int) -> str | None:
        client = _anthropic_client()
        if client is None:
            return None
        client = client.with_options(timeout=timeout, max_retries=1)
        # The stable system prompt is the cacheable prefix; the volatile fact
        # sheet rides in the user turn, after the last cache breakpoint.
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        effort = _env("FFBOT_LLM_EFFORT") or "low"
        try:
            # Server-side refusal fallbacks: if the model declines, the API
            # retries the same request on a fallback model inside one call.
            resp = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": effort},
                **kwargs)
        except (TypeError, _sdk.BadRequestError):
            # An older SDK or a model that rejects one of the extras
            # (fallbacks, output_config) - retry with the bare request.
            try:
                resp = client.messages.create(**kwargs)
            except Exception:
                return None
        except Exception:
            return None
        if getattr(resp, "stop_reason", None) == "refusal":
            return None
        parts = [b.text for b in resp.content
                 if getattr(b, "type", "") == "text"]
        text = "".join(parts)
        return text or None

    def _openai(self, system: str, messages: list[dict], timeout: float,
                max_tokens: int) -> str | None:
        key = _env("OPENAI_API_KEY")
        if not key:
            return None
        data = _request(
            OPENAI_URL, {"Authorization": f"Bearer {key}"}, timeout,
            {"model": self.model, "temperature": 0,
             "max_tokens": max_tokens,
             "messages": [{"role": "system", "content": system}] + messages})
        if not data:
            return None
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return None
        msg = choices[0].get("message") or {}
        text = msg.get("content") if isinstance(msg, dict) else None
        return text if isinstance(text, str) and text else None


_client_lock = threading.Lock()
_client: Any = None


def _anthropic_client() -> Any:
    """One SDK client per process.  Reads ANTHROPIC_API_KEY (and honours
    ANTHROPIC_BASE_URL, which is also how the tests point it at a stub)."""
    global _client
    if _sdk is None or not _env("ANTHROPIC_API_KEY"):
        return None
    with _client_lock:
        if _client is None:
            try:
                _client = _sdk.Anthropic()
            except Exception:
                return None
        return _client


# ---------------------------------------------------------------- detection


_detect_lock = threading.Lock()
_detected: Backend | None = None
_detect_done = False


def detect() -> Backend | None:
    """The backend to use, or None for the built-in path.  Probed once.

    Called from request threads, so the result is memoised for the life of
    the process: the panel polls, and re-probing a socket every few seconds
    to learn the same "nothing there" would be pure latency.  Restart the
    server after installing Ollama.
    """
    global _detected, _detect_done
    with _detect_lock:
        if _detect_done:
            return _detected
        try:
            _detected = _probe()
        except Exception:
            _detected = None
        _detect_done = True
        return _detected


def reset() -> None:
    """Forget the probe.  For tests, and for the rare mid-session install."""
    global _detected, _detect_done, _client
    with _detect_lock:
        _detected = None
        _detect_done = False
    with _client_lock:
        _client = None


def backend_label() -> str:
    """`engine` field value for whatever is available right now."""
    b = detect()
    return b.label if b else "builtin"


def available() -> bool:
    return detect() is not None


def _probe() -> Backend | None:
    choice = _env("FFBOT_LLM").lower()
    if choice in OFF_VALUES:
        return None
    if choice in KINDS:
        order = (choice,)
    elif choice in AUTO_VALUES:
        order = KINDS
    else:
        # An unrecognised name is a typo, and quietly falling back to some
        # other backend would hide it.  Stay on the built-in path.
        return None
    for kind in order:
        found = _PROBES[kind]()
        if found is not None:
            return found
    return None


def _probe_ollama() -> Backend | None:
    base = (_env("FFBOT_OLLAMA_URL") or DEFAULT_OLLAMA).rstrip("/")
    data = _request(f"{base}/api/tags", {}, PROBE_TIMEOUT)
    if not data:
        return None
    models = [m for m in data.get("models") or [] if isinstance(m, dict)]
    named = [(str(m.get("name") or m.get("model") or ""), m) for m in models]
    named = [(n, m) for n, m in named if n]
    if not named:
        return None
    forced = _env("FFBOT_LLM_MODEL")
    if forced:
        # Trust the override even if /api/tags did not list it; a pull may be
        # in flight and the failure mode is a dead call, which degrades fine.
        return Backend("ollama", forced, base)
    return Backend("ollama", _pick_ollama_model(named), base)


def _pick_ollama_model(named: list[tuple[str, dict]]) -> str:
    """Smallest model from the most preferred family that is installed.

    On-disk size is the honest proxy for how long a reply will take, and this
    module is only ever asked to classify a sentence or reword a paragraph -
    a 70B model would be a worse answer, not a better one, because it would
    miss the draft clock.
    """
    def rank(item: tuple[str, dict]) -> tuple[int, float]:
        name, meta = item
        low = name.lower()
        family = len(OLLAMA_PREFERRED)
        for i, want in enumerate(OLLAMA_PREFERRED):
            if want in low:
                family = i
                break
        size = meta.get("size")
        return (family, float(size) if isinstance(size, (int, float)) else 1e18)

    return sorted(named, key=rank)[0][0]


def _probe_anthropic() -> Backend | None:
    """Presence of a key is the whole probe - validating it costs a request.

    A key that is present but wrong shows up as a failed completion later,
    and a failed completion already means "use the built-in text".
    """
    if not _env("ANTHROPIC_API_KEY"):
        return None
    return Backend("anthropic", _env("FFBOT_LLM_MODEL")
                   or DEFAULT_ANTHROPIC_MODEL)


def _probe_openai() -> Backend | None:
    if not _env("OPENAI_API_KEY"):
        return None
    return Backend("openai", _env("FFBOT_LLM_MODEL") or DEFAULT_OPENAI_MODEL)


_PROBES = {
    "ollama": _probe_ollama,
    "anthropic": _probe_anthropic,
    "openai": _probe_openai,
}


# ------------------------------------------------------------- json salvage


_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", _FENCE.sub("", text)).strip()


def _json_object(text: str | None) -> dict | None:
    """The first JSON object in a reply, however it was wrapped."""
    if not text:
        return None
    body = _strip_fences(text)
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(body[start:end + 1])
        except (ValueError, TypeError):
            return None
    return parsed if isinstance(parsed, dict) else None


# ------------------------------------------------------------ assist_parse


_PARSE_SYSTEM = f"""\
You label questions asked during a fantasy football draft. You do not answer
them and you do not give draft advice - another program does that.

Reply with exactly one JSON object and nothing else, in this shape:
  {{"intent": "<one of the list>", "confidence": 0.0-1.0, "slots": {{...}}}}

intent must be exactly one of: {", ".join(INTENTS)}

What they mean:
  recommend      who should I take, best available, general advice
  recommend_pos  the same, restricted to one position
  why            justify one specific player
  compare        weigh two named players against each other
  availability   will a named player still be there at my next pick
  board          the state of the draft board or positional runs
  roster         what my own team has and needs
  room           what the other drafters are doing
  strategy_set   change a setting, preset or weighting
  player_info    facts about one player
  help           what can you do
  unknown        anything else, or too vague to place

slots may contain only these keys, all optional:
  player, player_b (player names exactly as the user typed them),
  pos (QB RB WR TE K DEF FLEX), n (how many, a number),
  pick_no (a pick number), field, value (for strategy_set), preset

Never invent a player name. If the user did not name one, leave the slot out.
If nothing fits, answer intent "unknown" with confidence 0.
"""


def assist_parse(text: str, context: dict | None = None) -> dict | None:
    """Second opinion on an ambiguous question, or None.

    Called only when the built-in NLU is already unsure, and the result is a
    label plus slots - never an answer.  Returns
    {"intent": str, "confidence": float, "slots": dict} once the reply has
    survived validation, otherwise None so the caller keeps its own parse.
    """
    try:
        question = (text or "").strip()
        if not question:
            return None
        backend = detect()
        if backend is None:
            return None
        raw = backend.complete(_PARSE_SYSTEM, _parse_prompt(question, context),
                               _timeout(PARSE_TIMEOUT), max_tokens=300)
        return _validate_parse(raw)
    except Exception:
        return None


def _parse_prompt(question: str, context: dict | None) -> str:
    """The question, plus just enough board state to disambiguate a name."""
    lines = [f"Question: {question}"]
    ctx = context or {}
    if ctx:
        trimmed = {k: v for k, v in ctx.items() if v not in (None, [], {}, "")}
        if trimmed:
            lines.append("")
            lines.append("Draft context (facts only, do not repeat back):")
            lines.append(json.dumps(trimmed, default=str)[:2000])
    lines.append("")
    lines.append("JSON:")
    return "\n".join(lines)


def _validate_parse(raw: str | None) -> dict | None:
    """Hard validation.  Anything off-contract is thrown away, not repaired."""
    obj = _json_object(raw)
    if obj is None:
        return None
    name = str(obj.get("intent") or "").strip().lower()
    if name not in INTENTS:
        return None
    conf = obj.get("confidence", 0.5)
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    slots_in = obj.get("slots")
    if slots_in is None:
        slots_in = {}
    if not isinstance(slots_in, dict):
        return None
    return {"intent": name, "confidence": conf, "slots": _clean_slots(slots_in)}


def _clean_slots(raw: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in raw.items():
        key = str(key).strip().lower()
        if key not in SLOT_NAMES or val is None or isinstance(val, bool):
            continue
        if key in ("n", "pick_no"):
            try:
                num = int(float(val))
            except (TypeError, ValueError):
                continue
            if 1 <= num <= 1000:
                out[key] = num
            continue
        if key == "pos":
            pos = str(val).strip().upper()
            if pos in POSITIONS:
                out[key] = pos
            continue
        if isinstance(val, (int, float)):
            out[key] = val
            continue
        if isinstance(val, str):
            text = val.strip()
            if text and len(text) <= MAX_SLOT_CHARS:
                out[key] = text
        # Lists and objects are never a slot value here, so they are dropped.
    return out


# ------------------------------------------------------------------ phrase


_PHRASE_SYSTEM = """\
You are the voice of a fantasy football draft assistant. You are handed the
assistant's own finished answer and you restate it conversationally.

Absolute rules:
  - Use only the facts given. Never add a player, team, statistic, rank,
    probability or recommendation that is not in them.
  - Never change a number, a name or which player is being recommended.
  - Never add advice, caveats or opinions of your own.
  - Drop nothing important. If you cannot restate a fact faithfully, keep the
    original wording for it.
  - No preamble, no sign-off, no markdown headings, no bullet characters
    other than a leading "- ". Plain sentences.
  - Two to six sentences. Shorter than the facts you were given.

Reply with the restatement only.
"""


def phrase(answer_facts: Any, question: str = "") -> str | None:
    """Reword a computed answer, or None to use the original text.

    The facts are the product; this only changes how they read.  Padding is
    treated as invention: a reply meaningfully longer than its source has
    almost certainly added something, so it is rejected.
    """
    try:
        facts = answer_facts if isinstance(answer_facts, str) else \
            json.dumps(answer_facts, default=str)
        facts = (facts or "").strip()
        if not facts:
            return None
        backend = detect()
        if backend is None:
            return None
        prompt = (f"Question asked: {question.strip() or '(none)'}\n\n"
                  f"Assistant's factual answer:\n{facts[:4000]}\n\n"
                  f"Restatement:")
        raw = backend.complete(_PHRASE_SYSTEM, prompt,
                               _timeout(PHRASE_TIMEOUT), max_tokens=500)
        return _validate_phrase(raw, facts)
    except Exception:
        return None


def _validate_phrase(raw: str | None, facts: str) -> str | None:
    if not raw:
        return None
    out = _strip_fences(raw).strip().strip('"').strip()
    if not out or len(out) > MAX_PHRASE_CHARS:
        return None
    if out.lstrip().startswith("{"):
        return None                      # answered with JSON, not prose
    # Room to reorganise, not room to add a paragraph of invented colour.
    if len(out) > 2.2 * len(facts) + 200:
        return None
    return out


# ------------------------------------------------------------- deep_answer


_DEEP_SYSTEM = """\
You are the analyst inside a fantasy football draft assistant, talking to a
drafter who is mid-draft and short on time.

You are handed a FACT SHEET produced seconds ago by the assistant's own
statistical engine: the draft state, the drafter's roster, the engine's
current recommendations with their scored reasons, projections, ADP and
survival probabilities, positional outlooks, a read on the other drafters,
the active strategy, and relevant notes from Joel Smyth's 2026 draft guide.

Rules, in order:
  1. Ground every claim in the fact sheet. Never invent or adjust a player,
     projection, rank, ADP, probability or statistic. If a number is not in
     the facts, you do not know it.
  2. The engine's numbers are the evidence; your job is the judgement call
     the numbers do not make by themselves - weighing roster construction,
     risk, timing and the drafter's stated intent against each other.
  3. Be decisive. Lead with the answer ("Take Olave", "Wait on QB"), then
     the two or three facts that argue for it, then the strongest fact
     against it if there is one.
  4. If the facts genuinely do not cover the question, say exactly that and
     name the closest thing they do cover.
  5. Plain text, no markdown headings.
  6. FORMAT, always: first a VERDICT of one to three short sentences - the
     call and the single strongest reason. The drafter may have thirty
     seconds on the clock and reads this part only. Then, on its own line,
     the exact marker ---MORE--- and after it the fuller analysis (the
     supporting numbers, the counter-argument, the contingency) in up to
     eight sentences. If the question genuinely needs only one line, put
     "Nothing more to add." after the marker.
"""

MORE_MARKER = "---MORE---"


_LABEL = re.compile(r"^\s*(verdict|answer|call)\s*:\s*", re.I)


def split_deep(text: str) -> tuple[str, str]:
    """(verdict, detail) - detail may be empty when the model had no more."""
    text = _LABEL.sub("", text.strip())
    if MORE_MARKER in text:
        head, _, tail = text.partition(MORE_MARKER)
        head, tail = head.strip(), tail.strip()
        if tail.lower().rstrip(".") == "nothing more to add":
            tail = ""
        if head:
            return head, tail
    return text.strip(), ""


def deep_answer(question: str, facts: str,
                history: list[dict] | None = None) -> str | None:
    """A grounded free-form answer, or None to use the built-in text.

    `facts` is the engine-produced sheet this answer must stay inside.
    `history` is prior chat turns [{"role", "content"}] for follow-ups
    ("what about the round after?"); it is bounded here, not trusted.
    """
    try:
        question = (question or "").strip()
        facts = (facts or "").strip()
        if not question or not facts:
            return None
        backend = detect()
        if backend is None:
            return None

        msgs: list[dict] = []
        for turn in (history or [])[-MAX_HISTORY_TURNS:]:
            role = turn.get("role")
            content = str(turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                msgs.append({"role": role, "content": content[:1500]})
        # History must alternate sanely and end before the current turn; a
        # malformed tail (two users in a row) is legal for the API, so no
        # repair is needed - only bounding.
        msgs.append({
            "role": "user",
            "content": (f"FACT SHEET (produced just now - answer only "
                        f"from this):\n{facts[:9000]}\n\n"
                        f"Question: {question}"),
        })
        raw = backend.chat(_DEEP_SYSTEM, msgs, _timeout(DEEP_TIMEOUT),
                           max_tokens=DEEP_MAX_TOKENS)
        if not raw:
            return None
        out = _strip_fences(raw).strip()
        if not out or len(out) > 6000:
            return None
        if out.lstrip().startswith("{"):
            return None                 # answered with JSON, not prose
        return out
    except Exception:
        return None
