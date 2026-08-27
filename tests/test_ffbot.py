"""Regression tests.

Everything here runs offline except the Sleeper-marked tests, which are skipped
when the cached player file is missing.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from ffbot.adp import AdpModel
from ffbot.curves import LogLogCurve, PowerLawFit
from ffbot.guide import get_guide
from ffbot.model import ValueModel
from ffbot.names import initial_key, normalize
from ffbot.sleeper import DraftState, extract_draft_id
from ffbot.strategy import PRESETS, Strategy, for_league

CACHE = Path(__file__).resolve().parents[1] / "data" / "cache" / "players_nfl.json"
needs_sleeper = pytest.mark.skipif(
    not CACHE.exists(), reason="no cached Sleeper player file")


# ------------------------------------------------------------------- names


def test_normalize_strips_suffixes_and_punctuation():
    assert normalize("Deebo Samuel Sr.") == normalize("Deebo Samuel")
    assert normalize("Amon-Ra St. Brown") == "amonrastbrown"
    assert normalize("Ja'Marr Chase") == "jamarrchase"
    assert normalize("Kenneth Walker III") == normalize("Kenneth Walker")
    assert normalize("T.J. Hockenson") == "tjhockenson"


def test_aliases_bridge_guide_spellings():
    assert normalize("Kenneth Gainwell") == normalize("Kenny Gainwell")
    assert normalize("Bill Croskey-Merritt") == normalize("Jacory Croskey-Merritt")


def test_initial_key():
    assert initial_key("R. Stevenson") == "rstevenson"
    assert initial_key("Rhamondre Stevenson") == "rstevenson"


# ------------------------------------------------------------------- guide


def test_guide_loads_all_boards():
    g = get_guide()
    for fmt in ("ppr", "half_ppr"):
        assert len(g.ranked(fmt)) == 150
        assert g.ranked(fmt)[0].name == "Jahmyr Gibbs"
        assert len(g.positional(fmt, "RB")) == 60
        assert len(g.positional(fmt, "QB")) == 32
        assert len(g.positional(fmt, "TE")) == 32


def test_guide_adj_ppg_tables():
    g = get_guide()
    assert len(g.adj_ppg_table("RB")) == 46
    assert len(g.adj_ppg_table("WR")) == 48
    assert g.resolve("Christian McCaffrey").adj_ppg_2025 == 24.8
    assert g.resolve("Josh Allen").adj_ppg_2025 == 23.2


def test_guide_resolves_abbreviated_names():
    g = get_guide()
    assert g.resolve("R. Stevenson").name == "Rhamondre Stevenson"
    assert g.resolve("T. Henderson").name == "TreVeyon Henderson"


def test_guide_has_all_fifty_stats_and_tags_players():
    g = get_guide()
    assert {s["n"] for s in g.stats} == set(range(1, 51))
    assert any("unluckiest" in n for n in g.resolve("CeeDee Lamb").notes)


# ------------------------------------------------------------------ curves


def test_powerlaw_fits_guide_tables_closely():
    g = get_guide()
    for pos, floor in (("RB", 0.95), ("WR", 0.95), ("TE", 0.90), ("QB", 0.80)):
        fit = PowerLawFit(g.adj_ppg_table(pos))
        assert fit.r2 > floor, f"{pos} fit degraded: {fit!r}"


def test_powerlaw_does_not_blow_up_at_rank_one():
    """The rank offset exists to stop an unshifted power law overshooting."""
    g = get_guide()
    for pos in ("RB", "WR", "TE", "QB"):
        table = dict(g.adj_ppg_table(pos))
        fit = PowerLawFit(g.adj_ppg_table(pos))
        assert fit(1) < table[1] * 1.15, f"{pos} overshoots rank 1"


def test_loglog_curve_hits_its_anchors():
    c = LogLogCurve([(1, 3), (12, 22), (36, 84)])
    for x, y in ((1, 3), (12, 22), (36, 84)):
        assert c(x) == pytest.approx(y, rel=1e-6)
    assert c(6) > 3 and c(6) < 22          # monotone between anchors


# ------------------------------------------------------------------- model


def test_projections_are_monotone_in_rank():
    m = ValueModel(get_guide(), "ppr")
    for pos in ("RB", "WR", "TE", "QB"):
        curve = [m.curve_ppg(pos, r) for r in range(1, 40)]
        assert all(a >= b for a, b in zip(curve, curve[1:])), pos


def test_superflex_raises_qb_replacement_level():
    m = ValueModel(get_guide(), "ppr")
    one_qb = m.replacement_levels(12, {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
    sf = m.replacement_levels(
        12, {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "SUPER_FLEX": 1})
    assert sf["QB"] < one_qb["QB"], "superflex should push QB replacement down"


def test_sigma_does_not_grow_with_positional_rank():
    """Deep players must not get fake ceilings - that bug drafted WR48 in Rd 5."""
    m = ValueModel(get_guide(), "ppr")
    elite = m.by_pos("WR")[0]
    deep = m.by_pos("WR")[45]
    assert deep.sigma < elite.sigma
    assert deep.upside < elite.ppg


# --------------------------------------------------------------------- adp


def test_adp_curve_matches_guide_landmarks():
    """The guide states QB3 goes 55th overall; our prior should agree."""
    a = AdpModel(teams=12)
    assert a.curve_adp("QB", 3) == pytest.approx(55, abs=2)
    assert a.curve_adp("WR", 36) == pytest.approx(84, abs=3)


def test_adp_is_not_stretched_by_league_size():
    """The Nth player off the board is the Nth-best whatever the league size."""
    small, big = AdpModel(teams=8), AdpModel(teams=16)
    assert big.curve_adp("RB", 12) == pytest.approx(small.curve_adp("RB", 12))


def test_one_per_roster_positions_go_earlier_in_deep_leagues():
    small, big = AdpModel(teams=8), AdpModel(teams=16)
    assert big.curve_adp("QB", 6) < small.curve_adp("QB", 6)
    assert big.curve_adp("TE", 4) < small.curve_adp("TE", 4)


def test_superflex_pulls_qbs_forward():
    one, sf = AdpModel(teams=12), AdpModel(teams=12, superflex=True)
    assert sf.curve_adp("QB", 1) < one.curve_adp("QB", 1) / 3


# ----------------------------------------------------------------- ordering


def _state(**kw) -> DraftState:
    base = dict(draft_id="t", teams=12, rounds=15, draft_type="snake",
                scoring="ppr", status="drafting")
    base.update(kw)
    return DraftState(**base)


def test_snake_order():
    s = _state()
    assert s.slot_for_pick(1) == 1
    assert s.slot_for_pick(12) == 12
    assert s.slot_for_pick(13) == 12       # snake turn
    assert s.slot_for_pick(24) == 1
    assert s.slot_for_pick(25) == 1


def test_linear_order():
    s = _state(draft_type="linear")
    assert s.slot_for_pick(13) == 1


def test_third_round_reversal():
    s = _state(reversal_round=3)
    assert s.slot_for_pick(25) == 12       # round 3 repeats round 2's order


def test_my_upcoming_picks():
    s = _state(my_slot=5)
    assert s.my_upcoming_picks(3) == [5, 20, 29]


def test_extract_draft_id():
    assert extract_draft_id("1234567890123456789") == "1234567890123456789"
    assert extract_draft_id(
        "https://sleeper.com/draft/nfl/1234567890123456789") == "1234567890123456789"
    with pytest.raises(ValueError):
        extract_draft_id("not-a-draft")


# ---------------------------------------------------------------- strategy


def test_presets_roundtrip_through_json():
    for name, factory in PRESETS.items():
        s = factory()
        again = Strategy.from_dict(s.to_dict())
        assert again.name == s.name
        assert again.round_targets == s.round_targets
        assert again.rb_dead_zone == s.rb_dead_zone


def test_for_league_picks_shape_appropriate_defaults():
    assert for_league(12, True, False).name == "superflex"
    assert for_league(10, False, True).name == "dynasty"
    assert for_league(12, False, False).name == "joel_rb_heavy"
    shallow = for_league(8, False, False)
    deep = for_league(16, False, False)
    assert shallow.rb_target_bonus < deep.rb_target_bonus


def test_upside_weight_ramps_late():
    s = Strategy()
    assert s.upside_weight(1) < s.upside_weight(8) < s.upside_weight(14)


# ------------------------------------------------------------------ engine


@needs_sleeper
def _engine(**kw):
    from ffbot.engine import Engine
    from ffbot.mockdraft import synthetic_state
    from ffbot.store import Store
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    return Engine(synthetic_state(**kw), store=Store(tmp))


@needs_sleeper
def test_fall_sign_convention():
    """ADP 20 still on the board at pick 40 is value; the reverse is a reach."""
    from ffbot.engine import Engine
    assert Engine.fall(20, 40) == 20        # fell 20 picks past ADP
    assert Engine.fall(149, 53) == -96      # 96-pick reach


@needs_sleeper
def test_engine_recommends_and_scores():
    e = _engine(teams=12, my_slot=5)
    recs = e.recommend(5)
    assert len(recs) == 5
    assert recs == sorted(recs, key=lambda r: -r.score)
    assert all(r.reasons for r in recs)
    assert all(0.0 <= r.survival <= 1.0 for r in recs)


@needs_sleeper
def test_engine_never_recommends_a_drafted_player():
    from ffbot.mockdraft import autopick
    e = _engine(teams=12, my_slot=5)
    autopick(e, 40, rng=random.Random(3))
    drafted = {e.resolve_key(p.name, p.pos) for p in e.state.picks}
    assert not {r.key for r in e.recommend(10)} & drafted


@needs_sleeper
def test_full_mock_builds_a_legal_roster():
    from ffbot.mockdraft import autopick
    e = _engine(teams=12, rounds=15, my_slot=5)
    autopick(e, 181, rng=random.Random(11))
    roster = e.state.my_roster()
    assert len(roster) == 15
    counts = e.my_counts()
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        assert counts[pos] >= e.state.starters.get(pos, 0), \
            f"unfilled starting {pos}: {counts}"
    assert counts["WR"] <= e.strategy.max_pos["WR"]


@needs_sleeper
def test_rb_heavy_default_starts_with_running_backs():
    from ffbot.mockdraft import autopick
    e = _engine(teams=12, my_slot=5)
    autopick(e, 30, rng=random.Random(11))
    first_two = [p.pos for p in e.state.my_roster()[:2]]
    assert first_two.count("RB") >= 1


@needs_sleeper
def test_kdst_suppressed_until_the_final_rounds():
    e = _engine(teams=12, rounds=15, my_slot=5)
    early = e.recommend(20, pick_no=5)
    assert not any(r.pos in ("K", "DEF") for r in early)


@needs_sleeper
def test_urgency_prefers_the_player_who_will_not_last():
    """Equal-value players should split on availability, not coin flips."""
    from ffbot.mockdraft import autopick
    e = _engine(teams=12, my_slot=5)
    autopick(e, 53, rng=random.Random(7))
    recs = e.recommend(8)
    gone_soon = [r for r in recs if r.survival < 0.15]
    can_wait = [r for r in recs if r.survival > 0.85]
    for a in gone_soon:
        for b in can_wait:
            if abs(a.value_now - b.value_now) < 0.5:
                assert a.score > b.score, (
                    f"{a.name} ({a.survival:.0%}) should beat "
                    f"{b.name} ({b.survival:.0%}) at equal value")


# ------------------------------------------------------------------ rookies


def test_rookies_surface_in_upside_rounds():
    """Regression: the engine used to shortlist zero rookies in round 9,
    despite guide rule 4 naming rookie WRs the late process players."""
    import random
    from ffbot.engine import Engine
    from ffbot.mockdraft import synthetic_state, autopick

    st = synthetic_state(teams=12, rounds=15, my_slot=5)
    e = Engine(st)
    autopick(e, 101, rng=random.Random(7))
    recs = e.recommend(10)
    rookies = [r for r in recs if r.proj.is_rookie]
    assert rookies, "no rookie in a round-9 shortlist"
    # the capital/rule-4 reasoning must be visible, not just the score
    texts = " ".join(t.text for r in rookies for t in r.reasons)
    assert "rookie" in texts.lower()


def test_rookie_profiles_attached_from_context_file():
    from ffbot.guide import Guide

    g = Guide()
    love = g.resolve("Jeremiyah Love")
    assert love is not None and love.is_rookie
    joined = " ".join(love.notes)
    assert "[Rookie profile]" in joined
    assert "Notre Dame" in joined          # college
    assert "3rd overall" in joined         # draft capital


def test_rookie_ceiling_beats_matching_veteran():
    """A rookie's 85th percentile sits further from the median than a
    veteran's at the same projection - the bimodal right tail."""
    from ffbot.guide import get_guide
    from ffbot.model import ValueModel

    m = ValueModel(get_guide(), "ppr")
    rook = [p for p in m.projections.values() if p.is_rookie]
    vets = [p for p in m.projections.values()
            if not p.is_rookie and p.hist_ppg is not None]
    assert rook and vets
    r_stretch = sum((p.upside - p.ppg) / p.sigma for p in rook) / len(rook)
    v_stretch = sum((p.upside - p.ppg) / p.sigma for p in vets) / len(vets)
    assert r_stretch > v_stretch + 0.2
