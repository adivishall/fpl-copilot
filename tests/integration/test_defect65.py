# ruff: noqa: F811  (module-scoped API client and squad fixtures are shared with test_api)
"""Defect 65 (found on the deployment rehearsal of 2026-10-20): a squad saved for an earlier
gameweek than the serving snapshot was accepted by every planning route and failed in the
worker with the optimiser's "horizon must start at the state's gameweek". Now the routes
refuse it with a 409 that says what to do, and a job that still meets one fails with the
same reason instead of a traceback."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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
