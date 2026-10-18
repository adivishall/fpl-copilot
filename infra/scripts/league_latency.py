"""Measure Mini-League room latency on a running stack (M1.8: reading a league is a paced job;
served views are instant).

Creates a throw-away manager with the operator key, imports a public FPL team by ID, then:

* ``follow``: ``POST /leagues`` — the time the API takes to answer (``202`` queued) and the time
  until the refresh job has read the standings and rivals' picks and computed the outlook;
* ``recompute``: ``POST /leagues/{id}/refresh`` with ``fetch: false`` — the outlook alone;
* ``view``: ``GET /leagues/{id}`` (p50 / p95) and ``GET /leagues`` (p50 / p95);
* the number of FPL requests the refresh made (from the stored read).

Only counts and timings are printed — never team or manager names. The manager (and with it the
league data) is erased at the end. Usage:
  FPL_OPS_KEY=… uv run python infra/scripts/league_latency.py --entry <FPL ID> --league <ID>
"""

from __future__ import annotations

import argparse
import os
import statistics
import time
from typing import Any

import httpx


def _pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, round(p * (len(s) - 1)))]


def _wait(c: httpx.Client, path: str, params: dict[str, Any], limit: float = 900) -> dict[str, Any]:
    t0 = time.perf_counter()
    while True:
        body = c.get(path, params=params).json()
        if body.get("status") not in ("queued", "running") or time.perf_counter() - t0 > limit:
            return dict(body)
        time.sleep(0.5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--entry", type=int, required=True)
    ap.add_argument("--league", type=int, required=True)
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()
    key = os.environ.get("FPL_OPS_KEY")
    if not key:
        raise SystemExit("FPL_OPS_KEY (an operator key) is required")
    c = httpx.Client(base_url=args.api + "/api/v1", headers={"x-api-key": key}, timeout=600)
    manager = f"lg-probe-{int(time.time())}"
    try:
        c.post(
            "/squad/sync", json={"manager_key": manager, "manager_id": args.entry}
        ).raise_for_status()
        q = {"manager_key": manager}
        t0 = time.perf_counter()
        r = c.post("/leagues", json={"manager_key": manager, "league_id": args.league})
        t_submit = (time.perf_counter() - t0) * 1000
        submit_code = r.status_code
        view = _wait(c, f"/leagues/{args.league}", q)
        t_follow = time.perf_counter() - t0
        data = view.get("data") or {}
        a = view.get("analysis") or {}
        rivals = a.get("rivals") or []
        t1 = time.perf_counter()
        c.post(f"/leagues/{args.league}/refresh", json={"manager_key": manager, "fetch": False})
        _wait(c, f"/leagues/{args.league}", q)
        t_recompute = time.perf_counter() - t1
        get_t, list_t = [], []
        for _ in range(args.n):
            s = time.perf_counter()
            c.get(f"/leagues/{args.league}", params=q).raise_for_status()
            get_t.append((time.perf_counter() - s) * 1000)
            s = time.perf_counter()
            c.get("/leagues", params=q).raise_for_status()
            list_t.append((time.perf_counter() - s) * 1000)
        again = c.post("/leagues", json={"manager_key": manager, "league_id": args.league})
    finally:
        c.delete(f"/managers/{manager}")
    rows = [
        (
            "POST /leagues (follow and read now)",
            f"{t_submit:.0f} ms (HTTP {submit_code}); {view.get('status')} after {t_follow:.1f} s",
        ),
        (
            "FPL requests made by the read",
            f"{data.get('requests')} ({data.get('pages')} standings page(s), "
            f"{len(data.get('rivals') or [])} rivals)",
        ),
        (
            "rivals with a readable squad / analysed",
            f"{sum(1 for x in rivals if x.get('outlook'))} / {len(rivals)}",
        ),
        ("outlook recomputed without FPL (fetch: false)", f"{t_recompute:.1f} s"),
        (
            f"GET /leagues/{{id}} p50 / p95 of {args.n}",
            f"{statistics.median(get_t):.0f} / {_pct(get_t, 0.95):.0f} ms",
        ),
        (
            f"GET /leagues p50 / p95 of {args.n}",
            f"{statistics.median(list_t):.0f} / {_pct(list_t, 0.95):.0f} ms",
        ),
        ("POST /leagues again within the cooldown", f"HTTP {again.status_code}"),
    ]
    print("| measurement | result |\n|---|---|")
    for k, v in rows:
        print(f"| {k} | {v} |")


if __name__ == "__main__":
    main()
