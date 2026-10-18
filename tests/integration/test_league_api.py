# ruff: noqa: F811  (module-scoped API client and squad fixtures are shared with test_api)
"""Mini-league room API (M1.8) with a synthetic FPL client: following a league reads the
standings and the nearest rivals' picks once (a job), the outlook is the joint simulation's and
equals the library on the same forecast, re-reads are rate-limited, a changed squad recomputes
the outlook without asking FPL again, unreadable leagues fail with a reason, and the data
belongs to its manager — refused to others, exported, erased, and kept out of the logs."""

from __future__ import annotations

from typing import Any

import pytest
import structlog.testing
from fastapi.testclient import TestClient

from fpl_api.league import positions_map
from fpl_api.security import loggable_path
from fpl_decision.league import standing_outlook
from fpl_domain.squad import Lineup
from tests.integration.test_api import client, squad  # noqa: F401  (module fixtures)
from tests.integration.test_identity import _bearer, _manager, _register
from tests.league_fake import LEAGUE_ID, MY_ENTRY, FakeFplClient, fake_from_dataset

KEY = "lg-demo"


def _save(client: TestClient, codes: list[int], bank: int) -> str:
    r = client.post(
        "/api/v1/squad",
        json={
            "manager_key": KEY,
            "picks": [{"player_code": c} for c in codes],
            "bank": bank,
            "free_transfers": 1,
        },
    )
    assert r.status_code == 200, r.text
    return str(r.json()["state_id"])


@pytest.fixture(scope="module")
def fake(client: TestClient) -> FakeFplClient:
    svc = client.app.state.services  # type: ignore[attr-defined]
    f = fake_from_dataset(svc.data.ds, svc.context().season)
    svc.extras["fpl_client_factory"] = lambda: f
    return f


def test_following_a_league_reads_it_once_and_serves_a_paired_outlook(
    client: TestClient, squad: list[int], fake: FakeFplClient
) -> None:
    sid = _save(client, squad, bank=10)
    # a manual squad has no FPL ID: the manager's own entry must be given
    r = client.post("/api/v1/leagues", json={"manager_key": KEY, "league_id": LEAGUE_ID})
    assert r.status_code == 422 and "FPL ID" in r.text
    calls = len(fake.calls)
    r = client.post(
        "/api/v1/leagues",
        json={"manager_key": KEY, "league_id": LEAGUE_ID, "entry_id": MY_ENTRY},
    )
    assert r.status_code == 200, r.text  # inline jobs: read and computed before answering
    lg = r.json()
    assert lg["status"] == "succeeded" and lg["error"] is None
    assert lg["league_name"] == "Synthetic Test League" and lg["entry_id"] == MY_ENTRY
    assert len(fake.calls) - calls == 1 + 2 * 11  # one standings page, two requests per rival
    data, a = lg["data"], lg["analysis"]
    assert data["picks_gameweek"] == 1 and data["me"]["rank"] == 8
    assert len(data["standings"]) == 20 and len(data["rivals"]) == 11
    assert "public" in data["source"] and "no credentials" in data["source"]
    # the outlook names its inputs and uses the manager's current squad and lineup
    assert (
        a["state_id"] == sid
        and a["snapshot_id"]
        == client.get("/api/v1/gameweeks/current").json()["freshness"]["data_snapshot_id"]
    )
    assert a["gameweek"] == 2 and a["picks_gameweek"] == 1 and a["n_simulations"] == 200
    lineup = client.post("/api/v1/lineup", json={"manager_key": KEY}).json()["lineup"]
    assert a["me"]["lineup"] == lineup  # the same XI and armband Home and /lineup serve
    rivals = a["rivals"]
    assert [r["rank"] for r in rivals] == [1, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13]
    for rv in rivals:
        assert rv["points_gap"] == rv["total"] - data["me"]["total"]
        o = rv["outlook"]
        assert o is not None and rv["squad"] and len(rv["squad"]) == 15
        for k in ("probability_ahead_this_gw", "probability_ahead_after"):
            assert 0.0 <= o[k] <= 1.0
        assert o["diff_p10"] <= o["diff_mean"] <= o["diff_p90"]
        assert o["points_gap"] == rv["points_gap"]
    wildcard = next(rv for rv in rivals if rv["rank"] == 3)
    assert wildcard["chips_used"] == ["wildcard"]
    # parity: the stored outlook is exactly the library's on the same forecast and lineups
    svc = client.app.state.services  # type: ignore[attr-defined]
    ctx = svc.context()
    fc = svc.forecast_for(ctx, svc.horizon(None))
    st = svc.states.get(sid, KEY)
    pos = positions_map(svc.data.ds, ctx.season) | st.positions
    mine = Lineup.model_validate(a["me"]["lineup"])
    lus = {str(rv["entry"]): Lineup.model_validate(rv["lineup"]) for rv in data["rivals"]}
    gaps = {str(rv["entry"]): rv["points_gap"] for rv in rivals}
    again = {
        o.rival: o.model_dump()
        for o in standing_outlook(
            fc.simulation, mine, lus, gaps, 2, pos, svc.data.ruleset(ctx.season)
        )
    }
    assert {str(rv["entry"]): rv["outlook"] for rv in rivals} == again
    # the captain view: my candidates against the rivals directly above and below
    cv = a["captain_vs_rivals"]
    assert cv["above"] == str(next(rv["entry"] for rv in rivals if rv["rank"] == 7))
    assert cv["below"] == str(next(rv["entry"] for rv in rivals if rv["rank"] == 9))
    assert cv["rows"] and set(cv["rows"][0]["p_ahead_after"]) == {cv["above"], cv["below"]}
    assert a["league"]["rivals_with_squads"] == 11
    assert any("unknown and not guessed" in x for x in a["assumptions"])
    named = {int(c) for c in a["names"]}
    assert set(mine.players) <= named and all(
        set(rv["squad"]) <= named for rv in rivals if rv["squad"]
    )
    listed = client.get("/api/v1/leagues", params={"manager_key": KEY}).json()
    assert [x["league_id"] for x in listed["leagues"]] == [LEAGUE_ID]
    assert listed["leagues"][0].get("data") is None  # summaries only
    got = client.get(f"/api/v1/leagues/{LEAGUE_ID}", params={"manager_key": KEY})
    assert got.status_code == 200 and got.json()["staleness"]["reasons"] == []
    assert got.json()["analysis"]["computed_at"] == a["computed_at"]  # nothing recomputed


