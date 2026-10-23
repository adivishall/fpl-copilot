# ruff: noqa: F811  (module-scoped fixtures are shared with test_api and test_league_api)
"""Rival-aware transfer planning through the API (M1.9): the rivals of a followed league are
pinned into a workbench run as read (lineups, ranks, totals, gaps, the read's timestamp), the
selection follows the perspective, every plan carries paired metrics against them, the view
is owner-only, invalid selections are refused with the reason, unreadable rivals are listed
and never estimated, and a dropped league marks the view stale."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from fpl_storage import models as m
from fpl_storage.db import session_scope
from tests.integration.test_api import client, squad  # noqa: F401  (module fixtures)
from tests.integration.test_identity import _bearer, _manager, _register
from tests.integration.test_league_api import KEY, _save, fake  # noqa: F401
from tests.league_fake import LEAGUE_ID, MY_ENTRY, FakeFplClient

ME_TOTAL = 451  # rank 8 of 20, seven points apart from rank 1's 500


def _follow(client: TestClient) -> dict[str, Any]:
    r = client.get(f"/api/v1/leagues/{LEAGUE_ID}", params={"manager_key": KEY})
    if r.status_code == 200 and r.json()["status"] == "succeeded":
        return r.json()
    r = client.post(
        "/api/v1/leagues", json={"manager_key": KEY, "league_id": LEAGUE_ID, "entry_id": MY_ENTRY}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _plans(client: TestClient) -> list[dict[str, Any]]:
    c = client.get("/api/v1/workbench/context", params={"manager_key": KEY}).json()
    mid = next(p["player_code"] for p in c["players"] if p["position"] == "MID")
    return [{"kind": "replace", "out_player": mid, "candidates": 1}]


def _evaluate(client: TestClient, rivals: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"manager_key": KEY, "plans": _plans(client), "horizon": 2, **extra}
    if rivals is not None:
        body["rivals"] = rivals
    r = client.post("/api/v1/workbench/evaluate", json=body)
    assert r.status_code == 200, r.text  # inline jobs: computed before answering
    run = r.json()
    assert run["status"] == "succeeded", run.get("error")
    return run


def test_perspectives_pin_the_nearest_rivals_as_read_with_paired_metrics(
    client: TestClient, squad: list[int], fake: FakeFplClient
) -> None:
    _save(client, squad, bank=10)
    lg = _follow(client)
    run = _evaluate(client, {"league_id": LEAGUE_ID, "perspective": "protect"})
    pinned = run["request"]["rivals"]
    assert pinned["perspective"] == "protect" and pinned["league_id"] == LEAGUE_ID
    assert [x["entry"] for x in pinned["rivals"]] == [900_009, 900_010, 900_011]
    assert [x["points_gap"] for x in pinned["rivals"]] == [-7, -14, -21]
    assert pinned["my_rank"] == 8 and pinned["my_total"] == ME_TOTAL
    assert pinned["fetched_at"] == lg["fetched_at"]
    assert all(x["lineup"]["starters"] and x["name"] for x in pinned["rivals"])
    assert run["rivals_status"] == {"stale": False}
    res = run["result"]["result"]
    summary = res["rivals"]
    assert summary["perspective"] == "protect" and summary["n_samples"] == res["n_simulations"]
    assert [r["entry"] for r in summary["rivals"]] == ["900009", "900010", "900011"]
    assert summary["unavailable"] == []
    hold = res["plans"][0]
    assert hold["label"] == "hold" and hold["rivals"]["p_above_all_vs_hold"] == 0.0
    for pl in res["plans"]:
        rv = pl["rivals"]
        assert set(rv["per_rival"]) == {"900009", "900010", "900011"}
        assert rv["per_rival"]["900009"]["points_gap"] == -7
        assert 0.0 <= rv["p_above_all_after"] <= 1.0 and rv["n_samples"] == res["n_simulations"]
        assert abs(rv["expected_points_1gw"] - pl["expected_points_1gw"]) < 1e-9
    rk = summary["ranking"]
    valid = {p["label"] for p in res["plans"] if p["valid"]}
    assert rk["best_by_points"] in valid and rk["best_by_rivals"] in valid
    assert rk["agree"] == (rk["best_by_points"] == rk["best_by_rivals"])
    assert rk["explanation"] and rk["p_gain_se"] >= 0.0
    assert any("rival view" in a for a in res["assumptions"])

    chase = _evaluate(client, {"league_id": LEAGUE_ID, "perspective": "chase"})
    assert [x["entry"] for x in chase["request"]["rivals"]["rivals"]] == [900_007, 900_006, 900_005]
    assert [x["points_gap"] for x in chase["request"]["rivals"]["rivals"]] == [7, 14, 21]
    one = _evaluate(client, {"league_id": LEAGUE_ID, "perspective": "chase", "max_rivals": 1})
    assert [x["entry"] for x in one["request"]["rivals"]["rivals"]] == [900_007]
    leader = _evaluate(
        client, {"league_id": LEAGUE_ID, "perspective": "selected", "entries": [900_001]}
    )
    assert leader["request"]["rivals"]["rivals"][0]["points_gap"] == 49
    # a different selection is a different question; the same one is the same run
    assert len({run["id"], chase["id"], one["id"], leader["id"]}) == 4
    again = _evaluate(client, {"league_id": LEAGUE_ID, "perspective": "protect"})
    assert again["id"] == run["id"]
    # the plain evaluation is untouched by the rival view
    plain = _evaluate(client, None)
    assert plain["result"]["result"]["rivals"] is None
    assert all(p["rivals"] is None for p in plain["result"]["result"]["plans"])
    assert plain["rivals_status"] is None


def test_selections_are_validated_and_the_view_is_owner_only(
    client: TestClient, squad: list[int], fake: FakeFplClient
) -> None:
    _save(client, squad, bank=10)
    _follow(client)
    base: dict[str, Any] = {"manager_key": KEY, "plans": _plans(client), "horizon": 2}
    for rivals, status, text in [
        ({"league_id": LEAGUE_ID, "perspective": "selected"}, 422, "at least one rival"),
        ({"league_id": LEAGUE_ID, "entries": [123]}, 422, "not among"),
        ({"league_id": LEAGUE_ID, "perspective": "protect", "entries": [900_007]}, 422, "side"),
        ({"league_id": 999, "perspective": "protect"}, 404, "not followed"),
        ({"league_id": LEAGUE_ID, "entries": [1, 2, 3, 4, 5, 6]}, 422, ""),
    ]:
        r = client.post("/api/v1/workbench/evaluate", json={**base, "rivals": rivals})
        assert r.status_code == status, (rivals, r.text)
        assert text in r.text
    # another user with their own manager cannot plan against a league they do not follow,
    # and cannot read this manager's rival-aware run
    tok = _register(client, "rivals-other@example.test")["session_token"]
    other = _manager(client, tok, squad)
    r = client.post(
        "/api/v1/workbench/evaluate",
        json={
            "manager_key": other,
            "plans": [{"kind": "hold"}],
            "horizon": 2,
            "rivals": {"league_id": LEAGUE_ID, "perspective": "protect"},
        },
        headers=_bearer(tok),
    )
    assert r.status_code == 404 and "not followed" in r.text
    run = _evaluate(client, {"league_id": LEAGUE_ID, "perspective": "protect"})
    r = client.get(f"/api/v1/workbench/runs/{run['id']}", headers=_bearer(tok))
    assert r.status_code == 403


def _set_unavailable(client: TestClient, entries: set[int], reason: str) -> None:
    engine = client.app.state.services.engine  # type: ignore[attr-defined]
    with session_scope(engine) as s:
        row = s.get(m.ManagerLeagueRow, (KEY, LEAGUE_ID))
        assert row is not None and row.data_json
        data = dict(row.data_json)
        rivals = []
        for orig in data["rivals"]:
            r = dict(orig)
            if int(r["entry"]) in entries:
                r["lineup"], r["unavailable"] = None, reason
            rivals.append(r)
        data["rivals"] = rivals
        row.data_json = data


def test_unreadable_rivals_are_listed_and_never_estimated(
    client: TestClient, squad: list[int], fake: FakeFplClient
) -> None:
    _save(client, squad, bank=10)
    _follow(client)
    _set_unavailable(client, {900_009}, "picks not published")
    run = _evaluate(client, {"league_id": LEAGUE_ID, "perspective": "protect"})
    pinned = run["request"]["rivals"]
    assert [x["entry"] for x in pinned["rivals"]] == [900_010, 900_011, 900_012]
    assert pinned["unavailable"] == [
        {
            "entry": 900_009,
            "name": pinned["unavailable"][0]["name"],
            "reason": "picks not published",
        }
    ]
    assert run["result"]["result"]["rivals"]["unavailable"][0]["entry"] == 900_009
    assert "900009" not in run["result"]["result"]["plans"][0]["rivals"]["per_rival"]
    _set_unavailable(client, {900_009, 900_010, 900_011, 900_012, 900_013}, "their payload failed")
    r = client.post(
        "/api/v1/workbench/evaluate",
        json={
            "manager_key": KEY,
            "plans": _plans(client),
            "horizon": 2,
            "rivals": {"league_id": LEAGUE_ID, "perspective": "protect"},
        },
    )
    assert r.status_code == 422 and "readable squad" in r.text and "payload failed" in r.text


def test_a_dropped_league_marks_the_rival_view_stale(
    client: TestClient, squad: list[int], fake: FakeFplClient
) -> None:
    _save(client, squad, bank=10)
    _follow(client)
    run = _evaluate(client, {"league_id": LEAGUE_ID, "perspective": "chase", "max_rivals": 2})
    assert run["rivals_status"] == {"stale": False}
    assert (
        client.delete(f"/api/v1/leagues/{LEAGUE_ID}", params={"manager_key": KEY}).status_code
        == 200
    )
    got = client.get(f"/api/v1/workbench/runs/{run['id']}").json()
    assert (
        got["rivals_status"]["stale"] is True
        and "no longer followed" in got["rivals_status"]["reason"]
    )
    # the stored result itself is unchanged: it names the read it was computed on
    assert got["result"]["result"]["rivals"]["fetched_at"] == run["request"]["rivals"]["fetched_at"]
