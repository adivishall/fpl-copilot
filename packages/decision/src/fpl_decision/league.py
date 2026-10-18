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
