"""Natural-language layer tests.

The NLU is what stands between a person typing "im thin at wr right" mid-draft
and a useful answer, so the cases below are deliberately messy: contractions,
typos, fragments and negation.  Everything runs against an offline mock draft -
no network, no model.
"""

from __future__ import annotations

import random

import pytest

from ffbot import nlu
from ffbot.engine import Engine
from ffbot.mockdraft import autopick, synthetic_state
from ffbot.strategy import Strategy


@pytest.fixture(scope="module")
def engine() -> Engine:
    """A 12-team PPR mock advanced into round 5, so a real board exists."""
    state = synthetic_state(teams=12, rounds=15, my_slot=5)
    eng = Engine(state)
    autopick(eng, 53, rng=random.Random(7))
    return eng


def intent_of(engine: Engine, text: str) -> str:
    return nlu.parse(text, engine).name


# ------------------------------------------------------------------ intents


@pytest.mark.parametrize("text,expected", [
    ("who should I take", "recommend"),
    ("what now", "recommend"),
    ("best available", "recommend"),
    ("who do you like", "recommend"),
    ("help me pick", "recommend"),
    ("best rb", "recommend_pos"),
    ("top receivers", "recommend_pos"),
    ("show me running backs", "recommend_pos"),
    ("any good qbs left", "recommend_pos"),
    ("how's the board look", "board"),
    ("what do I need", "roster"),
    ("my team", "roster"),
    ("read the room", "room"),
    ("what are the others doing", "room"),
    ("what can you do", "help"),
    ("hey", "greeting"),
])
def test_intent_classification(engine, text, expected):
    assert intent_of(engine, text) == expected


def test_gibberish_is_unknown_not_a_wrong_answer(engine):
    """A bad parse must say so rather than confidently answering something."""
    parsed = nlu.parse("asdfghjkl", engine)
    assert parsed.name == "unknown"
    assert parsed.confidence < 0.4


# ------------------------------------------------- players and negation


def test_resolves_surname_only(engine):
    parsed = nlu.parse("why olave", engine)
    assert parsed.name == "why"
    assert parsed.slots["players"][0]["key"] == "chrisolave"


def test_resolves_nickname(engine):
    parsed = nlu.parse("tell me about CMC", engine)
    keys = [p["key"] for p in parsed.slots.get("players") or []]
    assert "christianmccaffrey" in keys


def test_compare_extracts_both_players(engine):
    """The classic failure is silently keeping one side of an either/or."""
    parsed = nlu.parse("olave or nabers", engine)
    assert parsed.name == "compare"
    keys = [p["key"] for p in parsed.slots["players"]]
    assert "chrisolave" in keys and "maliknabers" in keys


def test_compare_vs_phrasing(engine):
    parsed = nlu.parse("compare bijan robinson vs jahmyr gibbs", engine)
    assert parsed.name == "compare"
    assert len(parsed.slots["players"]) == 2


def test_negation_is_a_ban_not_a_draft(engine):
    """"I don't want X" must never be read as interest in X."""
    for text in ("i don't want kamara", "never draft kamara", "avoid kamara"):
        parsed = nlu.parse(text, engine)
        assert parsed.name == "ban", text
        assert parsed.slots["players"][0]["key"] == "alvinkamara"


def test_position_words_are_not_eaten_as_names(engine):
    """"tight end" is a position, not a player called Tight End."""
    parsed = nlu.parse("who is the best tight end", engine)
    assert parsed.name == "recommend_pos"
    assert parsed.slots.get("position") == "TE"
    assert not parsed.slots.get("players")


# ------------------------------------------------------- slots and counts


@pytest.mark.parametrize("text,pos", [
    ("best rb", "RB"), ("top qb", "QB"), ("any wideouts", "WR"),
    ("who is the best tight end", "TE"),
])
def test_position_extraction(engine, text, pos):
    assert nlu.parse(text, engine).slots.get("position") == pos


def test_count_extraction(engine):
    parsed = nlu.parse("give me 8 options", engine)
    assert parsed.slots.get("count") == 8


def test_availability_intent_and_player(engine):
    parsed = nlu.parse("will olave last", engine)
    assert parsed.name == "availability"
    assert parsed.slots["players"][0]["key"] == "chrisolave"


# ------------------------------------------------------------- strategy


def test_preset_phrasing_maps_to_real_preset(engine):
    parsed = nlu.parse("go zero rb", engine)
    assert parsed.name == "strategy_set"
    assert parsed.slots.get("preset") == "zero_rb"


@pytest.mark.parametrize("text", [
    "be more aggressive", "draft more rbs", "stop recommending tight ends",
])
def test_strategy_edits_name_real_fields(engine, text):
    """Whatever it decides to change must be a field Strategy actually has."""
    parsed = nlu.parse(text, engine)
    assert parsed.name == "strategy_set"
    settings = parsed.slots.get("settings") or {}
    known = set(Strategy.__dataclass_fields__)
    assert settings, f"no settings extracted from {text!r}"
    assert set(settings) <= known, f"unknown fields: {set(settings) - known}"


# ------------------------------------- regressions fixed after live testing


@pytest.mark.parametrize("text", [
    "anyone running rbs", "are people taking rbs", "any run on running backs",
])
def test_run_questions_read_the_room(engine, text):
    """These asked about the room but were answering with a shortlist."""
    assert intent_of(engine, text) == "room"


@pytest.mark.parametrize("text", [
    "im thin at wr right", "i'm thin at te", "am i thin at rb",
])
def test_contracted_thin_is_a_roster_question(engine, text):
    assert intent_of(engine, text) == "roster"


# --------------------------------------------------------------- responses


def test_respond_is_grounded_and_shaped(engine):
    """respond() must return usable text plus chips, and never crash."""
    for text in ("who should I take", "best rb", "my team", "read the room",
                 "how's the board look", "will olave last"):
        parsed = nlu.parse(text, engine)
        out, data, suggestions = nlu.respond(parsed, engine, [])
        assert isinstance(out, str) and out.strip(), text
        assert isinstance(suggestions, list)
        assert len(suggestions) <= 4


def test_recommend_response_carries_real_recommendations(engine):
    parsed = nlu.parse("who should I take", engine)
    _out, data, _sug = nlu.respond(parsed, engine, [])
    recs = (data or {}).get("recommendations") or []
    assert recs, "recommend produced no structured recommendations"
    assert recs[0]["reasons"], "a recommendation with no reasoning is useless"
    drafted = {engine.resolve_key(p.name, p.pos) for p in engine.state.picks}
    assert recs[0]["key"] not in drafted, "recommended an already-drafted player"


def test_unknown_response_admits_it(engine):
    parsed = nlu.parse("asdfghjkl", engine)
    out, _data, _sug = nlu.respond(parsed, engine, [])
    assert out.strip()
    assert "did not get" in out.lower() or "didn't get" in out.lower()
