"""Mini-league fetching (M1.8) against a synthetic FPL client: the nearest rivals and the leader
are chosen, pages are read only until the manager is found, picks of the last passed deadline
become lineups of player codes, a Free Hit squad reverts, and a rival whose picks cannot be read
(not published, an outage, players unknown to the snapshot) is unavailable — never invented."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fpl_api.league import (
    LeagueError,
    fetch_league,
    lineup_from_picks,
    select_rivals,
)
from tests.league_fake import MY_ENTRY, FakeFplClient, standings_rows

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
ELEMENTS = {e: 10_000 + e for e in range(1, 400)}  # element id → player code


def _squad(seed: int) -> list[int]:
    return [seed * 15 + i for i in range(1, 16)]


def _client(n: int = 20, my_rank: int = 8, **kw) -> FakeFplClient:  # type: ignore[no-untyped-def]
    rows = standings_rows(n, MY_ENTRY, my_rank)
    picks = {r["entry"]: _squad(i % 20) for i, r in enumerate(rows)}
    return FakeFplClient(leagues={77: ("League", rows)}, picks=picks, **kw)


def test_rivals_are_the_nearest_above_and_below_plus_the_leader() -> None:
    rows = standings_rows(20, MY_ENTRY, 10)
    got = select_rivals(rows, MY_ENTRY)
    ranks = [r["rank"] for r in got]
    assert ranks == [1, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15]
    assert MY_ENTRY not in {r["entry"] for r in got}
    top = select_rivals(standings_rows(20, MY_ENTRY, 1), MY_ENTRY)
    assert [r["rank"] for r in top] == [2, 3, 4, 5, 6]  # no one above, no duplicate leader
    assert select_rivals(rows, 123) == []


def test_pages_are_read_until_the_manager_is_found() -> None:
    client = _client(n=120, my_rank=60, page_size=50)
    got = fetch_league(client, 77, MY_ENTRY, 7, ELEMENTS, NOW)
    assert got.pages == 2 and [c for c in client.calls if c.startswith("standings")] == [
        "standings:1",
        "standings:2",
    ]
    assert got.me["rank"] == 60 and len(got.standings) == 100
    # standings pages + history and picks for each of the eleven rivals, nothing more
    assert got.requests == 2 + 2 * 11 == len(client.calls)
    assert got.league_name == "League" and got.picks_gameweek == 7


def test_unknown_league_and_a_manager_outside_the_supported_places_are_refused() -> None:
    with pytest.raises(LeagueError, match="no classic league") as e:
        fetch_league(_client(), 78, MY_ENTRY, 7, ELEMENTS, NOW)
    assert e.value.retryable is False
    deep = _client(n=400, my_rank=200, page_size=50)
    with pytest.raises(LeagueError, match="not in the first 150 places"):
        fetch_league(deep, 77, MY_ENTRY, 7, ELEMENTS, NOW)
    assert len(deep.calls) == 3  # three pages at most, no rival request
    with pytest.raises(LeagueError, match="unavailable") as e:
        fetch_league(_client(outage_after=0), 77, MY_ENTRY, 7, ELEMENTS, NOW)
    assert e.value.retryable is True


def test_picks_become_lineups_and_unreadable_rivals_are_unavailable() -> None:
    rows = standings_rows(20, MY_ENTRY, 8)
    above = rows[6]["entry"]  # rank 7: directly above
    missing = rows[8]["entry"]  # rank 9: picks not published
    unknown = rows[9]["entry"]  # rank 10: a player this snapshot does not know
    client = _client(missing_picks={missing})
    client.picks[unknown] = [*_squad(3)[:14], 9_999]
    client.chips[above] = [("wildcard", 2), ("freehit", 7)]
    got = fetch_league(client, 77, MY_ENTRY, 7, ELEMENTS, NOW)
    by = {r["entry"]: r for r in got.rivals}
    # a Free Hit in the gameweek: the squad that counts afterwards is the one before it
    assert f"picks:{above}:6" in client.calls and f"picks:{above}:7" not in client.calls
    assert by[above]["chips_used"] == ["free_hit", "wildcard"]
    assert by[above]["active_chip"] == "free_hit" and "Free Hit" in by[above]["note"]
    lu = by[above]["lineup"]
    assert lu is not None and len(lu["starters"]) == 11 and len(lu["bench"]) == 4
    assert lu["captain"] == lu["starters"][5] and lu["vice_captain"] == lu["starters"][9]
    assert all(c >= 10_000 for c in lu["starters"])  # player codes, not element ids
    assert by[missing]["lineup"] is None and "not available" in by[missing]["unavailable"]
    assert by[unknown]["lineup"] is None and by[unknown]["unmapped"] == [9_999]
    assert "not in this data snapshot" in by[unknown]["unavailable"]


def test_an_outage_midway_marks_the_rest_unavailable_without_more_requests() -> None:
    client = _client(outage_after=5)  # standings + two full rivals, then the source is gone
    got = fetch_league(client, 77, MY_ENTRY, 7, ELEMENTS, NOW)
    known = [r for r in got.rivals if r["lineup"] is not None]
    lost = [r for r in got.rivals if r["lineup"] is None]
    assert len(known) == 2 and len(lost) == len(got.rivals) - 2
    assert all("unavailable" in r["unavailable"] for r in lost)
    assert len(client.calls) == 5 and got.warnings


def test_before_any_deadline_no_picks_are_requested() -> None:
    client = _client()
    got = fetch_league(client, 77, MY_ENTRY, None, ELEMENTS, NOW)
    assert client.calls == ["standings:1"]
    assert all("not public" in r["unavailable"] for r in got.rivals)


def test_lineup_from_picks_refuses_unknown_players() -> None:
    _, picks = _client().entry_picks(next(iter(_client().picks)), 7)
    lu, unmapped = lineup_from_picks(picks, ELEMENTS)
    assert lu is not None and unmapped == []
    lu2, unmapped2 = lineup_from_picks(picks, {})
    assert lu2 is None and len(unmapped2) == 15
