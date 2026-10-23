"""Optional mini-league / ownership awareness (§24, §65).

Rank effects are never claimed; everything is a modelled probability on the joint simulation:

* head-to-head: P(my gameweek points > rival's), the distribution of the points gap;
* differential exposure: players I own that the rival does not (and vice versa) with their
  ownership share; template assets (high ownership) I do not own;
* lead-protection vs chasing view of alternative plans: a plan that maximises the mean is not the
  one that maximises P(overtaking a rival) — both are shown, the user chooses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd
from pydantic import BaseModel

from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import Ruleset
from fpl_domain.squad import Lineup
from fpl_simulation.engine import SimulationResult
from fpl_simulation.lineup_eval import score_lineup


class HeadToHead(BaseModel):
    rival: str
    probability_ahead: float
    probability_level: float
    gap_mean: float
    gap_p10: float
    gap_p90: float
    my_differentials: list[int]
    rival_differentials: list[int]


def lineup_points(
    sim: SimulationResult,
    lineup: Lineup,
    gw: int,
    positions: Mapping[int, Position],
    ruleset: Ruleset,
    chip: ChipType | None = None,
) -> np.ndarray:
    gi = sim.gw_index(gw)
    have = set(sim.player_codes.tolist())
    pts, mins = {}, {}
    for c in lineup.players:
        if c in have:
            pi = sim.index_of(c)
            pts[c] = sim.points[:, pi, gi].astype(np.int64)
            mins[c] = sim.minutes[:, pi, gi].astype(np.int64)
        else:
            pts[c] = mins[c] = np.zeros(sim.n_sims, dtype=np.int64)
    return score_lineup(lineup, positions, pts, mins, ruleset, chip).astype(float)


def head_to_head(
    sim: SimulationResult,
    mine: Lineup,
    rivals: Mapping[str, Lineup],
    gw: int,
    positions: Mapping[int, Position],
    ruleset: Ruleset,
) -> list[HeadToHead]:
    me = lineup_points(sim, mine, gw, positions, ruleset)
    out = []
    for name, lu in rivals.items():
        r = lineup_points(sim, lu, gw, positions, ruleset)
        gap = me - r
        out.append(
            HeadToHead(
                rival=name,
                probability_ahead=float((gap > 0).mean()),
                probability_level=float((gap == 0).mean()),
                gap_mean=float(gap.mean()),
                gap_p10=float(np.percentile(gap, 10)),
                gap_p90=float(np.percentile(gap, 90)),
                my_differentials=sorted(set(mine.starters) - set(lu.starters)),
                rival_differentials=sorted(set(lu.starters) - set(mine.starters)),
            )
        )
    return out


class StandingOutlook(BaseModel):
    """One rival's observed standing and the modelled outlook for the next gameweek (M1.8):
    the observed points gap is data, every probability is the joint simulation's."""

    rival: str
    points_gap: int  # rival total − my total before the gameweek (negative: I am ahead)
    probability_ahead_this_gw: float  # P(my gameweek points > rival's)
    probability_level_this_gw: float
    probability_ahead_after: float  # P(I am above the rival on total points after the gameweek)
    probability_level_after: float
    diff_mean: float  # my gameweek points − rival's, mean and 80 % band
    diff_p10: float
    diff_p90: float


def standing_outlook(
    sim: SimulationResult,
    mine: Lineup,
    rivals: Mapping[str, Lineup],
    gaps: Mapping[str, int],
    gw: int,
    positions: Mapping[int, Position],
    ruleset: Ruleset,
) -> list[StandingOutlook]:
    """Head-to-head with the observed points gap folded in: I finish the gameweek above a rival
    when my points minus theirs exceed ``gaps[rival]`` (their total minus mine). Both lineups
    are scored sample by sample on the same simulation, so shared players cancel out; no hits,
    chips or further transfers are assumed for either side."""
    me = lineup_points(sim, mine, gw, positions, ruleset)
    out = []
    for name, lu in rivals.items():
        diff = me - lineup_points(sim, lu, gw, positions, ruleset)
        gap = int(gaps.get(name, 0))
        out.append(
            StandingOutlook(
                rival=name,
                points_gap=gap,
                probability_ahead_this_gw=float((diff > 0).mean()),
                probability_level_this_gw=float((diff == 0).mean()),
                probability_ahead_after=float((diff > gap).mean()),
                probability_level_after=float((diff == gap).mean()),
                diff_mean=float(diff.mean()),
                diff_p10=float(np.percentile(diff, 10)),
                diff_p90=float(np.percentile(diff, 90)),
            )
        )
    return out