def test_rereads_are_rate_limited_and_a_new_squad_recomputes_without_fpl(
    client: TestClient, squad: list[int], fake: FakeFplClient
) -> None:
    before = client.get(f"/api/v1/leagues/{LEAGUE_ID}", params={"manager_key": KEY}).json()
    calls = len(fake.calls)
    body = {"manager_key": KEY, "league_id": LEAGUE_ID, "entry_id": MY_ENTRY}
    again = client.post("/api/v1/leagues", json=body)
    assert again.status_code == 429 and int(again.headers["retry-after"]) > 0
    r = client.post(f"/api/v1/leagues/{LEAGUE_ID}/refresh", json={"manager_key": KEY})
    assert r.status_code == 429 and "minutes ago" in r.text
    r = client.post(
        f"/api/v1/leagues/{LEAGUE_ID}/refresh", json={"manager_key": KEY, "fetch": False}
    )
    assert r.status_code == 200 and r.json()["fetched_at"] == before["fetched_at"]
    assert len(fake.calls) == calls  # recomputing never asks FPL again
    new = _save(client, squad, bank=11)
    got = client.get(f"/api/v1/leagues/{LEAGUE_ID}", params={"manager_key": KEY}).json()
    assert got["analysis"]["state_id"] == new and got["fetched_at"] == before["fetched_at"]
    assert got["staleness"]["reasons"] == [] and len(fake.calls) == calls


def test_unreadable_leagues_fail_with_a_reason_and_followed_leagues_are_capped(
    client: TestClient, squad: list[int], fake: FakeFplClient
) -> None:
    r = client.post(
        "/api/v1/leagues", json={"manager_key": KEY, "league_id": 31337, "entry_id": MY_ENTRY}
    )
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert r.json()["error"] == "no classic league with this ID" and r.json()["data"] is None
    job = client.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "failed" and job["kind"] == "league_refresh"
    for lid in (31338, 31339, 31340):
        client.post(
            "/api/v1/leagues", json={"manager_key": KEY, "league_id": lid, "entry_id": MY_ENTRY}
        )
    sixth = client.post(
        "/api/v1/leagues", json={"manager_key": KEY, "league_id": 31341, "entry_id": MY_ENTRY}
    )
    assert sixth.status_code == 409 and "at most 5" in sixth.text
    for lid in (31337, 31338, 31339, 31340):
        d = client.delete(f"/api/v1/leagues/{lid}", params={"manager_key": KEY})
        assert d.status_code == 200
    assert client.delete("/api/v1/leagues/31337", params={"manager_key": KEY}).status_code == 404
    for bad in (
        {"manager_key": KEY, "league_id": 0},
        {"manager_key": KEY, "league_id": 1, "entry_id": 60_000_000},
        {"manager_key": KEY, "league_id": 1, "extra": True},
    ):
        assert client.post("/api/v1/leagues", json=bad).status_code == 422


