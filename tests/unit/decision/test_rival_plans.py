"""Rival-aware plan metrics (M1.9): paired on the same samples, the joint event is one event
per sample, the ranking by expected points and by the rival objective can disagree, and the
trade-off is reported with its paired standard error."""

from __future__ import annotations

import numpy as np
import pytest

from fpl_decision.league import joint_above, plans_vs_rivals, rank_vs_rivals


def _plans_and_rivals() -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, int]]:
    # eight samples; HOLD is steady, "swing" has a higher mean but the same upside rarely,
    # "safe" never beats the rival by enough; one rival 3 points ahead, one 2 behind
    hold = np.array([50, 52, 51, 49, 50, 53, 48, 51], dtype=float)
    swing = np.array([60, 40, 62, 38, 61, 39, 63, 37], dtype=float)  # mean 50, wide
    safe = np.array([47, 48, 47, 48, 47, 48, 47, 48], dtype=float)
    ahead = np.array([48, 48, 48, 48, 48, 48, 48, 48], dtype=float)  # rival A, 3 ahead on totals
    behind = np.array([55, 40, 55, 40, 55, 40, 55, 40], dtype=float)  # rival B, 2 behind
    return (
        {"hold": hold, "swing": swing, "safe": safe},
        {"A": ahead, "B": behind},
        {"A": 3, "B": -2},
    )


def test_per_rival_probabilities_are_paired_and_the_gap_is_folded_in() -> None:
    plans, rivals, gaps = _plans_and_rivals()
    m = plans_vs_rivals(plans, rivals, gaps)
    hold_a = m["hold"].per_rival["A"]
    # hold − A per sample: 2,4,3,1,2,5,0,3 → beats this GW 7/8, above after (> 3) 2/8,
    # level after (= 3) 2/8
    assert hold_a.points_gap == 3
    assert hold_a.p_beat_this_gw == pytest.approx(7 / 8)
    assert hold_a.p_above_after == pytest.approx(2 / 8)
    assert hold_a.p_level_after == pytest.approx(2 / 8)
    assert hold_a.diff_mean == pytest.approx(2.5)
    # swing − A: 12,−8,14,−10,13,−9,15,−11 → above after 4/8
    assert m["swing"].per_rival["A"].p_above_after == pytest.approx(0.5)
    assert m["hold"].n_samples == 8 and m["swing"].expected_points_1gw == pytest.approx(50.0)


def test_above_all_is_one_joint_event_per_sample_not_a_product() -> None:
    plans, rivals, gaps = _plans_and_rivals()
    m = plans_vs_rivals(plans, rivals, gaps)
    # swing above A (gap 3) on samples 0,2,4,6; above B (gap −2 → diff > −2) on samples where
    # swing − behind > −2: (5,0,7,−2,6,−1,8,−3) → samples 0,1,2,4,5,6 → joint = 0,2,4,6 → 4/8
    joint = joint_above(plans["swing"], rivals, gaps)
    assert joint.tolist() == [True, False, True, False, True, False, True, False]
    assert m["swing"].p_above_all_after == pytest.approx(0.5)
    pa = m["swing"].per_rival["A"].p_above_after
    pb = m["swing"].per_rival["B"].p_above_after
    assert m["swing"].p_above_all_after != pytest.approx(pa * pb)  # not a product of marginals
    assert m["swing"].p_above_all_se == pytest.approx(np.sqrt(0.5 * 0.5 / 8))
    assert m["hold"].p_above_all_vs_hold == 0.0
    assert m["swing"].p_above_all_vs_hold == pytest.approx(0.5 - m["hold"].p_above_all_after)


def test_ranking_by_points_and_by_rivals_can_disagree_and_the_trade_off_is_paired() -> None:
    plans, rivals, gaps = _plans_and_rivals()
    m = plans_vs_rivals(plans, rivals, gaps)
    r = rank_vs_rivals(
        m, plans, rivals, gaps, eligible=["hold", "swing", "safe"], rival_names={"A": "Ann"}
    )
    assert r.best_by_points == "hold"  # mean 50.5 > 50 > 47.5
    assert r.best_by_rivals == "swing"  # 0.5 > hold's joint probability
    assert not r.agree
    assert r.points_cost_of_rival_plan == pytest.approx(0.5)
    assert r.p_gain_of_rival_plan == pytest.approx(0.5 - m["hold"].p_above_all_after)
    d = joint_above(plans["swing"], rivals, gaps).astype(float) - joint_above(
        plans["hold"], rivals, gaps
    ).astype(float)
    assert r.p_gain_se == pytest.approx(d.std(ddof=1) / np.sqrt(8))
    assert "Ann" in r.explanation and "swing" in r.explanation
    assert r.decisive == (r.p_gain_of_rival_plan > 2 * r.p_gain_se)


def test_ranking_respects_eligibility_and_agreement() -> None:
    plans, rivals, gaps = _plans_and_rivals()
    m = plans_vs_rivals(plans, rivals, gaps)
    only = rank_vs_rivals(m, plans, rivals, gaps, eligible=["hold", "safe"])
    assert only.best_by_rivals == "hold" and only.best_by_points == "hold" and only.agree
    assert (
        only.points_cost_of_rival_plan == 0.0
        and only.p_gain_se == 0.0
        and "agree" in only.explanation
    )
    with pytest.raises(ValueError, match="eligible"):
        rank_vs_rivals(m, plans, rivals, gaps, eligible=[])


def test_inputs_are_validated() -> None:
    plans, rivals, gaps = _plans_and_rivals()
    with pytest.raises(ValueError, match="HOLD"):
        plans_vs_rivals({"x": plans["hold"]}, rivals, gaps)
    with pytest.raises(ValueError, match="same samples"):
        plans_vs_rivals(plans, {"A": rivals["A"][:4]}, gaps)
    with pytest.raises(ValueError, match="at least one rival"):
        plans_vs_rivals(plans, {}, gaps)
