"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Badge, Callout, Card, FreshnessBanner, PageHeader, PlayerName, Prob, State, useApi } from "@/components/ui";
import { track } from "@/lib/analytics";
import { ApiError, api, type AvailableLeagues, type LeagueList, type LeagueRivalView, type LeagueView } from "@/lib/api";
import { pct, signed, when } from "@/lib/format";
import { loadSettings } from "@/lib/settings";

// Mini-League room (M1.8). The standings are what FPL publishes (observed); each rival's squad
// is their picks at the last deadline (observed, public after it); every probability is the
// serving forecast's joint simulation with both squads scored on the same draws (modelled).
// Rivals' transfers before the next deadline are unknown and are never guessed. The page shows
// only what the API stored; it computes nothing itself.

function movement(rank: number, last: number | null) {
  if (last === null || last === 0 || last === rank) return <span className="muted">–</span>;
  return last > rank ? <span className="good-text" title={`up from ${last}`}>▲ {last - rank}</span> : <span className="bad-text" title={`down from ${last}`}>▼ {rank - last}</span>;
}

function gapText(gap: number) {
  if (gap === 0) return "level";
  return gap > 0 ? `${gap} ahead of you` : `${-gap} behind you`;
}

export default function LeaguePage() {
  const [settings] = useState(loadSettings);
  const key = settings.managerKey;
  const list = useApi<LeagueList>(key ? `/leagues?manager_key=${key}` : null, [key]);
  const [available, setAvailable] = useState<AvailableLeagues | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [view, setView] = useState<LeagueView | null>(null);
  const [viewErr, setViewErr] = useState<string | null>(null);
  const [leagueId, setLeagueId] = useState("");
  const [entryId, setEntryId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const shown = useRef<number | null>(null);

  const leagues = useMemo(() => list.data?.leagues ?? [], [list.data]);
  useEffect(() => {
    const first = leagues[0];
    if (selected === null && first) setSelected(first.league_id);
  }, [leagues, selected]);
  useEffect(() => {
    if (list.data?.entry_id) setEntryId((e) => e || String(list.data?.entry_id));
  }, [list.data?.entry_id]);
  useEffect(() => {
    if (!key || !list.data?.entry_id || !list.data.live_source) return;
    api<AvailableLeagues>(`/leagues/available?manager_key=${key}`).then(setAvailable).catch(() => setAvailable(null));
  }, [key, list.data?.entry_id, list.data?.live_source]);

  const load = useCallback(async (id: number) => {
    shown.current = id;
    try {
      const v = await api<LeagueView>(`/leagues/${id}?manager_key=${key}`);
      if (shown.current === id) {
        setView(v);
        setViewErr(null);
      }
    } catch (e) {
      if (shown.current === id) setViewErr(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }, [key]);
  useEffect(() => {
    if (selected === null) return;
    setView(null);
    void load(selected).then(() => track("league_viewed", { page: "league" }));
  }, [selected, load]);
  const pending = view !== null && (view.status === "queued" || view.status === "running");
  useEffect(() => {
    if (!pending || selected === null) return;
    const t = setTimeout(() => void load(selected), 3000);
    return () => clearTimeout(t);
  }, [pending, view, selected, load]);

  async function follow(id: number) {
    setBusy(true);
    setErr(null);
    try {
      const body: Record<string, unknown> = { manager_key: key, league_id: id };
      if (entryId) body.entry_id = Number(entryId);
      const v = await api<LeagueView>("/leagues", { method: "POST", body: JSON.stringify(body) });
      track("league_followed", { status: v.status });
      await list.reload();
      setSelected(id);
      shown.current = id;
      setView(v);
      setLeagueId("");
    } catch (e) {
      setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function refresh(fetch: boolean) {
    if (selected === null) return;
    setBusy(true);
    setErr(null);
    try {
      setView(await api<LeagueView>(`/leagues/${selected}/refresh`, { method: "POST", body: JSON.stringify({ manager_key: key, fetch }) }));
    } catch (e) {
      setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (selected === null) return;
    setBusy(true);
    try {
      await api(`/leagues/${selected}?manager_key=${key}`, { method: "DELETE" });
      setSelected(null);
      setView(null);
      await list.reload();
    } catch (e) {
      setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  }

  const a = view?.analysis ?? null;
  const data = view?.data ?? null;
  const names = a?.names ?? {};
  const rivalByEntry = useMemo(() => new Map((a?.rivals ?? []).map((r) => [r.entry, r])), [a]);
  const rivalLabel = (entry: string | null) => {
    if (!entry) return "—";
    const r = rivalByEntry.get(Number(entry));
    return r ? `${r.entry_name} (#${r.rank})` : entry;
  };
  const myEntry = data?.me.entry ?? view?.entry_id;
  const canFollow = !!list.data?.live_source && leagues.length < (list.data?.max_leagues ?? 5);
  const cooldownMin = view ? Math.ceil(view.cooldown_seconds / 60) : 0;

  return (
    <>
      <PageHeader
        title="Mini-League"
        lede="Your classic league as FPL publishes it, and — against the rivals around you — how likely you are to be above them after the next gameweek, from the same forecast and simulation as your plans. Standings are observed; every probability is modelled. Rivals' transfers before the deadline are unknown and are not guessed."
      />
      <FreshnessBanner f={view?.freshness} />
      <div className="grid">
        <Card title="Your leagues" testId="league-form" tone="brand">
          <State loading={list.loading} error={list.error} retry={list.reload} />
          {list.data && !list.data.has_squad ? <Callout tone="warn">Import or enter your squad first: the outlook compares your squad with your rivals'.</Callout> : null}
          {list.data && !list.data.live_source ? (
            <Callout tone="warn" testId="league-no-live">The live FPL source is disabled on this deployment, so no league can be read.</Callout>
          ) : null}
          {leagues.length ? (
            <div className="row" data-testid="league-tabs">
              {leagues.map((l) => (
                <button key={l.league_id} className={selected === l.league_id ? "" : "secondary"} onClick={() => setSelected(l.league_id)} data-testid={`league-tab-${l.league_id}`}>
                  {l.league_name ?? `League ${l.league_id}`} {l.status === "failed" ? <Badge tone="bad">failed</Badge> : null}
                </button>
              ))}
            </div>
          ) : list.data ? (
            <p className="muted">You follow no league yet.</p>
          ) : null}
          {available && available.leagues.length ? (
            <details data-testid="league-available">
              <summary className="small">Leagues of your FPL team ({available.leagues.length})</summary>
              <ul className="clean">
                {available.leagues.map((l) => (
                  <li key={l.league_id} className="small">
                    {l.name} · {l.kind} · you are {l.entry_rank ?? "?"} of {l.rank_count ?? "?"}{" "}
                    {l.supported ? (
                      <button className="secondary" disabled={busy || !canFollow || leagues.some((x) => x.league_id === l.league_id)} onClick={() => void follow(l.league_id)}>Follow</button>
                    ) : (
                      <span className="muted">only leagues where you are in the top {available.max_rank} are supported</span>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
          <div className="row">
            <label>
              League ID{" "}
              <input inputMode="numeric" pattern="[0-9]*" value={leagueId} onChange={(e) => setLeagueId(e.target.value.replace(/\D/g, "").slice(0, 9))} placeholder="e.g. 123456" data-testid="league-id" aria-label="League ID" />
            </label>
            <label>
              Your FPL ID{" "}
              <input inputMode="numeric" pattern="[0-9]*" value={entryId} onChange={(e) => setEntryId(e.target.value.replace(/\D/g, "").slice(0, 9))} placeholder={list.data?.entry_id ? "" : "required"} data-testid="league-entry" aria-label="Your FPL ID" />
            </label>
            <button disabled={busy || !canFollow || !leagueId || (!entryId && !list.data?.entry_id)} onClick={() => void follow(Number(leagueId))} data-testid="follow-league">
              {busy ? "Working…" : "Follow league"}
            </button>
          </div>
          <p className="small">The league ID is the number in the league's address on fantasy.premierleague.com (…/leagues/<strong>123456</strong>/standings/c). Up to {list.data?.max_leagues ?? 5} leagues; you must be in the first 150 places.</p>
          {err ? <p className="error" role="alert" data-testid="league-error">{err}</p> : null}
        </Card>

        {selected !== null ? (
          <Card title={view?.league_name ?? data?.league_name ?? `League ${selected}`} testId="league-status">
            {viewErr ? <p className="error" role="alert">{viewErr}</p> : null}
            {!view && !viewErr ? <p className="muted" role="status">Loading…</p> : null}
            {view ? (
              <>
                <p className="small" data-testid="league-state" data-state={view.status}>
                  <Badge tone={view.status === "succeeded" ? "good" : view.status === "failed" ? "bad" : "warn"}>{view.status}</Badge>{" "}
                  {pending ? "reading the league in the background; this page updates by itself" : null}
                  {view.fetched_at ? <> · standings read {when(view.fetched_at)}</> : null}
                  {view.computed_at ? <> · outlook computed {when(view.computed_at)}</> : null}
                </p>
                {view.status === "failed" && view.error ? (
                  <Callout tone="bad" testId="league-failed">
                    {view.error}
                    {data ? " — the last good read is shown below." : null}
                  </Callout>
                ) : null}
                {view.staleness.reasons.length ? (
                  <Callout tone="warn" testId="league-stale">{view.staleness.reasons.join(" · ")}</Callout>
                ) : null}
                <div className="row">
                  <button className="secondary" disabled={busy || pending || view.cooldown_seconds > 0 || !list.data?.live_source} onClick={() => void refresh(true)} data-testid="refresh-league">
                    {view.cooldown_seconds > 0 ? `Read again in ${cooldownMin} min` : "Read standings again"}
                  </button>
                  {view.status === "failed" && data ? (
                    <button className="secondary" disabled={busy || pending} onClick={() => void refresh(false)} data-testid="recompute-league">Recompute outlook</button>
                  ) : null}
                  <button className="danger" disabled={busy || pending} onClick={() => void remove()} data-testid="remove-league">Stop following</button>
                </div>
                {view.status === "succeeded" && data ? (
                  <p className="small" data-testid="plan-vs-rivals">
                    Plan transfers against these rivals in the Workbench:{" "}
                    <Link href={`/transfers?league=${selected}&perspective=protect`} data-testid="plan-protect">protect your lead</Link>
                    {" · "}
                    <Link href={`/transfers?league=${selected}&perspective=chase`} data-testid="plan-chase">chase the rivals above you</Link>
                  </p>
                ) : null}
              </>
            ) : null}
          </Card>
        ) : null}

        {data && view ? (
          <Card title={`Standings (observed${data.picks_gameweek ? `, after GW${data.picks_gameweek}` : ""})`} testId="standings" wide>
            <p className="small">As published by FPL when read ({when(data.fetched_at)}); {data.standings.length} entries read{data.pages > 1 ? ` over ${data.pages} pages` : ""}. Rivals analysed: the leader and up to five above and five below you.</p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th className="num">Rank</th><th>Move</th><th>Team</th><th className="num">GW</th><th className="num">Total</th><th>Gap</th></tr>
                </thead>
                <tbody>
                  {data.standings.map((r) => (
                    <tr key={r.entry} className={r.entry === myEntry ? "selected" : rivalByEntry.has(r.entry) ? undefined : "muted"} data-testid={`standing-${r.entry}`}>
                      <td className="num">{r.rank}</td>
                      <td>{movement(r.rank, r.last_rank)}</td>
                      <td>
                        {r.entry_name}{r.entry === myEntry ? <> <Badge tone="good">you</Badge></> : null}
                        <div className="small">{r.player_name}</div>
                      </td>
                      <td className="num">{r.event_total}</td>
                      <td className="num">{r.total}</td>
                      <td className="small">{r.entry === myEntry ? "—" : gapText(r.total - data.me.total)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        ) : null}

        {a ? (
          <Card title={`Head-to-head outlook for GW${a.gameweek} (modelled)`} testId="outlook" wide tone="accent">
            <p className="small">
              Your XI: the lineup optimiser's for your current squad (captain <PlayerName code={a.me.captain} names={names} />, {a.me.expected_points.toFixed(1)} expected points). A rival's squad is their GW{a.picks_gameweek} picks. <em>Above after GW{a.gameweek}</em> = your points minus theirs beat the current gap, in {a.n_simulations} paired simulations.
            </p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th className="num">Rank</th><th>Rival</th><th>P(above after GW{a.gameweek})</th><th>P(more points in GW{a.gameweek})</th><th>Your margin p10 / p90</th><th>Their captain</th><th>Chips used</th><th>Differentials</th></tr>
                </thead>
                <tbody>
                  {a.rivals.map((r) => <RivalRow key={r.entry} r={r} names={names} />)}
                </tbody>
              </table>
            </div>
            <p className="small">Probabilities are estimates from the forecast, not guarantees; ties on points are not resolved (FPL breaks them by transfers).</p>
          </Card>
        ) : null}

        {a && a.captain_vs_rivals.rows.length ? (
          <Card title="Captaincy against your nearest rivals" testId="captain-vs" wide>
            <p className="small">Each row re-scores your XI with that captain on the same samples. Protecting a lead favours the row that keeps you above the rival below; chasing favours the one that best overtakes the rival above. The highest expected points is not always either.</p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Captain</th><th className="num">Expected points</th><th>P(above {rivalLabel(a.captain_vs_rivals.above)})</th><th>P(stay above {rivalLabel(a.captain_vs_rivals.below)})</th></tr>
                </thead>
                <tbody>
                  {a.captain_vs_rivals.rows.map((row) => (
                    <tr key={row.captain} data-testid={`captain-${row.captain}`} className={row.captain === a.me.captain ? "baseline" : undefined}>
                      <td><PlayerName code={row.captain} names={names} />{row.captain === a.me.captain ? <> <Badge tone="neutral">current</Badge></> : null}</td>
                      <td className="num">{row.expected_points.toFixed(2)}</td>
                      <td>{a.captain_vs_rivals.above ? <Prob p={row.p_ahead_after[a.captain_vs_rivals.above]} /> : "—"}</td>
                      <td>{a.captain_vs_rivals.below ? <Prob p={row.p_ahead_after[a.captain_vs_rivals.below]} /> : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        ) : null}

        {a ? (
          <Card title="What your rivals hold" testId="rival-holdings">
            <p className="small">Counted over the {a.league.rivals_with_squads} rival squads that could be read ({a.league.rivals} analysed); nothing is estimated for the others.</p>
            <strong className="small">Owned by several rivals, not by you</strong>
            {a.league.popular_not_mine.length ? (
              <ul className="clean">
                {a.league.popular_not_mine.map((x) => (
                  <li key={x.player_code} className="small"><PlayerName code={x.player_code} names={names} /> — {x.rivals} of {a.league.rivals_with_squads}</li>
                ))}
              </ul>
            ) : <p className="muted small">none</p>}
            <strong className="small">Your starters no analysed rival owns</strong>
            <p className="small">{a.league.my_unique_starters.length ? a.league.my_unique_starters.map((c, i) => <span key={c}>{i ? ", " : ""}<PlayerName code={c} names={names} /></span>) : "none"}</p>
            <strong className="small">Owned by over {a.league.template_threshold}% of all FPL managers, not by you</strong>
            <p className="small">{a.league.template_gaps.length ? a.league.template_gaps.map((x, i) => <span key={x.player_code}>{i ? ", " : ""}<PlayerName code={x.player_code} names={names} /> ({x.ownership.toFixed(0)}%)</span>) : "none"}</p>
          </Card>
        ) : null}

        {a ? (
          <Card title="What this assumes" testId="league-assumptions">
            <ul className="clean">
              {a.assumptions.map((x) => <li key={x} className="small">{x}</li>)}
            </ul>
            <p className="small" data-testid="league-trace">
              snapshot {a.snapshot_id} · forecast {a.forecast_key} · squad state {a.state_id} · {a.n_simulations} simulations · computed {when(a.computed_at)} · {data?.source}
            </p>
          </Card>
        ) : null}
      </div>
    </>
  );
}

function RivalRow({ r, names }: { r: LeagueRivalView; names: Record<string, string | null> }) {
  const o = r.outlook;
  return (
    <tr data-testid={`rival-${r.entry}`} className={r.unavailable ? "muted" : undefined}>
      <td className="num">{r.rank}</td>
      <td>
        {r.entry_name}
        <div className="small">{r.player_name} · {gapText(r.points_gap)}{r.note ? ` · ${r.note}` : ""}</div>
      </td>
      {o ? (
        <>
          <td data-testid={`p-after-${r.entry}`}><Prob p={o.probability_ahead_after} />{o.probability_level_after >= 0.005 ? <span className="small"> (level {pct(o.probability_level_after)})</span> : null}</td>
          <td><Prob p={o.probability_ahead_this_gw} /></td>
          <td className="small nowrap">{signed(o.diff_p10, 0)} / {signed(o.diff_p90, 0)}</td>
          <td>{r.captain ? <PlayerName code={r.captain} names={names} /> : "—"}</td>
          <td className="small">{r.chips_used.length ? r.chips_used.join(", ") : "none"}</td>
          <td className="small">
            <details>
              <summary>{r.their_differentials.length} theirs · {r.my_differentials.length} yours</summary>
              <div>Theirs: {r.their_differentials.map((c, i) => <span key={c}>{i ? ", " : ""}<PlayerName code={c} names={names} /></span>)}</div>
              <div>Yours: {r.my_differentials.map((c, i) => <span key={c}>{i ? ", " : ""}<PlayerName code={c} names={names} /></span>)}</div>
            </details>
          </td>
        </>
      ) : (
        <td colSpan={6} className="small" data-testid={`rival-unavailable-${r.entry}`}>squad unavailable: {r.unavailable ?? "not read"} — nothing is estimated for this rival</td>
      )}
    </tr>
  );
}
