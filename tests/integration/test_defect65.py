# ruff: noqa: F811  (module-scoped API client and squad fixtures are shared with test_api)
"""Defect 65 (found on the deployment rehearsal of 2026-10-20): a squad saved for an earlier
gameweek than the serving snapshot was accepted by every planning route and failed in the
worker with the optimiser's "horizon must start at the state's gameweek". Now the routes
refuse it with a 409 that says what to do, and a job that still meets one fails with the
same reason instead of a traceback."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fpl_api.container import build_recommendation
from fpl_api.jobs import _job_recommendation
from fpl_storage import models as m
from fpl_storage.db import session_scope
from tests.integration.test_api import client, squad  # noqa: F401  (module fixtures)


def _shift_state_gameweek(client: TestClient, manager_key: str, delta: int) -> int:
    """Rewrite the stored squad as if it had been saved `delta` gameweeks earlier/later —
    exactly what a passed deadline does to a squad nobody re-synced."""
    engine = client.app.state.services.engine  # type: ignore[attr-defined]
    with session_scope(engine) as s:
        rows = s.query(m.ManagerStateRow).all()
        mine = [r for r in rows if r.state_json.get("manager_key") == manager_key]
        assert mine, "no stored squad"
        for row in mine:
            payload = dict(row.state_json)
            payload["gameweek"] = payload["gameweek"] + delta
            row.state_json = payload
            row.gw = row.gw + delta
        return int(mine[0].state_json["gameweek"])


def test_planning_routes_refuse_a_squad_from_an_earlier_gameweek(
    client: TestClient, squad: list[int]
) -> None:
    gw = client.get("/api/v1/gameweeks/current").json()["gameweek"]
    assert (
        client.post("/api/v1/recommendations/generate", json={"manager_key": "demo"}).status_code
        == 202
    )
    _shift_state_gameweek(client, "demo", -1)
    try:
        for path, body in [
            ("/api/v1/recommendations/generate", {"manager_key": "demo"}),
            ("/api/v1/workbench/evaluate", {"manager_key": "demo", "plans": [{"kind": "hold"}]}),
            ("/api/v1/optimize/transfer", {"manager_key": "demo", "horizon": 2}),
            ("/api/v1/lineup", {"manager_key": "demo"}),
        ]:
            r = client.post(path, json=body)
            assert r.status_code == 409, (path, r.text)
            assert (
                f"saved for GW{gw - 1}" in r.json()["detail"]
                and f"now at GW{gw}" in r.json()["detail"]
            )
        r = client.get("/api/v1/workbench/context", params={"manager_key": "demo"})
        assert r.status_code == 409
        # reading the squad itself still works: the user must be able to see and re-save it
        assert client.get("/api/v1/squad", params={"manager_key": "demo"}).status_code == 200
    finally:
        _shift_state_gameweek(client, "demo", +1)
    assert client.post("/api/v1/lineup", json={"manager_key": "demo"}).status_code == 200


def test_a_job_that_meets_such_a_squad_fails_with_the_reason_not_a_traceback(
    client: TestClient, squad: list[int]
) -> None:
    svc = client.app.state.services  # type: ignore[attr-defined]
    sid, _ = svc.states.latest("demo")
    _shift_state_gameweek(client, "demo", -1)
    try:
        with pytest.raises(LookupError, match=r"saved for GW\d+ but the data is at GW\d+"):
            _job_recommendation(svc, {"manager_key": "demo", "state_id": sid})
    finally:
        _shift_state_gameweek(client, "demo", +1)


def test_the_engine_still_raises_the_original_error_so_the_guard_is_what_protects_users(
    client: TestClient, squad: list[int]
) -> None:
    """The original failure (defect 65) is reproduced below the guard: building a recommendation
    for a state one gameweek behind the serving snapshot raises the optimiser's error, exactly
    as the worker did before the fix."""
    svc = client.app.state.services  # type: ignore[attr-defined]
    sid, state = svc.states.latest("demo")
    behind = state.model_copy(update={"gameweek": state.gameweek - 1})
    with pytest.raises(ValueError, match="horizon must start at the state's gameweek"):
        build_recommendation(svc, "demo", behind, sid, persist=False)


def test_boundaries_a_squad_ahead_of_the_data_and_a_missing_squad(
    client: TestClient, squad: list[int]
) -> None:
    gw = client.get("/api/v1/gameweeks/current").json()["gameweek"]
    _shift_state_gameweek(client, "demo", +1)
    try:
        r = client.post("/api/v1/recommendations/generate", json={"manager_key": "demo"})
        assert r.status_code == 409 and f"saved for GW{gw + 1}" in r.json()["detail"]
    finally:
        _shift_state_gameweek(client, "demo", -1)
    # exactly the serving gameweek is accepted again
    assert client.post("/api/v1/lineup", json={"manager_key": "demo"}).status_code == 200
    # no squad at all keeps its own answer (404), not the gameweek message
    r = client.post("/api/v1/recommendations/generate", json={"manager_key": "nobody-here"})
    assert r.status_code == 404 and "no squad stored" in r.json()["detail"]
