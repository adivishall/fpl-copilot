"""Mini-league outlook (M1.8) on a toy simulation: the observed points gap turns a head-to-head
into P(above the rival after the gameweek); the captain view re-scores my lineup per candidate
on the same samples; rival ownership counts whole squads and estimates nothing."""

from __future__ import annotations

import numpy as np
import pytest

from fpl_decision.league import captain_vs_rivals, rival_ownership, standing_outlook
from fpl_domain.enums import Position
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import Lineup
from fpl_simulation.engine import SimulationResult

RS = load_ruleset("2026-27")
POS = [Position.GK] * 2 + [Position.DEF] * 5 + [Position.MID] * 5 + [Position.FWD] * 3


def _sim(points: np.ndarray) -> SimulationResult:
    s, p = points.shape
    return SimulationResult(
        player_codes=np.arange(1, p + 1),
        gameweeks=np.array([5]),
        points=points.reshape(s, p, 1).astype(np.int16),
        minutes=np.full((s, p, 1), 90, dtype=np.int16),
        starts=np.ones((s, p, 1), dtype=np.int8),
        has_fixture=np.ones((p, 1), bool),
        components={},
        events={},
        seed=0,
        n_sims=s,
        ruleset_version=RS.ruleset_version,
    )


def _world() -> tuple[SimulationResult, dict[int, Position], Lineup, Lineup]:
    rng = np.random.default_rng(1)
    pts = np.full((4000, 17), 2)
    pts[:, 15] = np.where(rng.random(4000) < 0.5, 10, 2)  # my differential: boom or bust
    pts[:, 16] = 5  # the rival's steady pick
    positions = {i + 1: POS[i] for i in range(15)} | {16: Position.MID, 17: Position.MID}
    base = (1, 3, 4, 5, 8, 9, 10, 11, 13, 14)
    mine = Lineup(starters=(*base, 16), bench=(2, 6, 7, 12), captain=13, vice_captain=8)
    rival = Lineup(starters=(*base, 17), bench=(2, 6, 7, 12), captain=13, vice_captain=8)
    return _sim(pts), positions, mine, rival


def test_points_gap_turns_head_to_head_into_overtake_probability() -> None:
    sim, positions, mine, rival = _world()
    # my differential scores 10 or 2 against a steady 5: diff is +5 or −3, half the time each
    level = standing_outlook(sim, mine, {"r": rival}, {"r": 0}, 5, positions, RS)[0]
    assert level.points_gap == 0 and level.probability_ahead_this_gw == pytest.approx(0.5, 0.05)
    assert level.probability_ahead_after == level.probability_ahead_this_gw
    assert level.diff_p10 == -3 and level.diff_p90 == 5
    behind = standing_outlook(sim, mine, {"r": rival}, {"r": 4}, 5, positions, RS)[0]
    assert behind.probability_ahead_after == pytest.approx(0.5, 0.05)  # need +5: the boom
    far = standing_outlook(sim, mine, {"r": rival}, {"r": 6}, 5, positions, RS)[0]
    assert far.probability_ahead_after == 0.0  # no sample closes a 6-point gap
    ahead = standing_outlook(sim, mine, {"r": rival}, {"r": -4}, 5, positions, RS)[0]
    assert ahead.probability_ahead_after == 1.0  # −3 at worst still keeps a 4-point lead
    exactly = standing_outlook(sim, mine, {"r": rival}, {"r": 5}, 5, positions, RS)[0]
    assert exactly.probability_level_after == pytest.approx(0.5, 0.05)
    assert exactly.probability_ahead_after == 0.0


def test_captain_view_rescores_my_lineup_per_candidate_against_each_rival() -> None:
    sim, positions, mine, rival = _world()
    table = captain_vs_rivals(sim, mine, [13, 16], {"r": rival}, {"r": 8}, 5, positions, RS)
    assert list(table["captain"]) == [13, 16]
    steady, boom = table.iloc[0], table.iloc[1]
    # with the steady captain my margin is +5 or −3 (never closes 8 points); captaining the
    # boom-or-bust pick makes it +11 or −5: more expected points and a real chance to overtake
    assert boom["expected_points"] > steady["expected_points"]
    assert steady["p_ahead_after:r"] == 0.0
    assert boom["p_ahead_after:r"] == pytest.approx(0.5, 0.05)
    with pytest.raises(ValueError, match="not a starter"):
        captain_vs_rivals(sim, mine, [2], {"r": rival}, {}, 5, positions, RS)


def test_rival_ownership_counts_known_squads_only() -> None:
    _, _, mine, rival = _world()
    other = Lineup(starters=rival.starters, bench=(2, 6, 7, 15), captain=13, vice_captain=8)
    shared, held = rival_ownership({"a": rival, "b": other}, set(mine.players))
    assert shared == {17: 2, 15: 1}  # both rivals hold 17; one benches 15 — neither is mine
    assert held[16] == 0 and held[13] == 2 and held[12] == 1  # 12 is on only one bench
    assert rival_ownership({}, {1, 2}) == ({}, {1: 0, 2: 0})
