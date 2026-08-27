"""deep_answer grounding tests, against a stub Messages API.

No real credits are spent: the SDK honours ANTHROPIC_BASE_URL, so a local
HTTP stub plays the API and the tests assert on what was actually sent -
the grounding contract is "the facts travelled with the question".
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ffbot import llm


class _StubAPI(BaseHTTPRequestHandler):
    """Plays api.anthropic.com. The next canned reply is set per test."""

    reply: dict = {}
    seen: list[dict] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            _StubAPI.seen.append(json.loads(body))
        except ValueError:
            _StubAPI.seen.append({})
        out = json.dumps(_StubAPI.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def _message(text, stop="end_turn"):
    return {
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


@pytest.fixture()
def stub(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubAPI)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL",
                       f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("FFBOT_LLM", "anthropic")
    monkeypatch.setenv("FFBOT_LLM_MODEL", "claude-opus-5")
    monkeypatch.setenv("FFBOT_LLM_TIMEOUT", "5")
    llm.reset()
    _StubAPI.seen = []
    yield _StubAPI
    srv.shutdown()
    llm.reset()


FACTS = ("Pick 53 (Round 5, slot 5) | Engine recommendations: "
         "1. Emeka Egbuka (WR) score 12.42, 13.3 PPG, ADP 35, 0% lasts.")


def test_answer_comes_back_and_facts_travelled(stub):
    stub.reply = _message("Take Egbuka - 0% chance he lasts to 68.")
    out = llm.deep_answer("should i take egbuka or wait", FACTS, [])
    assert out == "Take Egbuka - 0% chance he lasts to 68."
    sent = stub.seen[-1]
    text = json.dumps(sent)
    assert "Egbuka" in text and "ADP 35" in text, \
        "the fact sheet must travel with the question"
    assert "should i take egbuka" in text


def test_history_rides_along_bounded(stub):
    stub.reply = _message("Still Egbuka.")
    history = [{"role": "user", "content": f"turn {i}"} for i in range(40)]
    out = llm.deep_answer("and now?", FACTS, history)
    assert out == "Still Egbuka."
    msgs = stub.seen[-1]["messages"]
    # bounded to MAX_HISTORY_TURNS plus the live turn
    assert len(msgs) <= llm.MAX_HISTORY_TURNS + 1


def test_refusal_falls_back_to_builtin(stub):
    stub.reply = _message("", stop="refusal")
    assert llm.deep_answer("question", FACTS, []) is None


def test_json_shaped_reply_is_rejected(stub):
    stub.reply = _message('{"intent": "recommend"}')
    assert llm.deep_answer("question", FACTS, []) is None


def test_no_facts_means_no_call(stub):
    assert llm.deep_answer("question", "", []) is None
    assert not stub.seen


def test_dead_endpoint_degrades_silently(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")  # nothing
    monkeypatch.setenv("FFBOT_LLM", "anthropic")
    monkeypatch.setenv("FFBOT_LLM_TIMEOUT", "1")
    llm.reset()
    assert llm.deep_answer("question", FACTS, []) is None
    llm.reset()


def test_nothing_installed_stays_builtin(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FFBOT_LLM", "anthropic")
    llm.reset()
    assert llm.detect() is None
    assert llm.deep_answer("question", FACTS, []) is None
    llm.reset()


def test_followup_routing_prefers_model_over_wrong_referent():
    """"and if he is gone at 68" must not be answered by the builtin path
    resolving "he" to whoever tops the last list - with history present it
    belongs to the model, which holds the conversation."""
    from ffbot import server

    class FakeIntent:
        slots: dict = {}

    fi = FakeIntent()
    assert server._followup_shaped("and if he is gone at 68 what is my pivot", fi)
    assert server._followup_shaped("what about the round after", fi)
    assert server._followup_shaped("ok but why him over the te", fi)
    assert not server._followup_shaped("best rb", fi)
    fi.slots = {"players": [{"name": "Chris Olave"}]}
    assert not server._followup_shaped("will olave last", fi), \
        "a named player is not a dangling referent"
