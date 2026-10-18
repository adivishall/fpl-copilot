"""A network-free stand-in for the public FPL API's league endpoints (tests and the e2e stack).

It serves classic-league standings in pages, entry histories with chips, entry picks and the
entry's league list from synthetic data — invented team and manager names, squads drawn from a
dataset's real player registry so that picks map to player codes. Nothing here is real FPL data
and nothing is fetched. Knobs reproduce what the real API does: unknown leagues (404), picks not
published (404), an outage after N requests, players unknown to the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from fpl_ingestion.http import HttpStatusError, SourceUnavailableError
from fpl_ingestion.sources import fpl_api_schemas as s

LEAGUE_ID = 4242
MY_ENTRY = 900_008
BASE = "https://fantasy.premierleague.com/api"


@dataclass
class FakeFplClient:
    leagues: dict[int, tuple[str, list[dict[str, Any]]]]  # id → (name, standings rows by rank)
    picks: dict[int, list[int]]  # entry → 15 element ids in pick order (1–11 start, 12–15 bench)
    chips: dict[int, list[tuple[str, int]]] = field(default_factory=dict)  # entry → (name, gw)
    missing_picks: set[int] = field(default_factory=set)
    outage_after: int | None = None  # requests served before the source becomes unreachable
    page_size: int = 50
    calls: list[str] = field(default_factory=list)

    def _call(self, what: str) -> None:
        if self.outage_after is not None and len(self.calls) >= self.outage_after:
            raise SourceUnavailableError("fake outage")
        self.calls.append(what)

    def league_standings(self, league_id: int, page: int = 1) -> tuple[None, s.LeagueStandings]:
        self._call(f"standings:{page}")
        if league_id not in self.leagues:
            raise HttpStatusError(f"{BASE}/leagues-classic/{league_id}/standings/", 404)
        name, rows = self.leagues[league_id]
        chunk = rows[(page - 1) * self.page_size : page * self.page_size]
        return None, s.LeagueStandings.model_validate(
            {
                "league": {"id": league_id, "name": name},
                "standings": {
                    "has_next": page * self.page_size < len(rows),
                    "page": page,
                    "results": chunk,
                },
            }
        )

    def entry_history(self, entry_id: int) -> tuple[None, s.EntryHistory]:
        self._call(f"history:{entry_id}")
        return None, s.EntryHistory.model_validate(
            {
                "current": [],
                "chips": [
                    {"name": n, "time": "2026-08-20T10:00:00Z", "event": gw}
                    for n, gw in self.chips.get(entry_id, [])
                ],
            }
        )

    def entry_picks(self, entry_id: int, gameweek: int) -> tuple[None, s.EntryPicks]:
        self._call(f"picks:{entry_id}:{gameweek}")
        if entry_id in self.missing_picks or entry_id not in self.picks:
            raise HttpStatusError(f"{BASE}/entry/{entry_id}/event/{gameweek}/picks/", 404)
        els = self.picks[entry_id]
        return None, s.EntryPicks.model_validate(
            {
                "active_chip": None,
                "picks": [
                    {
                        "element": e,
                        "position": i + 1,
                        "multiplier": 2 if i == 5 else (1 if i < 11 else 0),
                        "is_captain": i == 5,
                        "is_vice_captain": i == 9,
                    }
                    for i, e in enumerate(els)
                ],
                "entry_history": {
                    "event": gameweek,
                    "points": 50,
                    "total_points": 400,
                    "bank": 5,
                    "value": 1000,
                    "event_transfers": 1,
                    "event_transfers_cost": 0,
                    "points_on_bench": 4,
                },
            }
        )

    def entry(self, entry_id: int) -> tuple[None, s.ApiEntry]:
        self._call(f"entry:{entry_id}")
        classic = [
            {
                "id": lid,
                "name": name,
                "league_type": "x",
                "entry_rank": next((r["rank"] for r in rows if r["entry"] == entry_id), None),
                "rank_count": len(rows),
            }
            for lid, (name, rows) in self.leagues.items()
            if any(r["entry"] == entry_id for r in rows)
        ]
        return None, s.ApiEntry.model_validate(
            {"id": entry_id, "name": f"Team {entry_id}", "leagues": {"classic": classic}}
        )


def standings_rows(n: int, my_entry: int = MY_ENTRY, my_rank: int = 8) -> list[dict[str, Any]]:
    """``n`` synthetic entries ranked 1…n, seven points apart; ``my_entry`` at ``my_rank``."""
    others = [900_000 + k for k in range(1, n + 2) if 900_000 + k != my_entry][: n - 1]
    order = [*others[: my_rank - 1], my_entry, *others[my_rank - 1 :]]
    return [
        {
            "entry": e,
            "entry_name": f"Synthetic XI {e % 1000}",
            "player_name": f"Test Manager {e % 1000}",
            "rank": i + 1,
            "last_rank": i + 2 if i % 3 == 0 else max(1, i),
            "total": 500 - 7 * i,
            "event_total": 40 + (i * 5) % 30,
        }
        for i, e in enumerate(order)
    ]


def squad_elements(ds: Any, season: str, seed: int) -> list[int]:
    """A legal 15-player squad (2/5/5/3, ≤ 3 per club) from the season's registry, in pick
    order 1-4-4-2 starters then the bench; deterministic for ``seed``."""
    p = ds["players"]
    p = p[p["season"] == season][["source_id", "position", "team_code"]]
    rng = np.random.default_rng(seed)
    rows = p.iloc[rng.permutation(len(p))].itertuples(index=False)
    need = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
    got: dict[str, list[int]] = {k: [] for k in need}
    clubs: dict[int, int] = {}
    for sid, position, team in rows:
        pos = str(position)
        if pos in need and len(got[pos]) < need[pos] and clubs.get(int(team), 0) < 3:
            got[pos].append(int(sid))
            clubs[int(team)] = clubs.get(int(team), 0) + 1
        if all(len(got[k]) == v for k, v in need.items()):
            break
    g, d, m, f = got["GK"], got["DEF"], got["MID"], got["FWD"]
    return [g[0], *d[:4], *m[:4], *f[:2], g[1], d[4], m[4], f[2]]


def fake_from_dataset(
    ds: Any,
    season: str,
    league_id: int = LEAGUE_ID,
    my_entry: int = MY_ENTRY,
    n: int = 20,
    my_rank: int = 8,
    missing_picks: set[int] | None = None,
) -> FakeFplClient:
    rows = standings_rows(n, my_entry, my_rank)
    picks = {r["entry"]: squad_elements(ds, season, r["entry"]) for r in rows}
    chips = {rows[2]["entry"]: [("wildcard", 1)]}
    return FakeFplClient(
        leagues={league_id: ("Synthetic Test League", rows)},
        picks=picks,
        chips=chips,
        missing_picks=missing_picks or set(),
    )