def captain_vs_rivals(
    sim: SimulationResult,
    mine: Lineup,
    candidates: Sequence[int],
    rivals: Mapping[str, Lineup],
    gaps: Mapping[str, int],
    gw: int,
    positions: Mapping[int, Position],
    ruleset: Ruleset,
) -> pd.DataFrame:
    """For each captain candidate among my starters: my expected gameweek points with that
    armband and, per rival, P(above them after the gameweek) — the "protect a lead or chase"
    view of the captaincy choice. The vice-captain stays as set unless the candidate is the
    vice, in which case the current captain becomes vice."""
    rows = []
    rival_pts = {n: lineup_points(sim, lu, gw, positions, ruleset) for n, lu in rivals.items()}
    for cand in candidates:
        if cand not in mine.starters:
            raise ValueError(f"captain candidate {cand} is not a starter")
        vice = mine.vice_captain if cand != mine.vice_captain else mine.captain
        lu = Lineup(starters=mine.starters, bench=mine.bench, captain=cand, vice_captain=vice)
        me = lineup_points(sim, lu, gw, positions, ruleset)
        row: dict[str, object] = {"captain": cand, "expected_points": float(me.mean())}
        for name, pts in rival_pts.items():
            row[f"p_ahead_after:{name}"] = float(((me - pts) > int(gaps.get(name, 0))).mean())
        rows.append(row)
    return pd.DataFrame(rows)


def rival_ownership(
    rivals: Mapping[str, Lineup], owned: set[int]
) -> tuple[dict[int, int], dict[int, int]]:
    """Among the rivals whose squads are known: how many hold each player I do not own
    (``shared_not_mine``) and how many hold each player I own (``mine_held``). Counts over
    whole 15-player squads; nothing is estimated for rivals without a known squad."""
    shared_not_mine: dict[int, int] = {}
    mine_held: dict[int, int] = dict.fromkeys(owned, 0)
    for lu in rivals.values():
        for c in lu.players:
            if c in owned:
                mine_held[c] += 1
            else:
                shared_not_mine[c] = shared_not_mine.get(c, 0) + 1
    return shared_not_mine, mine_held


def template_gaps(owned: set[int], ownership: pd.Series, threshold: float = 30.0) -> pd.DataFrame:
    """Highly-owned players (≥ threshold %) not in my squad: main sources of rank volatility."""
    t = ownership[ownership >= threshold].sort_values(ascending=False)
    keep = ~t.index.isin(list(owned))
    return pd.DataFrame({"player_code": t.index[keep], "ownership": t.to_numpy()[keep]})


