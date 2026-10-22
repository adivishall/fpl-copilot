"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { RecordDecision } from "@/components/journal";
import { LifecycleBadge, WhatChanged } from "@/components/lifecycle";
import { PlayerPicker } from "@/components/player-picker";
import { Badge, Callout, Card, PageHeader, PlayerName, Prob, State, confidenceTone, useApi } from "@/components/ui";
import { track } from "@/lib/analytics";
import { ApiError, api, post, type PlanSpec, type PlayerRow, type WorkbenchContext, type WorkbenchPlan, type WorkbenchPreferences, type WorkbenchRun } from "@/lib/api";
import { money, pct, signed, when } from "@/lib/format";
import { horizonMax, loadSettings } from "@/lib/settings";

// Transfer Workbench (M1.2; M1.7 ladder and preferences). Every number on this page comes from
// one evaluation run of the API: concrete plans (HOLD always; your transfer, the engine's
// replacements for a player, the optimiser's best plan, the transfer ladder — the best plan with
// exactly 1, 2, … transfers now) solved as whole-squad plans on the same forecast and
// simulation, each validated against the rules, with its hit cost separated from its gross gain.
// Players you keep or exclude constrain every plan of the run. The page never computes a plan
// itself.

const STATUS: Record<string, string> = { a: "available", d: "doubtful", i: "injured", s: "suspended", u: "unavailable", n: "not in squad" };

function actionTone(a: string): "good" | "warn" | "bad" | "neutral" {
  return a === "HOLD" ? "neutral" : a === "HIT" ? "warn" : "good";
}