def test_available_leagues_come_from_the_managers_own_fpl_entry(
    client: TestClient, squad: list[int], fake: FakeFplClient
) -> None:
    manual = client.get("/api/v1/leagues/available", params={"manager_key": KEY}).json()
    assert manual == {"entry_id": None, "leagues": [], "reason": "squad entered manually"}
    svc = client.app.state.services  # type: ignore[attr-defined]
    st = svc.states.latest(KEY)[1]
    svc.states.save("lg-imported", st.model_copy(update={"manager_id": MY_ENTRY}), "fpl_api")
    calls = len(fake.calls)
    got = client.get("/api/v1/leagues/available", params={"manager_key": "lg-imported"}).json()
    assert got["entry_id"] == MY_ENTRY and got["max_rank"] == 150
    assert got["leagues"] == [
        {
            "league_id": LEAGUE_ID,
            "name": "Synthetic Test League",
            "kind": "invitational",
            "entry_rank": 8,
            "rank_count": 20,
            "supported": True,
        }
    ]
    client.get("/api/v1/leagues/available", params={"manager_key": "lg-imported"})
    assert len(fake.calls) == calls + 1  # one request per FPL ID within ten minutes
    # an imported squad follows a league without giving the ID again
    r = client.post("/api/v1/leagues", json={"manager_key": "lg-imported", "league_id": LEAGUE_ID})
    assert r.status_code == 200 and r.json()["entry_id"] == MY_ENTRY
    client.delete("/api/v1/managers/lg-imported")