def plan_choice_vs_rival(plans: Mapping[str, np.ndarray], rival: np.ndarray) -> pd.DataFrame:
    """For alternative plans' sample totals: mean points vs P(finishing ahead of the rival)."""
    rows = [
        {
            "plan": k,
            "mean_points": float(v.mean()),
            "probability_ahead": float((v > rival).mean()),
            "std": float(v.std()),
        }
        for k, v in plans.items()
    ]
    return pd.DataFrame(rows).sort_values("mean_points", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- rival-aware plans (M1.9)


class RivalPlanOutcome(BaseModel):
    """One plan against one rival, paired sample by sample on the joint simulation: the
    observed gap is data (their total minus mine before the gameweek); every probability is
    the model's for the coming gameweek only."""

    points_gap: int
    p_beat_this_gw: float  # P(my gameweek points with this plan > the rival's)
    p_level_this_gw: float
    p_above_after: float  # P(my points − theirs > gap): above them on totals after the gameweek
    p_level_after: float
    diff_mean: float  # my gameweek points − theirs: mean and 80 % band
    diff_p10: float
    diff_p90: float


class RivalPlanMetrics(BaseModel):
    """One plan against every selected rival. ``p_above_all_after`` is the joint probability,
    on the same samples, that the plan ends the gameweek above *all* of them (one event per
    sample, not a product of marginals); ``se`` is its binomial standard error."""

    per_rival: dict[str, RivalPlanOutcome]
    p_above_all_after: float
    p_above_all_se: float
    p_above_all_vs_hold: float  # minus HOLD's joint probability (paired on the same samples)
    expected_points_1gw: float  # the plan's expected gameweek points, net of hits
    n_samples: int


class RivalRanking(BaseModel):
    """Which plan is best for expected points and which for the rival objective, and what
    choosing the rival-aware plan gives up — all from the same evaluation."""

    best_by_points: str
    best_by_rivals: str
    agree: bool
    points_cost_of_rival_plan: float  # E[points] of best_by_points − E[points] of best_by_rivals
    p_gain_of_rival_plan: float  # p_above_all(best_by_rivals) − p_above_all(best_by_points)
    p_gain_se: float  # standard error of that paired difference
    decisive: bool  # the difference exceeds two standard errors
    explanation: str


def joint_above(
    points: np.ndarray, rival_points: Mapping[str, np.ndarray], gaps: Mapping[str, int]
) -> np.ndarray:
    """Per sample: my points with this plan put me above every selected rival on totals."""
    ok = np.ones(len(points), dtype=bool)
    for name, rp in rival_points.items():
        ok &= (points - rp) > int(gaps.get(name, 0))
    return ok


def plans_vs_rivals(
    plan_points: Mapping[str, np.ndarray],
    rival_points: Mapping[str, np.ndarray],
    gaps: Mapping[str, int],
    hold_label: str = "hold",
) -> dict[str, RivalPlanMetrics]:
    """``plan_points[label]`` are a plan's gameweek points per sample, net of its hit;
    ``rival_points[name]`` a rival's on the same samples. Every plan is scored against every
    rival pairwise, and jointly against all of them."""
    if hold_label not in plan_points:
        raise ValueError(f"the HOLD plan ({hold_label!r}) must be among the plans")
    if not rival_points:
        raise ValueError("at least one rival with a known squad is needed")
    n = len(plan_points[hold_label])
    for arr in (*plan_points.values(), *rival_points.values()):
        if len(arr) != n:
            raise ValueError("every plan and rival must be scored on the same samples")
    hold_all = joint_above(plan_points[hold_label], rival_points, gaps).mean()
    out: dict[str, RivalPlanMetrics] = {}
    for label, mine in plan_points.items():
        per: dict[str, RivalPlanOutcome] = {}
        for name, rp in rival_points.items():
            diff = mine - rp
            gap = int(gaps.get(name, 0))
            per[name] = RivalPlanOutcome(
                points_gap=gap,
                p_beat_this_gw=float((diff > 0).mean()),
                p_level_this_gw=float((diff == 0).mean()),
                p_above_after=float((diff > gap).mean()),
                p_level_after=float((diff == gap).mean()),
                diff_mean=float(diff.mean()),
                diff_p10=float(np.percentile(diff, 10)),
                diff_p90=float(np.percentile(diff, 90)),
            )
        p_all = float(joint_above(mine, rival_points, gaps).mean())
        out[label] = RivalPlanMetrics(
            per_rival=per,
            p_above_all_after=p_all,
            p_above_all_se=float(np.sqrt(p_all * (1.0 - p_all) / n)) if n else 0.0,
            p_above_all_vs_hold=p_all - float(hold_all),
            expected_points_1gw=float(mine.mean()),
            n_samples=n,
        )
    return out


def rank_vs_rivals(
    metrics: Mapping[str, RivalPlanMetrics],
    plan_points: Mapping[str, np.ndarray],
    rival_points: Mapping[str, np.ndarray],
    gaps: Mapping[str, int],
    eligible: Sequence[str],
    rival_names: Mapping[str, str] | None = None,
) -> RivalRanking:
    """Among ``eligible`` plans (the valid ones): the best by expected points and the best by
    P(above all selected rivals after the gameweek), ties broken by the other criterion, and
    the paired trade-off between them with its exact standard error (the per-sample
    difference of the two joint indicators)."""
    if not eligible:
        raise ValueError("no eligible plan to rank")
    by_pts = max(
        eligible, key=lambda k: (metrics[k].expected_points_1gw, metrics[k].p_above_all_after)
    )
    by_riv = max(
        eligible, key=lambda k: (metrics[k].p_above_all_after, metrics[k].expected_points_1gw)
    )
    cost = metrics[by_pts].expected_points_1gw - metrics[by_riv].expected_points_1gw
    gain = metrics[by_riv].p_above_all_after - metrics[by_pts].p_above_all_after
    se = 0.0
    if by_pts != by_riv:
        d = joint_above(plan_points[by_riv], rival_points, gaps).astype(float) - joint_above(
            plan_points[by_pts], rival_points, gaps
        ).astype(float)
        se = float(d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 1 else 0.0
    decisive = by_pts != by_riv and gain > 2 * se
    names = rival_names or {}
    who = ", ".join(names.get(k, k) for k in rival_points) or "the selected rivals"
    if by_pts == by_riv:
        text = (
            f"'{by_riv}' is the best plan for expected points and also the most likely to "
            f"finish the gameweek above {who}: the two objectives agree."
        )
    elif decisive:
        text = (
            f"'{by_riv}' is more likely to finish above {who} than the best plan for expected "
            f"points ('{by_pts}'): {gain:+.1%} on the joint probability for {cost:.1f} expected "
            f"points less this gameweek. The difference is larger than two standard errors of "
            f"the paired estimate ({se:.1%}), so it is not sampling noise."
        )
    else:
        text = (
            f"'{by_riv}' scores higher than '{by_pts}' on finishing above {who} ({gain:+.1%} on "
            f"the joint probability for {cost:.1f} expected points less), but the difference is "
            f"within two standard errors of the paired estimate ({se:.1%}): the samples cannot "
            f"separate the two plans on this objective."
        )
    return RivalRanking(
        best_by_points=by_pts,
        best_by_rivals=by_riv,
        agree=by_pts == by_riv,
        points_cost_of_rival_plan=float(cost),
        p_gain_of_rival_plan=float(gain),
        p_gain_se=se,
        decisive=decisive,
        explanation=text,
    )