export default function TransferWorkbench() {
  const [settings] = useState(loadSettings);
  const ctx = useApi<WorkbenchContext>(`/workbench/context?manager_key=${settings.managerKey}`);
  const [out, setOut] = useState<number | null>(null);
  const [mode, setMode] = useState<"replace" | "transfer">("replace");
  const [incoming, setIncoming] = useState<PlayerRow | null>(null);
  const [includeBest, setIncludeBest] = useState(true);
  const [allowHits, setAllowHits] = useState(true);
  const [maxTransfers, setMaxTransfers] = useState<string>("");
  const [ladder, setLadder] = useState(true);
  const [rungs, setRungs] = useState<string>(""); // "" = the API's default: every free transfer plus one at a hit
  const [locked, setLocked] = useState<number[]>([]);
  const [banned, setBanned] = useState<PlayerRow[]>([]);
  const [horizon, setHorizon] = useState<number>(settings.horizon);
  const [hMax, setHMax] = useState<number | undefined>(undefined);
  const [run, setRunState] = useState<WorkbenchRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  // the run this page is showing: a poll or a late response for another run never replaces it
  const shown = useRef<string | null>(null);
  const setRun = useCallback((r: WorkbenchRun | null, expect?: string) => {
    if (expect && shown.current !== expect) return;
    shown.current = r?.id ?? null;
    setRunState(r);
  }, []);
  // on load, show the manager's most recent run with its lifecycle verdict (never as current
  // unless the API says so)
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const latest = await api<{ runs: { id: string }[] }>(`/workbench/runs?manager_key=${settings.managerKey}&limit=1`);
        const id = latest.runs[0]?.id;
        if (!id || cancelled || shown.current) return;
        const r = await api<WorkbenchRun>(`/workbench/runs/${id}`);
        if (!cancelled && !shown.current) setRun(r);
      } catch {
        /* no previous run or not reachable: the page starts empty */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [settings.managerKey, setRun]);

  useEffect(() => {
    void horizonMax().then((m) => {
      setHMax(m);
      if (m) setHorizon((h) => Math.max(1, Math.min(h, m)));
    });
  }, []);

  const outPlayer = useMemo(() => ctx.data?.players.find((p) => p.player_code === out) ?? null, [ctx.data, out]);
  const squadCodes = useMemo(() => ctx.data?.players.map((p) => p.player_code) ?? [], [ctx.data]);
  const weak = useMemo(() => {
    const m = new Map<number, string[]>();
    for (const w of ctx.data?.weak_spots ?? []) for (const c of w.players) m.set(c, [...(m.get(c) ?? []), `${w.kind}: ${w.message}`]);
    return m;
  }, [ctx.data]);
  const pending = run !== null && (run.status === "queued" || run.status === "running");

  // a queued or running evaluation is polled until it finishes (the browser never waits on a solve)
  const refresh = useCallback(async () => {
    if (!run) return;
    const id = run.id;
    try {
      setRun(await api<WorkbenchRun>(`/workbench/runs/${id}`), id);
    } catch (e) {
      setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }, [run, setRun]);
  useEffect(() => {
    if (!pending) return;
    const t = setTimeout(() => void refresh(), 3000);
    return () => clearTimeout(t);
  }, [pending, run, refresh]);

  const preferences: WorkbenchPreferences | undefined = useMemo(() => {
    if (!locked.length && !banned.length) return undefined;
    return { locked: [...locked].sort((a, b) => a - b), banned: banned.map((b) => b.player_code).sort((a, b) => a - b) };
  }, [locked, banned]);

  async function evaluate() {
    if (!ctx.data) return;
    const plans: PlanSpec[] = [{ kind: "hold" }];
    if (out !== null && mode === "replace") plans.push({ kind: "replace", label: "replace", out_player: out, candidates: 3 });
    if (out !== null && mode === "transfer" && incoming) plans.push({ kind: "transfer", label: "your transfer", sells: [out], buys: [incoming.player_code] });
    if (ladder) plans.push({ kind: "ladder", label: "ladder", allow_hits: allowHits, max_transfers: rungs ? Number(rungs) : null });
    if (includeBest) plans.push({ kind: "best", label: "best", allow_hits: allowHits, max_transfers: maxTransfers ? Number(maxTransfers) : null, alternatives: 1 });
    await submit({ manager_key: settings.managerKey, horizon, profile: settings.profile, plans, preferences });
  }

  // retrying a failed run asks *its* question again (its plans, horizon, profile, preferences
  // and squad state), not whatever the form holds now: the same run is re-queued
  async function retry(r: WorkbenchRun) {
    await submit({ manager_key: settings.managerKey, state_id: r.state_id, horizon: r.request.horizon, profile: r.request.profile, plans: r.request.plans, preferences: r.request.preferences });
  }

  async function submit(body: { manager_key: string; state_id?: string; horizon: number; profile: string; plans: PlanSpec[]; preferences?: WorkbenchPreferences }) {
    setBusy(true);
    setErr(null);
    setSelected(null);
    try {
      // a new evaluation replaces whatever was shown: the previous run's plans leave the page
      // now (they are not the answer to this question), and while the request is in flight
      // nothing else (the initial latest-run load, a late poll) may put another run on it
      shown.current = "pending";
      setRunState(null);
      const r = await post<WorkbenchRun>("/workbench/evaluate", body.preferences ? body : { ...body, preferences: undefined });
      setRun(r);
      track("workbench_evaluated", { plans: body.plans.length, horizon: body.horizon });
    } catch (e) {
      shown.current = null;
      setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  }

  const res = run?.result?.result ?? null;
  const names = run?.names ?? {};
  const hold = res?.plans[0] ?? null;
  const chosen = res?.plans.find((p) => p.label === selected) ?? null;
  const c = ctx.data;
  const ladderPlans = useMemo(() => (res?.plans ?? []).filter((p) => p.kind === "ladder").sort((a, b) => (a.rung ?? 0) - (b.rung ?? 0)), [res]);
  const ladderExcluded = useMemo(() => (res?.excluded ?? []).filter((e) => e.rung !== undefined).sort((a, b) => (a.rung ?? 0) - (b.rung ?? 0)), [res]);
  const bestPlan = res?.plans.find((p) => p.kind === "best" && p.label === "best") ?? null;
  const runPrefs = (res?.problem as { preferences?: { locked?: number[]; banned?: number[] } } | undefined)?.preferences;
  const freeLeft = c ? Math.max(0, c.state.free_transfers - c.state.transfers_made) : 1;
  const defaultRungs = Math.min(3, Math.max(1, freeLeft + 1));
  function toggleLock(code: number) {
    setLocked((l) => (l.includes(code) ? l.filter((x) => x !== code) : [...l, code]));
  }

  return (
    <>
      <PageHeader
        title="Transfer Workbench"
        lede="Compare concrete plans for your whole squad against doing nothing: the same forecast and simulation for every plan, legality checked by the rules engine, hit costs shown separately. The workbench recommends; you decide."
      />
      <div className="grid">
        <Card title="Your squad" testId="wb-squad" wide>
          <State loading={ctx.loading} error={ctx.error} retry={ctx.reload} />
          {c ? (
            <>
              <p className="small" data-testid="wb-state">
                GW{c.gameweek} · bank {money(c.state.bank)} · {c.state.free_transfers} free transfer(s) · squad value {money(c.state.squad_value)} (sells for{" "}
                {money(c.state.selling_value)}) · chips available: {c.state.chips_available.length ? c.state.chips_available.join(", ") : "none"} · hit cost{" "}
                {c.rules.hit_cost} pts
              </p>
              <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Player</th><th>Pos</th><th>Club</th><th>Bought / now / sells</th><th className="num">xP next</th><th className="num">xP {c.horizon} GW</th><th className="num">Start</th><th>Status</th><th></th><th></th></tr>
                </thead>
                <tbody>
                  {c.players.map((p) => (
                    <tr key={p.player_code} className={out === p.player_code ? "selected" : undefined}>
                      <td>
                        <PlayerName code={p.player_code} names={{ [String(p.player_code)]: p.name }} />
                        {weak.get(p.player_code)?.map((w) => (
                          <div key={w} className="small"><Badge tone="warn">weak spot</Badge> {w}</div>
                        ))}
                      </td>
                      <td>{p.position}</td>
                      <td>{p.team}</td>
                      <td className="small nowrap">{money(p.purchase_price)} / {money(p.price)} / <strong>{money(p.selling_price)}</strong></td>
                      <td className="num">{p.xp_next.toFixed(1)}</td>
                      <td className="num">{p.xp_horizon.toFixed(1)}</td>
                      <td className="num">{pct(p.p_start_next)}</td>
                      <td><Badge tone={p.status === "a" ? "good" : p.status === "d" ? "warn" : "bad"}>{STATUS[p.status] ?? p.status}</Badge></td>
                      <td>
                        <button className={out === p.player_code ? "" : "secondary"} onClick={() => { setOut(p.player_code); setIncoming(null); }} data-testid={`out-${p.player_code}`} disabled={locked.includes(p.player_code)}>
                          {out === p.player_code ? "Selling" : "Sell"}
                        </button>
                      </td>
                      <td>
                        <button
                          className={locked.includes(p.player_code) ? "" : "secondary"}
                          onClick={() => { toggleLock(p.player_code); if (out === p.player_code) setOut(null); }}
                          data-testid={`lock-${p.player_code}`}
                          title="Keep this player in every plan of the evaluation (all gameweeks of the horizon)"
                        >
                          {locked.includes(p.player_code) ? "Kept" : "Keep"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            </>
          ) : null}
        </Card>

        <Card title="What to evaluate" testId="wb-plan" tone="brand">
          <p>
            Outgoing: {outPlayer ? <strong data-testid="out-name">{outPlayer.name}</strong> : <span className="muted">none (hold and the optimiser's plan only)</span>}
            {outPlayer ? <span className="small"> · sells for {money(outPlayer.selling_price)} → budget {money(outPlayer.selling_price + (c?.state.bank ?? 0))}</span> : null}
          </p>
          {outPlayer ? (
            <div className="row">
              <label><input type="radio" name="mode" checked={mode === "replace"} onChange={() => setMode("replace")} data-testid="mode-replace" /> engine shortlist (3 replacements)</label>
              <label><input type="radio" name="mode" checked={mode === "transfer"} onChange={() => setMode("transfer")} data-testid="mode-transfer" /> a specific replacement</label>
            </div>
          ) : null}
          {outPlayer && mode === "transfer" && c ? (
            <>
              <PlayerPicker
                onPick={setIncoming}
                exclude={squadCodes}
                position={outPlayer.position}
                maxPrice={outPlayer.selling_price + c.state.bank}
                label="Incoming player"
                testId="wb-picker"
              />
              <p className="small" data-testid="incoming">
                Incoming: {incoming ? `${incoming.name} (${incoming.team} · ${money(incoming.price)})` : "pick a player above"}
              </p>
            </>
          ) : null}
          <div className="row">
            <label>
              Horizon{" "}
              <select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))} data-testid="wb-horizon">
                {Array.from({ length: hMax ?? horizon }, (_, i) => i + 1).map((h) => (
                  <option key={h} value={h}>{h} GW</option>
                ))}
              </select>
            </label>
            <label><input type="checkbox" checked={ladder} onChange={(e) => setLadder(e.target.checked)} data-testid="include-ladder" /> transfer ladder (best plan with exactly 1, 2, … transfers now)</label>
            {ladder ? (
              <label>
                rungs{" "}
                <select value={rungs} onChange={(e) => setRungs(e.target.value)} data-testid="ladder-rungs">
                  <option value="">{`${defaultRungs} (free transfers + one hit)`}</option>
                  {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </label>
            ) : null}
            <label><input type="checkbox" checked={includeBest} onChange={(e) => setIncludeBest(e.target.checked)} data-testid="include-best" /> include the optimiser's best plan</label>
            {includeBest || ladder ? (
              <label><input type="checkbox" checked={allowHits} onChange={(e) => setAllowHits(e.target.checked)} data-testid="allow-hits" /> allow hits</label>
            ) : null}
            {includeBest ? (
              <label>
                max transfers now{" "}
                <select value={maxTransfers} onChange={(e) => setMaxTransfers(e.target.value)} data-testid="max-transfers">
                  <option value="">optimiser decides</option>
                  {[1, 2, 3].map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </label>
            ) : null}
          </div>
          <details data-testid="wb-preferences">
            <summary className="small">
              Constraints for every plan: {locked.length ? `${locked.length} kept` : "none kept"} · {banned.length ? `${banned.length} excluded` : "none excluded"}
            </summary>
            <p className="small">Kept players stay in the squad for the whole horizon (use the Keep buttons above); excluded players are never bought nor shortlisted.</p>
            {c ? (
              <PlayerPicker onPick={(p) => setBanned((b) => (b.some((x) => x.player_code === p.player_code) ? b : [...b, p]))} exclude={[...squadCodes, ...banned.map((b) => b.player_code)]} label="Exclude a player" testId="wb-exclude" />
            ) : null}
            {banned.length ? (
              <ul className="clean" data-testid="wb-excluded-list">
                {banned.map((b) => (
                  <li key={b.player_code} className="small">
                    {b.name} ({b.team}) <button className="secondary" onClick={() => setBanned((l) => l.filter((x) => x.player_code !== b.player_code))} data-testid={`unban-${b.player_code}`}>remove</button>
                  </li>
                ))}
              </ul>
            ) : null}
          </details>
          <p>
            <button onClick={() => void evaluate()} disabled={busy || pending || !c || (mode === "transfer" && out !== null && !incoming)} data-testid="evaluate">
              {busy ? "Submitting…" : pending ? "Evaluating…" : "Evaluate plans"}
            </button>
          </p>
          {err ? <p className="error" role="alert">{err}</p> : null}
          {run ? (
            <p className="small" data-testid="run-status">
              Run {run.id} · <Badge tone={run.status === "succeeded" ? "good" : run.status === "failed" ? "bad" : "warn"}>{run.status}</Badge>
              {pending ? " · solving in the background, this page updates by itself" : null}
              {run.status === "failed" ? (
                <>
                  {" "}· {run.error ?? run.job?.error ?? "the evaluation failed"} <button className="secondary" onClick={() => void retry(run)} data-testid="retry-run">Retry</button>
                </>
              ) : null}
            </p>
          ) : null}
          {run?.lifecycle ? (
            <div data-testid="run-lifecycle" data-state={run.lifecycle.state}>
              <LifecycleBadge lc={run.lifecycle} testId="run-lifecycle-badge" />
              {!run.lifecycle.current && run.lifecycle.state !== "pending" && run.lifecycle.state !== "failed" ? (
                <>
                  <WhatChanged path={`/workbench/runs/${run.id}/changes`} />
                  <p className="small">Evaluate again to compare plans for your current squad and the data served now.</p>
                </>
              ) : null}
            </div>
          ) : null}
          {run?.stale_state ? (
            <p className="error" data-testid="stale-run">
              Your squad changed after this run was requested (state {run.state_id} → {run.latest_state_id}); evaluate again for the current squad.
            </p>
          ) : null}
          {run?.stale_snapshot ? <p className="error" data-testid="stale-snapshot">The data snapshot changed since this run; evaluate again for current data.</p> : null}
        </Card>

        <Card title={run && run.lifecycle && !run.lifecycle.current && run.status === "succeeded" ? "Plans compared with holding (historical run — not current)" : "Plans compared with holding"} testId="plans" wide>
          {!res && !pending ? <Callout>Choose a player to sell (or just evaluate) to compare plans.</Callout> : null}
          {pending && !res ? <Callout tone="pending" role="status">Solving every plan on the whole squad…</Callout> : null}
          {res && hold ? (
            <>
              <p className="small" data-testid="wb-trace">
                {res.horizon} GW from GW{res.gameweek} · {res.n_simulations} simulations{res.simulation_seed != null ? ` (seed ${res.simulation_seed})` : ""} · snapshot{" "}
                {run?.result?.snapshot_id} · forecast {run?.result?.forecast_key} · squad state {run?.state_id} · computed {when(run?.result?.computed_at)} · optimiser {res.config_ref} ·
                rules {res.ruleset_version}
              </p>
              <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Plan</th><th>Moves now</th><th className="num">Net vs hold ({res.horizon} GW)</th><th className="num">Gross</th><th className="num">Hit cost</th><th>p10 / p90</th><th>P(&gt;hold)</th><th className="num">Next GW xP</th><th>Bank / FT after</th><th>Solver</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {res.plans.map((p) => (
                    <tr key={p.label} data-testid={`plan-${p.label}`} className={!p.valid ? "invalid" : p.label === "hold" ? "baseline" : undefined}>
                      <td>
                        <Badge tone={actionTone(p.action)}>{p.action}</Badge> <strong>{p.label}</strong>
                        {p.rung ? <> <Badge tone="neutral">exactly {p.rung} now</Badge></> : null}
                        <div className="small">{p.reason}{p.requested_by === "engine" ? " (engine)" : ""}</div>
                        {!p.valid ? <div className="error">illegal — not recommended: {p.issues.join("; ")}</div> : null}
                      </td>
                      <td className="small">
                        {p.sells.length === 0 ? "none" : p.sells.map((s, i) => (
                          <div key={s}><PlayerName code={s} names={names} /> → <PlayerName code={p.buys[i] ?? s} names={names} /></div>
                        ))}
                      </td>
                      <td className="num" data-testid={`net-${p.label}`}>{p.label === "hold" ? <span className="muted">baseline</span> : signed(p.gain_horizon.mean)}</td>
                      <td className="num">{p.label === "hold" ? "—" : signed(p.gross_gain_horizon)}</td>
                      <td className="num">{p.hit_cost_vs_hold ? `−${p.hit_cost_vs_hold}` : "0"}{p.hit_now ? ` (${p.hit_now} now)` : ""}</td>
                      <td className="small nowrap">{p.label === "hold" ? "—" : `${signed(p.gain_horizon.p10, 0)} / ${signed(p.gain_horizon.p90, 0)}`}</td>
                      <td>{p.label === "hold" ? "—" : <Badge tone={confidenceTone(p.gain_horizon.probability_positive)}>{pct(p.gain_horizon.probability_positive)}</Badge>}</td>
                      <td className="num">{p.expected_points_1gw.toFixed(1)}</td>
                      <td className="small nowrap">{money(p.bank_after)} / {p.free_transfers_after}</td>
                      <td title={p.optimality}>{p.proven_optimal ? <Badge tone="good">proven optimal</Badge> : <Badge tone="warn">{p.optimality.startsWith("best valid plan among those explored") ? "best explored" : "best within limit"}</Badge>}</td>
                      <td><button className="secondary" onClick={() => { setSelected(p.label); track("workbench_plan_selected", { kind: p.kind }); }} data-testid={`details-${p.label}`}>Details</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
              <p className="small">
                Net = expected points of the plan minus holding over the horizon, paired per simulation sample and net of hits; gross adds the hit cost back. Expected gains
                are estimates, not guarantees.
              </p>
              {res.excluded.length ? (
                <div data-testid="excluded">
                  <strong>Not evaluated</strong>
                  <ul className="clean">
                    {res.excluded.map((e) => <li key={e.label} className="small">{e.label}: {e.reason}</li>)}
                  </ul>
                </div>
              ) : null}
              {res.notes.length ? <p className="small">{res.notes.join(" · ")}</p> : null}
              {res.assumptions.length ? (
                <details data-testid="wb-assumptions">
                  <summary className="small">What this comparison assumes ({res.assumptions.length})</summary>
                  <ul className="clean">
                    {res.assumptions.map((a) => <li key={a} className="small">{a}</li>)}
                  </ul>
                </details>
              ) : null}
            </>
          ) : null}
        </Card>

        {res && hold && (ladderPlans.length || ladderExcluded.length) ? (
          <Card title="Transfer ladder: what each transfer buys" testId="ladder" wide tone="accent">
            <p className="small">
              Each rung is the optimiser's best plan with <em>exactly</em> that many transfers now, later gameweeks re-optimised, scored on the same samples as holding; a rung's hit follows from your free transfers.
              {bestPlan ? ` The unconstrained best plan makes ${bestPlan.transfers_now} transfer${bestPlan.transfers_now === 1 ? "" : "s"} now.` : ""}
              {runPrefs && (runPrefs.locked?.length || runPrefs.banned?.length) ? (
                <> Constraints: {runPrefs.locked?.length ? <>kept {runPrefs.locked.map((code) => <span key={code}><PlayerName code={code} names={names} /> </span>)}</> : null}{runPrefs.banned?.length ? <>excluded {runPrefs.banned.map((code) => <span key={code}><PlayerName code={code} names={names} /> </span>)}</> : null}</>
              ) : null}
            </p>
            <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Transfers now</th><th>Moves</th><th className="num">Hit now</th><th className="num">Net vs hold ({res.horizon} GW)</th><th className="num">Gross</th><th>p10 / p90</th><th>P(&gt;hold)</th><th>Bank / FT after</th><th>Solver</th><th></th></tr>
              </thead>
              <tbody>
                <tr className="baseline" data-testid="ladder-rung-0">
                  <td>0 (hold)</td><td className="small">none</td><td className="num">0</td><td className="num"><span className="muted">baseline</span></td><td className="num">—</td><td>—</td><td>—</td>
                  <td className="small nowrap">{money(hold.bank_after)} / {hold.free_transfers_after}</td>
                  <td>{hold.proven_optimal ? <Badge tone="good">proven optimal</Badge> : <Badge tone="warn">best within limit</Badge>}</td>
                  <td><button className="secondary" onClick={() => setSelected("hold")}>Details</button></td>
                </tr>
                {ladderPlans.map((p) => (
                  <tr key={p.label} data-testid={`ladder-rung-${p.rung}`} className={!p.valid ? "invalid" : undefined}>
                    <td><strong>{p.rung}</strong> <Badge tone={actionTone(p.action)}>{p.action}</Badge>{!p.valid ? <div className="error">illegal — not recommended: {p.issues.join("; ")}</div> : null}</td>
                    <td className="small">
                      {p.sells.map((s, i) => (
                        <div key={s}><PlayerName code={s} names={names} /> → <PlayerName code={p.buys[i] ?? s} names={names} /></div>
                      ))}
                    </td>
                    <td className="num">{p.hit_now ? `−${p.hit_now}` : "0"}</td>
                    <td className="num" data-testid={`ladder-net-${p.rung}`}>{signed(p.gain_horizon.mean)}</td>
                    <td className="num">{signed(p.gross_gain_horizon)}</td>
                    <td className="small nowrap">{signed(p.gain_horizon.p10, 0)} / {signed(p.gain_horizon.p90, 0)}</td>
                    <td><Badge tone={confidenceTone(p.gain_horizon.probability_positive)}>{pct(p.gain_horizon.probability_positive)}</Badge></td>
                    <td className="small nowrap">{money(p.bank_after)} / {p.free_transfers_after}</td>
                    <td title={p.optimality}>{p.proven_optimal ? <Badge tone="good">proven optimal</Badge> : <Badge tone="warn">{p.optimality.startsWith("best valid plan among those explored") ? "best explored" : "best within limit"}</Badge>}</td>
                    <td><button className="secondary" onClick={() => { setSelected(p.label); track("workbench_plan_selected", { kind: p.kind }); }} data-testid={`ladder-details-${p.rung}`}>Details</button></td>
                  </tr>
                ))}
                {ladderExcluded.map((e) => (
                  <tr key={e.label} className="muted" data-testid={`ladder-excluded-${e.rung}`}>
                    <td><strong>{e.rung}</strong></td><td colSpan={9} className="small">not evaluated: {e.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
            <p className="small">A later rung that scores below an earlier one means the extra transfer does not pay on this forecast. Expected gains are estimates, not guarantees.</p>
          </Card>
        ) : null}

        {chosen && hold ? (
          <Card title={`Plan: ${chosen.label}`} testId="plan-details" wide>
            <PlanDetails plan={chosen} hold={hold} names={names} />
            {run && run.status === "succeeded" && chosen.valid ? (
              <RecordDecision
                key={`${run.id}|${res?.plans.indexOf(chosen) ?? 0}:${chosen.label}`} // a fresh form per plan
                managerKey={settings.managerKey}
                source="workbench"
                sourceId={run.id}
                plan={`${res?.plans.indexOf(chosen) ?? 0}:${chosen.label}`}
                starters={chosen.lineup.starters}
                captain={chosen.lineup.captain}
                viceCaptain={chosen.lineup.vice_captain}
                names={names}
                current={!!run.lifecycle?.current}
              />
            ) : null}
          </Card>
        ) : null}
      </div>
    </>
  );
}

function PlanDetails({ plan, hold, names }: { plan: WorkbenchPlan; hold: WorkbenchPlan; names: Record<string, string | null> }) {
  const n = (c: number) => names[String(c)] ?? String(c);
  const outSet = new Set(plan.sells);
  return (
    <>
      <p>
        {plan.reason} · {plan.action} · {plan.valid ? "passes the independent rules check" : `illegal: ${plan.issues.join("; ")}`} · {plan.optimality}
      </p>
      {plan.moves_in.length ? (
        <div className="table-wrap">
        <table>
          <thead><tr><th></th><th>Player</th><th>Pos</th><th>Club</th><th>Price</th><th className="num">xP next</th><th className="num">xP horizon</th><th className="num">Start</th></tr></thead>
          <tbody>
            {plan.moves_out.map((m) => (
              <tr key={`o${m.player_code}`}><td>out</td><td>{m.name}</td><td>{m.position}</td><td>{m.team}</td><td>sells for {money(m.selling_price)}</td><td>{m.xp_next.toFixed(1)}</td><td>{m.xp_horizon.toFixed(1)}</td><td>{pct(m.p_start_next)}</td></tr>
            ))}
            {plan.moves_in.map((m) => (
              <tr key={`i${m.player_code}`}><td>in</td><td>{m.name}</td><td>{m.position}</td><td>{m.team}</td><td>{money(m.price)}</td><td className="num">{m.xp_next.toFixed(1)}</td><td className="num">{m.xp_horizon.toFixed(1)}</td><td className="num">{pct(m.p_start_next)}</td></tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : null}
      <p className="small" data-testid="squad-impact">
        Squad after: XI {plan.lineup.starters.map(n).join(", ")} · bench {plan.lineup.bench.map(n).join(", ")} · captain {n(plan.lineup.captain)} (vice {n(plan.lineup.vice_captain)}) · bank{" "}
        {money(plan.bank_after)} · {plan.free_transfers_after} free transfer(s) next GW
        {plan.squad_after.filter((c) => !outSet.has(c)).length !== 15 ? "" : ""}
      </p>
      <div className="table-wrap">
      <table>
        <thead><tr><th>GW</th><th>Action</th><th>Transfers</th><th className="num">Hit</th><th className="num">xP</th><th>p10 / p90</th><th className="num">vs hold</th><th>P(better)</th><th>Captain</th></tr></thead>
        <tbody>
          {plan.timeline.map((s, i) => (
            <tr key={s.gameweek}>
              <td>{s.gameweek}</td>
              <td>{s.action}</td>
              <td className="small">{s.transfers_in.length ? `${s.transfers_out.map(n).join(", ")} → ${s.transfers_in.map(n).join(", ")}` : "—"}</td>
              <td>{s.hit_points ? `−${s.hit_points}` : "0"}</td>
              <td>{s.expected_points.toFixed(1)} <span className="small">(hold {hold.timeline[i]?.expected_points.toFixed(1)})</span></td>
              <td className="small">{s.p10.toFixed(0)} / {s.p90.toFixed(0)}</td>
              <td>{signed(s.expected_delta_vs_hold)}</td>
              <td><Prob p={s.probability_delta_positive} /></td>
              <td>{n(s.captain)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      {plan.follow_up.length ? (
        <p className="small">Later moves in this plan: {plan.follow_up.map((f) => `GW${f.gameweek} ${f.out.map(n).join(",")} → ${f.in.map(n).join(",")}${f.hit_points ? ` (−${f.hit_points})` : ""}`).join("; ")}</p>
      ) : null}
      <p className="small">Later gameweeks are re-optimised for every plan; only the first gameweek's moves are a commitment.</p>
    </>
  );
}