def test_disabled_live_source_is_reported_not_faked(
    client: TestClient, squad: list[int], fake: FakeFplClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = client.app.state.services  # type: ignore[attr-defined]
    monkeypatch.delitem(svc.extras, "fpl_client_factory")
    monkeypatch.setattr(
        svc, "settings", svc.settings.model_copy(update={"live_sync_enabled": False})
    )
    r = client.post(
        "/api/v1/leagues", json={"manager_key": KEY, "league_id": 555, "entry_id": MY_ENTRY}
    )
    assert r.status_code == 503 and "disabled" in r.text
    listed = client.get("/api/v1/leagues", params={"manager_key": KEY}).json()
    assert listed["live_source"] is False


def test_leagues_belong_to_their_owner_are_exported_erased_and_kept_out_of_logs(
    client: TestClient, squad: list[int], fake: FakeFplClient
) -> None:
    alice = _register(client, "lg-alice@example.test")["session_token"]
    bob = _register(client, "lg-bob@example.test")["session_token"]
    a_key = _manager(client, alice, squad)
    body = {"manager_key": a_key, "league_id": LEAGUE_ID, "entry_id": MY_ENTRY}
    with structlog.testing.capture_logs() as logs:
        mine = client.post("/api/v1/leagues", json=body, headers=_bearer(alice))
        assert mine.status_code == 200, mine.text
        job_id = mine.json()["job_id"]
        forbidden: dict[str, Any] = {
            "list": client.get(
                "/api/v1/leagues", params={"manager_key": a_key}, headers=_bearer(bob)
            ),
            "get": client.get(
                f"/api/v1/leagues/{LEAGUE_ID}", params={"manager_key": a_key}, headers=_bearer(bob)
            ),
            "available": client.get(
                "/api/v1/leagues/available", params={"manager_key": a_key}, headers=_bearer(bob)
            ),
            "attach": client.post("/api/v1/leagues", json=body, headers=_bearer(bob)),
            "refresh": client.post(
                f"/api/v1/leagues/{LEAGUE_ID}/refresh",
                json={"manager_key": a_key, "fetch": False},
                headers=_bearer(bob),
            ),
            "delete": client.delete(
                f"/api/v1/leagues/{LEAGUE_ID}", params={"manager_key": a_key}, headers=_bearer(bob)
            ),
            "job": client.get(f"/api/v1/jobs/{job_id}", headers=_bearer(bob)),
        }
    for name, resp in forbidden.items():
        assert resp.status_code == 403, (name, resp.status_code, resp.text)
    text = repr(logs)
    assert str(LEAGUE_ID) not in text and "Synthetic" not in text and "Test Manager" not in text
    # a path rejected before routing (rate limit, key) is logged pseudonymised
    logged = loggable_path(f"/api/v1/leagues/{LEAGUE_ID}/refresh")
    assert str(LEAGUE_ID) not in logged and logged.startswith("/api/v1/leagues/l:")
    # bob's own manager sees none of alice's leagues
    b_key = _manager(client, bob, squad)
    assert (
        client.get("/api/v1/leagues", params={"manager_key": b_key}, headers=_bearer(bob)).json()[
            "leagues"
        ]
        == []
    )
    export = client.get(f"/api/v1/managers/{a_key}/export", headers=_bearer(alice)).json()
    assert [(x["manager_key"], x["league_id"]) for x in export["manager_leagues"]] == [
        (a_key, LEAGUE_ID)
    ]
    erased = client.delete(f"/api/v1/managers/{a_key}", headers=_bearer(alice)).json()
    assert erased["deleted"]["manager_leagues"] == 1
    assert (
        client.get(f"/api/v1/leagues/{LEAGUE_ID}", params={"manager_key": a_key}).status_code == 404
    )


def test_a_manager_erased_while_its_league_refresh_runs_is_not_recreated(
    client: TestClient, squad: list[int], fake: FakeFplClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Erasure can land while a job runs: the refresh then writes nothing back (no league row
    for an erased manager), and the job runner finishes quietly although the job's own record
    went with the manager."""
    import fpl_api.league as league_mod
    from fpl_api.privacy import delete_manager

    svc = client.app.state.services  # type: ignore[attr-defined]
    key = "lg-erased"
    st = svc.states.latest(KEY)[1]
    svc.states.save(key, st, "manual")
    real = league_mod.fetch_league

    def erase_midway(*a: Any, **kw: Any) -> Any:
        got = real(*a, **kw)
        delete_manager(svc.engine, key)  # the manager is erased while the job is reading
        return got

    monkeypatch.setattr(league_mod, "fetch_league", erase_midway)
    r = client.post(
        "/api/v1/leagues", json={"manager_key": key, "league_id": LEAGUE_ID, "entry_id": MY_ENTRY}
    )
    assert r.status_code == 404  # the refresh finished; its league row is gone with the manager
    assert svc.leagues.get(key, LEAGUE_ID) is None and svc.leagues.for_manager(key) == []
    from sqlalchemy import func, select

    from fpl_storage import models as m
    from fpl_storage.db import session_scope

    with session_scope(svc.engine) as s:
        left = s.scalar(
            select(func.count())
            .select_from(m.JobRow)
            .where(m.JobRow.request_json["manager_key"].astext == key)
        )
    assert left == 0  # the job's record went with the manager; the runner did not fail on it


def test_an_outlook_from_another_snapshot_is_recomputed_once_on_the_served_one(
    client: TestClient, squad: list[int], fake: FakeFplClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (found on the Docker stack): the worker had loaded a newer export than the API
    served, the outlook carried that snapshot, every view judged it stale, and a repeated
    recompute was de-duplicated onto the finished job, leaving the league 'failed'. Jobs now
    compute on the snapshot the API names, and each refresh is its own job."""
    from sqlalchemy import update

    from fpl_storage import models as m
    from fpl_storage.db import session_scope

    svc = client.app.state.services  # type: ignore[attr-defined]
    served = svc.data.snapshot_id
    pinned: list[str] = []
    real = svc.at_snapshot

    def spy(snapshot_id: str) -> Any:
        pinned.append(snapshot_id)
        return real(snapshot_id)

    monkeypatch.setattr(svc, "at_snapshot", spy)
    row = svc.leagues.get(KEY, LEAGUE_ID)
    stale = {**row["analysis"], "snapshot_id": "snap_newer_export_on_the_worker"}
    with session_scope(svc.engine) as s:
        s.execute(
            update(m.ManagerLeagueRow)
            .where(m.ManagerLeagueRow.manager_key == KEY)
            .values(analysis_json=stale)
        )
    first = client.get(f"/api/v1/leagues/{LEAGUE_ID}", params={"manager_key": KEY}).json()
    assert pinned == [served]  # the recompute ran on the API's snapshot
    assert first["status"] == "succeeded" and first["analysis"]["snapshot_id"] == served
    assert first["staleness"]["reasons"] == []
    for _ in range(3):  # further views neither recompute nor fail
        again = client.get(f"/api/v1/leagues/{LEAGUE_ID}", params={"manager_key": KEY}).json()
        assert again["status"] == "succeeded"
        assert again["analysis"]["computed_at"] == first["analysis"]["computed_at"]
    assert pinned == [served]
