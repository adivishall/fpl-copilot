"""Mini-league room (M1.8): a classic league the manager follows, with observed standings and a
modelled outlook against the nearest rivals.

What is *observed* comes from the public FPL API through the paced client — the league's
standings and, after a deadline, every entry's picks for that gameweek (public data, no
credentials). What is *modelled* comes from the serving forecast's joint simulation: both
squads are scored sample by sample on the same draws (``fpl_decision.league``), so a shared
player cancels out and P(ahead) is a paired probability, never a rank projection. Rivals'
transfers before the next deadline are unknowable and are not guessed: a rival's squad is their
last-deadline picks, said so in the payload; a rival whose picks could not be read is listed as
unavailable, never invented.

Everything is keyed by the importing manager (``manager_leagues``): the row is exported and
erased with the manager, holds at most the nearest rivals' public standings and picks, and is
overwritten on every refresh, so the footprint stays bounded without a retention policy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import structlog
from pydantic import ValidationError
from sqlalchemy import Engine, delete, select

from fpl_api.copilot import lineup_and_captaincy
from fpl_api.services import CurrentContext, sources_config
from fpl_decision.league import (
    captain_vs_rivals,
    rival_ownership,
    standing_outlook,
    template_gaps,
)
from fpl_domain.enums import Position
from fpl_domain.squad import Lineup
from fpl_domain.state import ManagerState
from fpl_forecasting.pipeline import Forecast
from fpl_ingestion.http import HttpStatusError, SourceUnavailableError
from fpl_ingestion.sources import fpl_api_schemas as fs
from fpl_ingestion.sources.fpl_api import FplApiClient
from fpl_storage import models as m
from fpl_storage.db import session_scope

LEAGUE_VERSION = "league-1"
JOB_KIND = "league_refresh"
MAX_LEAGUES_PER_MANAGER = 5
MAX_STANDINGS_PAGES = 3  # the manager must appear within the first 150 places
RIVALS_ABOVE = 5
RIVALS_BELOW = 5
REFRESH_COOLDOWN = timedelta(minutes=10)
TEMPLATE_THRESHOLD = 30.0  # global ownership (%) from which a player I lack is a template gap
CAPTAIN_CANDIDATES = 5
POPULAR_LIMIT = 10
TEMPLATE_LIMIT = 8

log = structlog.get_logger("fpl_api.league")
ClientFactory = Callable[[], FplApiClient]


class LeagueError(Exception):
    """A refresh that could not produce league data; the message is shown to the manager."""

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


def client_factory(svc: Any) -> ClientFactory | None:
    """How a refresh reaches the FPL API: an injected factory (tests and the e2e stack, no
    network), else the configured client when live sync is enabled, else nothing."""
    injected = svc.extras.get("fpl_client_factory")
    if injected is not None:
        return injected
    if not svc.settings.live_sync_enabled:
        return None
    return lambda: FplApiClient.from_config(sources_config())


def picks_gameweek(ctx: CurrentContext) -> int | None:
    """The latest gameweek whose deadline has passed: the only one with public picks."""
    return ctx.gameweek - 1 if ctx.gameweek > 1 else None


def element_map(ds: Any, season: str) -> dict[int, int]:
    """FPL element id → player code for the season (the registry keeps the source id)."""
    p = ds["players"]
    p = p[p["season"] == season]
    return {int(e): int(c) for e, c in zip(p["source_id"], p["player_code"], strict=True)}


def positions_map(ds: Any, season: str) -> dict[int, Position]:
    p = ds["players"]
    p = p[p["season"] == season]
    return {
        int(c): Position.parse(str(v)) for c, v in zip(p["player_code"], p["position"], strict=True)
    }


# ----------------------------------------------------------------------------- fetching


@dataclass
class FetchedLeague:
    league_id: int
    league_name: str | None
    picks_gameweek: int | None
    standings: list[dict[str, Any]]
    me: dict[str, Any]
    rivals: list[dict[str, Any]]
    pages: int
    requests: int
    fetched_at: str
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "league_id": self.league_id,
            "league_name": self.league_name,
            "picks_gameweek": self.picks_gameweek,
            "standings": self.standings,
            "me": self.me,
            "rivals": self.rivals,
            "pages": self.pages,
            "requests": self.requests,
            "fetched_at": self.fetched_at,
            "warnings": self.warnings,
            "source": "fantasy.premierleague.com (public classic-league standings and entry "
            "picks after each deadline; no credentials)",
        }


def _standing(r: fs.ApiStanding) -> dict[str, Any]:
    return {
        "entry": r.entry,
        "entry_name": r.entry_name,
        "player_name": r.player_name,
        "rank": r.rank,
        "last_rank": r.last_rank,
        "total": r.total,
        "event_total": r.event_total,
    }


def select_rivals(
    standings: list[dict[str, Any]],
    entry_id: int,
    above: int = RIVALS_ABOVE,
    below: int = RIVALS_BELOW,
) -> list[dict[str, Any]]:
    """The entries directly above and below the manager, plus the leader: the rivals a
    manager actually races, bounded so a refresh stays a few dozen paced requests."""
    ordered = sorted(standings, key=lambda r: (r["rank"], r["entry"]))
    idx = next((i for i, r in enumerate(ordered) if r["entry"] == entry_id), None)
    if idx is None:
        return []
    chosen = ordered[max(0, idx - above) : idx] + ordered[idx + 1 : idx + 1 + below]
    if ordered and ordered[0]["entry"] != entry_id and ordered[0] not in chosen:
        chosen.insert(0, ordered[0])
    seen: set[int] = set()
    out = []
    for r in chosen:
        if r["entry"] not in seen:
            seen.add(r["entry"])
            out.append(r)
    return out


def lineup_from_picks(
    picks: fs.EntryPicks, elements: Mapping[int, int]
) -> tuple[Lineup | None, list[int]]:
    """A rival's picks as a lineup of player codes; ``None`` with the unmapped element ids when
    any of their players is unknown to this data snapshot (nothing is guessed)."""
    ordered = sorted(picks.picks, key=lambda p: p.position)
    unmapped = sorted({p.element for p in ordered if p.element not in elements})
    if unmapped:
        return None, unmapped
    code = {p.element: elements[p.element] for p in ordered}
    starters = tuple(code[p.element] for p in ordered if p.position <= 11)
    bench = tuple(code[p.element] for p in ordered if p.position > 11)
    cap = next((code[p.element] for p in ordered if p.is_captain), None)
    vice = next((code[p.element] for p in ordered if p.is_vice_captain), None)
    if cap is None or vice is None:
        return None, []
    try:
        return Lineup(starters=starters, bench=bench, captain=cap, vice_captain=vice), []
    except ValidationError:
        return None, []


def fetch_league(
    client: FplApiClient,
    league_id: int,
    entry_id: int,
    gw: int | None,
    elements: Mapping[int, int],
    now: datetime,
) -> FetchedLeague:
    """Standings pages until the manager is found, then the nearest rivals' history (chips) and
    picks of the last passed deadline; one paced request each. A rival that cannot be read is
    marked unavailable; an outage stops further rival requests (the rest are unavailable too)."""
    standings: list[dict[str, Any]] = []
    name: str | None = None
    pages = requests = 0
    found = False
    for page in range(1, MAX_STANDINGS_PAGES + 1):
        try:
            _, ls = client.league_standings(league_id, page)
        except HttpStatusError as exc:
            if exc.status == 404:
                raise LeagueError("no classic league with this ID", False) from exc
            raise LeagueError(f"the FPL API refused the request (HTTP {exc.status})", True) from exc
        except SourceUnavailableError as exc:
            raise LeagueError("live FPL API unavailable from this deployment", True) from exc
        except ValidationError as exc:
            log.warning("fpl_api_contract_violation", errors=exc.error_count())
            raise LeagueError(
                "the FPL API answered, but its payload failed validation", True
            ) from exc
        requests += 1
        pages += 1
        if ls.league is not None:
            name = ls.league.name
        for res in ls.standings.results:
            standings.append(_standing(res))
            if res.entry == entry_id:
                found = True
        if found or not ls.standings.has_next:
            break
    if not found:
        raise LeagueError(
            f"your FPL team is not in the first {len(standings)} places of this league; "
            "check the league ID and your FPL ID (only leagues where you are in the top "
            f"{MAX_STANDINGS_PAGES * 50} are supported)",
            False,
        )
    me = next(r for r in standings if r["entry"] == entry_id)
    warnings: list[str] = []
    rivals: list[dict[str, Any]] = []
    outage: str | None = None
    for r in select_rivals(standings, entry_id):
        row: dict[str, Any] = {
            **r,
            "picks_gameweek": gw,
            "lineup": None,
            "active_chip": None,
            "chips_used": [],
            "event_transfers": None,
            "event_transfers_cost": None,
            "unavailable": None,
            "unmapped": [],
            "note": None,
        }
        if gw is None:
            row["unavailable"] = "no deadline has passed yet: picks are not public before it"
        elif outage:
            row["unavailable"] = outage
        else:
            try:
                _, hist = client.entry_history(int(r["entry"]))
                requests += 1
                played = {c.event: c.name for c in hist.chips}
                row["chips_used"] = sorted(
                    {fs.CHIP_NAME_MAP.get(c.name, c.name) for c in hist.chips}
                )
                squad_gw = gw
                if played.get(gw) == "freehit" and gw > 1:
                    squad_gw = gw - 1  # the Free Hit squad reverts after the gameweek
                    row["note"] = f"played Free Hit in GW{gw}: their squad is the GW{gw - 1} one"
                _, picks = client.entry_picks(int(r["entry"]), squad_gw)
                requests += 1
                row["active_chip"] = (
                    fs.CHIP_NAME_MAP.get(played[gw], played[gw]) if gw in played else None
                )
                row["event_transfers"] = picks.entry_history.event_transfers
                row["event_transfers_cost"] = picks.entry_history.event_transfers_cost
                lineup, unmapped = lineup_from_picks(picks, elements)
                row["unmapped"] = unmapped
                if lineup is None:
                    row["unavailable"] = (
                        f"{len(unmapped)} of their players are not in this data snapshot"
                        if unmapped
                        else "their picks could not be read as a legal lineup"
                    )
                else:
                    row["lineup"] = lineup.model_dump()
            except HttpStatusError as exc:
                row["unavailable"] = (
                    f"their picks for GW{gw} are not available"
                    if exc.status == 404
                    else f"the FPL API refused their picks (HTTP {exc.status})"
                )
            except SourceUnavailableError:
                outage = "live FPL API unavailable: picks not fetched"
                row["unavailable"] = outage
            except ValidationError as exc:
                log.warning("fpl_api_contract_violation", errors=exc.error_count())
                row["unavailable"] = "their payload failed contract validation"
        rivals.append(row)
    if outage:
        warnings.append(outage)
    return FetchedLeague(
        league_id=league_id,
        league_name=name,
        picks_gameweek=gw,
        standings=standings,
        me=me,
        rivals=rivals,
        pages=pages,
        requests=requests,
        fetched_at=now.isoformat(),
        warnings=warnings,
    )


# ----------------------------------------------------------------------------- analysis


def compute_league_analysis(
    svc: Any,
    ctx: CurrentContext,
    fc: Forecast,
    manager_key: str,
    state_id: str,
    st: ManagerState,
    data: dict[str, Any],
) -> dict[str, Any]:
    """The outlook for the next gameweek on the serving forecast: head-to-head and P(above
    after the gameweek) per rival with a known squad, the captain view against the rivals
    directly above and below, what the rivals hold that I do not, and template gaps."""
    season, gw = ctx.season, ctx.gameweek
    names = svc.data.names(season)
    rs = svc.data.ruleset(season)
    positions = positions_map(svc.data.ds, season)
    positions.update(st.positions)
    lu = lineup_and_captaincy(svc, ctx, fc, st)
    mine = Lineup.model_validate(lu["lineup"])
    sim = fc.simulation
    my_total = int(data["me"]["total"])
    lineups: dict[str, Lineup] = {}
    gaps: dict[str, int] = {}
    for r in data["rivals"]:
        if r.get("lineup"):
            key = str(r["entry"])
            lineups[key] = Lineup.model_validate(r["lineup"])
            gaps[key] = int(r["total"]) - my_total
    outlook = {o.rival: o for o in standing_outlook(sim, mine, lineups, gaps, gw, positions, rs)}
    me_rank = int(data["me"]["rank"])
    known = [r for r in data["rivals"] if str(r["entry"]) in lineups]
    above = max(
        (r for r in known if int(r["rank"]) < me_rank), key=lambda r: int(r["rank"]), default=None
    )
    below = min(
        (r for r in known if int(r["rank"]) > me_rank), key=lambda r: int(r["rank"]), default=None
    )
    targets = {str(r["entry"]): lineups[str(r["entry"])] for r in (above, below) if r is not None}
    candidates = [int(o["player_code"]) for o in lu["captaincy"]["options"][:CAPTAIN_CANDIDATES]]
    captain_rows: list[dict[str, Any]] = []
    if targets and candidates:
        table = captain_vs_rivals(sim, mine, candidates, targets, gaps, gw, positions, rs)
        for rec in table.to_dict("records"):
            captain_rows.append(
                {
                    "captain": int(rec["captain"]),
                    "expected_points": float(rec["expected_points"]),
                    "p_ahead_after": {
                        k.split(":", 1)[1]: float(v)
                        for k, v in rec.items()
                        if str(k).startswith("p_ahead_after:")
                    },
                }
            )
    shared, held = rival_ownership(lineups, set(st.codes))
    n_known = len(lineups)
    snap = svc.data.view(ctx.cutoff).latest_snapshot(season)
    ownership = (
        snap.set_index("player_code")["ownership"].dropna().astype(float)
        if (not snap.empty and "ownership" in snap.columns)
        else pd.Series(dtype=float)
    )
    gaps_df = template_gaps(set(st.codes), ownership, TEMPLATE_THRESHOLD)
    popular = [
        {"player_code": int(c), "rivals": int(n)}
        for c, n in sorted(shared.items(), key=lambda kv: (-kv[1], kv[0]))
        if n >= 2 or n_known == 1
    ][:POPULAR_LIMIT]
    rivals_out: list[dict[str, Any]] = []
    mentioned: set[int] = set(mine.players)
    for r in data["rivals"]:
        key = str(r["entry"])
        lu_r = lineups.get(key)
        o = outlook.get(key)
        row = {
            k: r.get(k)
            for k in (
                "entry",
                "entry_name",
                "player_name",
                "rank",
                "last_rank",
                "total",
                "event_total",
                "picks_gameweek",
                "chips_used",
                "active_chip",
                "event_transfers",
                "event_transfers_cost",
                "unavailable",
                "note",
            )
        }
        row["points_gap"] = int(r["total"]) - my_total
        row["captain"] = lu_r.captain if lu_r else None
        row["squad"] = list(lu_r.players) if lu_r else None
        row["their_differentials"] = sorted(set(lu_r.starters) - set(mine.starters)) if lu_r else []
        row["my_differentials"] = sorted(set(mine.starters) - set(lu_r.starters)) if lu_r else []
        row["outlook"] = o.model_dump() if o else None
        rivals_out.append(row)
        if lu_r:
            mentioned.update(lu_r.players)
    mentioned.update(int(x["player_code"]) for x in popular)
    mentioned.update(int(c) for c in gaps_df["player_code"].tolist()[:TEMPLATE_LIMIT])
    pg = data.get("picks_gameweek")
    return {
        "version": LEAGUE_VERSION,
        "computed_at": datetime.now(UTC).isoformat(),
        "manager_key": manager_key,
        "state_id": state_id,
        "snapshot_id": svc.data.snapshot_id,
        "forecast_key": str(fc.provenance.get("forecast_key", fc.run_id)),
        "season": season,
        "gameweek": gw,
        "picks_gameweek": pg,
        "n_simulations": int(sim.n_sims),
        "me": {
            **{
                k: data["me"].get(k)
                for k in ("entry", "entry_name", "rank", "last_rank", "total", "event_total")
            },
            "lineup": lu["lineup"],
            "captain": mine.captain,
            "expected_points": lu["expected_points"],
        },
        "rivals": rivals_out,
        "league": {
            "rivals": len(data["rivals"]),
            "rivals_with_squads": n_known,
            "popular_not_mine": popular,
            "my_unique_starters": [c for c in mine.starters if held.get(c, 0) == 0]
            if n_known
            else [],
            "template_gaps": [
                {"player_code": int(c), "ownership": float(o)}
                for c, o in zip(gaps_df["player_code"], gaps_df["ownership"], strict=True)
            ][:TEMPLATE_LIMIT],
            "template_threshold": TEMPLATE_THRESHOLD,
        },
        "captain_vs_rivals": {
            "above": str(above["entry"]) if above else None,
            "below": str(below["entry"]) if below else None,
            "rows": captain_rows,
        },
        "assumptions": [
            (
                f"rival squads are their picks at the GW{pg} deadline (public data); transfers "
                f"they make before the GW{gw} deadline are unknown and not guessed"
                if pg
                else "no deadline has passed yet, so no rival squad is known"
            ),
            "rivals play no chip and take no hit this gameweek; your XI is the lineup "
            "optimiser's for your current squad with no transfer",
            f"probabilities are shares of the serving forecast's {int(sim.n_sims)} joint "
            "simulation samples, both squads scored on the same draws so shared players cancel "
            "out; standings are observed, everything else is modelled",
            "'above after this gameweek' compares your gameweek points minus theirs with the "
            "observed points gap; FPL tie-breaks are not modelled (level is shown separately)",
        ],
        "names": {str(c): names.get(c) for c in sorted(mentioned)},
    }


# ----------------------------------------------------------------------------- store


def _row(r: m.ManagerLeagueRow, full: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "manager_key": r.manager_key,
        "league_id": r.league_id,
        "entry_id": r.entry_id,
        "league_name": r.league_name,
        "status": r.status,
        "job_id": r.job_id,
        "error": r.error,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
        "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None,
        "computed_at": r.computed_at.isoformat() if r.computed_at else None,
    }
    if full:
        out["data"] = dict(r.data_json) if r.data_json else None
        out["analysis"] = dict(r.analysis_json) if r.analysis_json else None
    return out


@dataclass
class LeagueStore:
    engine: Engine

    def for_manager(self, manager_key: str) -> list[dict[str, Any]]:
        with session_scope(self.engine) as s:
            rows = s.scalars(
                select(m.ManagerLeagueRow)
                .where(m.ManagerLeagueRow.manager_key == manager_key)
                .order_by(m.ManagerLeagueRow.created_at)
            ).all()
            return [_row(r, full=False) for r in rows]

    def get(self, manager_key: str, league_id: int) -> dict[str, Any] | None:
        with session_scope(self.engine) as s:
            r = s.get(m.ManagerLeagueRow, (manager_key, league_id))
            return _row(r) if r else None

    def queue(self, manager_key: str, league_id: int, entry_id: int) -> dict[str, Any]:
        """Attach the league (or re-queue a refresh of an attached one): status ``queued``."""
        now = datetime.now(UTC)
        with session_scope(self.engine) as s:
            r = s.get(m.ManagerLeagueRow, (manager_key, league_id))
            if r is None:
                r = m.ManagerLeagueRow(
                    manager_key=manager_key,
                    league_id=league_id,
                    entry_id=entry_id,
                    status="queued",
                    updated_at=now,
                )
                s.add(r)
                s.flush()
            else:
                r.entry_id, r.status, r.error, r.job_id, r.updated_at = (
                    entry_id,
                    "queued",
                    None,
                    None,
                    now,
                )
            return _row(r)

    def set_job(self, manager_key: str, league_id: int, job_id: str) -> None:
        with session_scope(self.engine) as s:
            r = s.get(m.ManagerLeagueRow, (manager_key, league_id))
            if r is not None:
                r.job_id = job_id

    def start(self, manager_key: str, league_id: int) -> bool:
        with session_scope(self.engine) as s:
            r = s.get(m.ManagerLeagueRow, (manager_key, league_id))
            if r is None:
                return False
            r.status, r.updated_at = "running", datetime.now(UTC)
            return True

    def finish(
        self,
        manager_key: str,
        league_id: int,
        *,
        data: dict[str, Any] | None,
        fetched: bool,
        analysis: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        """Store what a refresh produced: new data when fetched, the analysis when computed,
        and ``failed`` with the reason when neither could be (earlier data is kept)."""
        now = datetime.now(UTC)
        with session_scope(self.engine) as s:
            r = s.get(m.ManagerLeagueRow, (manager_key, league_id))
            if r is None:
                return
            if fetched and data is not None:
                r.data_json, r.fetched_at = data, now
                r.league_name = (data.get("league_name") or r.league_name or None) and str(
                    data.get("league_name") or r.league_name
                )[:200]
            if analysis is not None:
                r.analysis_json, r.computed_at = analysis, now
            r.status = "failed" if error else "succeeded"
            r.error = error[:2000] if error else None
            r.updated_at = now

    def delete(self, manager_key: str, league_id: int) -> bool:
        with session_scope(self.engine) as s:
            res = s.execute(
                delete(m.ManagerLeagueRow).where(
                    m.ManagerLeagueRow.manager_key == manager_key,
                    m.ManagerLeagueRow.league_id == league_id,
                )
            )
            return bool(getattr(res, "rowcount", 0))

    def export(self, manager_key: str) -> list[dict[str, Any]]:
        with session_scope(self.engine) as s:
            rows = s.scalars(
                select(m.ManagerLeagueRow).where(m.ManagerLeagueRow.manager_key == manager_key)
            ).all()
            return [_row(r) for r in rows]

    def delete_manager(self, manager_key: str) -> int:
        with session_scope(self.engine) as s:
            res = s.execute(
                delete(m.ManagerLeagueRow).where(m.ManagerLeagueRow.manager_key == manager_key)
            )
            return int(getattr(res, "rowcount", 0) or 0)

    def all_with_data(self) -> list[tuple[str, int]]:
        """Every attached league that has data: the scheduler recomputes their outlook after a
        forecast precompute (no fetch)."""
        with session_scope(self.engine) as s:
            rows = s.execute(
                select(m.ManagerLeagueRow.manager_key, m.ManagerLeagueRow.league_id).where(
                    m.ManagerLeagueRow.data_json.is_not(None)
                )
            ).all()
            return [(str(k), int(lid)) for k, lid in rows]


# ----------------------------------------------------------------------------- the job


def cooldown_remaining(row: Mapping[str, Any], now: datetime) -> timedelta:
    fetched = row.get("fetched_at")
    if not fetched:
        return timedelta(0)
    at = datetime.fromisoformat(str(fetched))
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(timedelta(0), REFRESH_COOLDOWN - (now - at))


def refresh_league(svc: Any, manager_key: str, league_id: int, fetch: bool) -> str:
    """The job body: fetch (unless within the cooldown, or asked not to) and recompute the
    outlook for the manager's current squad on the serving forecast. Failures are stored on
    the row with their reason and re-raised so the job record shows them too."""
    store = svc.leagues
    row = store.get(manager_key, league_id)
    if row is None:
        raise LookupError(f"league {league_id} is not attached to {manager_key}")
    store.start(manager_key, league_id)
    now = datetime.now(UTC)
    data = row.get("data")
    fetched = False
    try:
        ctx = svc.context()
        if fetch and (data is None or cooldown_remaining(row, now) == timedelta(0)):
            factory = client_factory(svc)
            if factory is None:
                raise LeagueError("live FPL source disabled by configuration", False)
            got = fetch_league(
                factory(),
                league_id,
                int(row["entry_id"]),
                picks_gameweek(ctx),
                element_map(svc.data.ds, ctx.season),
                now,
            )
            data, fetched = got.as_dict(), True
        if data is None:
            raise LeagueError("no league data has been fetched yet", True)
        latest = svc.states.latest(manager_key)
        if latest is None:
            raise LeagueError("no squad stored for this manager; import or enter it first", True)
        sid, st = latest
        fc = svc.forecast_for(ctx, svc.horizon(None))
        analysis = compute_league_analysis(svc, ctx, fc, manager_key, sid, st, data)
    except LeagueError as exc:
        store.finish(
            manager_key, league_id, data=data, fetched=fetched, analysis=None, error=exc.message
        )
        raise
    except Exception as exc:
        store.finish(
            manager_key,
            league_id,
            data=data,
            fetched=fetched,
            analysis=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    store.finish(manager_key, league_id, data=data, fetched=fetched, analysis=analysis, error=None)
    return f"{manager_key}:{league_id}"
