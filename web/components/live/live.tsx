"use client";
import { memo, useEffect, useMemo, useState, type ReactNode } from "react";
import { AnimatePresence, m } from "motion/react";
import { Brain, Radar, ScrollText, ShieldCheck, ShieldHalf, Wallet, Activity as ActivityIcon, TrendingDown, TrendingUp, Zap } from "lucide-react";
import { cn } from "@/lib/utils";
import type { LiveState } from "@/lib/useDesk";
import type { Cycle, Equity, Kill, Position, Score, Status, Trade } from "@/lib/types";
import { int, tfmt, useNow } from "@/components/desk/format";
import { Odometer } from "./odometer";
import { Spark } from "./spark";

export const CYCLE_S = 180;
const GREEN = "#10B981", RED = "#EF4444";
// en-US style money for the big numbers (matches the reference); Italian everywhere else in text
const money = (n: number, d = 2) => (n < 0 ? "−$" : "$") + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const sMoney = (n: number, d = 2) => (n >= 0 ? "+" : "") + money(n, d);
const price = (n: number) => "$" + (n >= 1 ? n.toLocaleString("en-US", { maximumFractionDigits: 2 }) : n.toLocaleString("en-US", { maximumSignificantDigits: 4 }));
const sPct = (n: number, d = 1) => (n >= 0 ? "+" : "−") + Math.abs(n).toFixed(d) + "%";

// ------------------------------------------------------------------ pills
export function Pill({ tone = "idle", solid, children, className }: { tone?: "up" | "down" | "idle" | "info" | "warn"; solid?: boolean; children: ReactNode; className?: string }) {
  const t = {
    up: solid ? "bg-[#10B981] text-white" : "bg-[#10B981]/12 text-[#059669]",
    down: solid ? "bg-[#EF4444] text-white" : "bg-[#EF4444]/12 text-[#DC2626]",
    idle: "bg-[#F3F4F6] text-[#6B7280]",
    info: "bg-[#3B82F6]/12 text-[#2563EB]",
    warn: "bg-[#F59E0B]/15 text-[#B45309]",
  }[tone];
  return <span className={cn("num inline-flex h-6 shrink-0 items-center gap-1 rounded-full px-2.5 text-xs font-semibold", t, className)}>{children}</span>;
}
const toneOf = (n: number | null | undefined) => (n == null || n === 0 ? "idle" : n > 0 ? "up" : "down");

// ------------------------------------------------------------------ header
export const Header = memo(function Header({ status, live }: { status: Status | null; live: LiveState }) {
  const now = useNow(5000);
  const hb = status ? (now - Date.parse(status.heartbeat_at)) / 1000 : Infinity;
  const ok = live === "live" && !!status?.desk_alive && hb < 75;
  const label = ok ? "Live" : live === "connecting" ? "Connessione…" : !status?.desk_alive ? "Desk fermo" : "Offline";
  return (
    <header className="flex items-center gap-3">
      <div aria-hidden className="grid size-11 shrink-0 place-items-center rounded-[14px] bg-gradient-to-br from-[#111827] to-[#374151] text-white shadow-[0_4px_12px_rgb(0_0_0/0.12)]">
        <ActivityIcon className="size-5" />
      </div>
      <div className="min-w-0">
        <h1 className="text-[1.375rem] font-bold leading-tight tracking-tight text-[#111827]">MemeDesk</h1>
        <p className="truncate text-[0.8125rem] text-[#6B7280]">Solana memes • 6 agenti</p>
      </div>
      <div className="ml-auto flex items-center gap-2">
        <Pill tone="warn">PAPER</Pill>
        <span role="status" aria-live="polite" className="inline-flex h-7 items-center gap-1.5 rounded-full bg-white px-3 text-xs font-semibold text-[#111827] shadow-[0_4px_12px_rgb(0_0_0/0.05)]">
          <span className={cn("size-2 rounded-full", ok ? "pulse-dot bg-[#10B981] text-[#10B981]" : "bg-[#9CA3AF]")} />{label}
        </span>
      </div>
    </header>
  );
});

// ------------------------------------------------------------------ ticker (real DexScreener prices for the desk's own coins)
type Tick = { mint: string; symbol: string; price: number; h1: number | null };
export function useMarket(mints: string[]) {
  const key = mints.join(",");
  const [ticks, set] = useState<Tick[]>([]);
  useEffect(() => {
    if (!key) return;
    let dead = false;
    const load = async () => {
      try {
        const r = await fetch(`https://api.dexscreener.com/tokens/v1/solana/${key}`);
        if (!r.ok) return;
        const pairs = (await r.json()) as { baseToken: { address: string; symbol: string }; priceUsd?: string; priceChange?: { h1?: number }; liquidity?: { usd?: number } }[];
        const best = new Map<string, (typeof pairs)[number]>();
        for (const p of pairs) {
          const b = best.get(p.baseToken.address);
          if (!b || (p.liquidity?.usd ?? 0) > (b.liquidity?.usd ?? 0)) best.set(p.baseToken.address, p);
        }
        const out = key.split(",").map((mm) => best.get(mm)).filter(Boolean).map((p) => ({
          mint: p!.baseToken.address, symbol: p!.baseToken.symbol, price: Number(p!.priceUsd ?? 0), h1: p!.priceChange?.h1 ?? null }));
        if (!dead) set(out);
      } catch { /* keep last ticks */ }
    };
    load();
    const t = setInterval(load, 60_000);
    return () => { dead = true; clearInterval(t); };
  }, [key]);
  return ticks;
}

export const Ticker = memo(function Ticker({ ticks }: { ticks: Tick[] }) {
  const items = ticks.length ? ticks : null;
  return (
    <div className="marquee-wrap fade-x -mx-4 h-8 overflow-hidden sm:mx-0" aria-label="Ticker dei token seguiti dal desk (prezzi DexScreener, variazione 1h)">
      {items ? (
        <div className="marquee" style={{ ["--marquee-s" as string]: `${Math.max(20, items.length * 5)}s` }}>
          {[0, 1].map((dup) => (
            <ul key={dup} className="flex shrink-0 items-center gap-5 pr-5" aria-hidden={dup === 1}>
              {items.map((t) => (
                <li key={t.mint} className="num flex items-center gap-2 whitespace-nowrap text-[0.8125rem]">
                  <span className="font-bold text-[#111827]">{t.symbol}</span>
                  <span className="text-[#6B7280]">{price(t.price)}</span>
                  <Pill tone={toneOf(t.h1)}>{t.h1 == null ? "—" : sPct(t.h1)}</Pill>
                </li>))}
            </ul>))}
        </div>
      ) : <p className="flex h-8 items-center px-4 text-xs text-[#6B7280]">in attesa dei prezzi dei token seguiti…</p>}
    </div>
  );
});

// ------------------------------------------------------------------ hero equity
export const Hero = memo(function Hero({ status, equity, positions, loading }: { status: Status | null; equity: Equity[]; positions: Position[]; loading: boolean }) {
  const start = status?.start_equity ?? 10000;
  const eq = status?.equity ?? equity[0]?.equity ?? null;
  const series = useMemo(() => equity.slice(0, 400).reverse().map((e) => e.equity), [equity]);
  const closed = useMemo(() => positions.filter((p) => p.status === "CLOSED"), [positions]);
  const wins = closed.filter((p) => (p.pnl_usd ?? 0) > 0).length;
  const chg = eq == null ? null : eq - start;
  const mult = eq == null ? null : eq / start;
  const up = (chg ?? 0) >= 0;
  const txt = eq == null ? "$0.00" : money(eq);
  const cut = txt.lastIndexOf(".");
  return (
    <section aria-label="Equity" className="card-soft flex h-full min-h-[19rem] flex-col overflow-hidden p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <p className="text-[0.8125rem] font-medium text-[#6B7280]">Equity • partito da {money(start, 0)}</p>
        {mult != null ? <Pill tone={up ? "up" : "down"} solid className="h-7 px-3 text-[0.8125rem]">{mult.toFixed(3)}x</Pill> : null}
      </div>
      <div className="mt-1 flex items-baseline font-bold tracking-tight text-[#111827]">
        {loading && eq == null ? <span className="h-12 w-60 animate-pulse rounded-xl bg-[#F3F4F6]" /> : (
          <>
            <Odometer text={txt.slice(0, cut)} label={txt} className="num text-[2.75rem] leading-none sm:text-5xl" />
            <Odometer text={txt.slice(cut)} label="" className="num ml-0.5 text-2xl leading-none text-[#9CA3AF]" />
          </>)}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <span className={cn("num font-semibold", chg == null ? "text-[#6B7280]" : up ? "text-[#10B981]" : "text-[#EF4444]")}>
          {chg == null ? "—" : `${sMoney(chg)} (${sPct((100 * chg) / start, 2)})`}
        </span>
        <span className="num ml-auto text-[0.8125rem] text-[#6B7280]">{closed.length} trade • {closed.length ? Math.round((100 * wins) / closed.length) : 0}% win</span>
      </div>
      <div className="mt-auto flex min-h-28 flex-1 flex-col justify-end pt-3">
        {series.length >= 2 ? <Spark values={series} color={up ? GREEN : RED} height="100%" className="min-h-28 flex-1" dot baseline={start} strokeWidth={2.5} />
          : <div className="h-28 rounded-xl bg-[#F9FAFB]" />}
      </div>
    </section>
  );
});

// ------------------------------------------------------------------ agents
type AgentDef = { key: string; name: string; desc: string; color: string; icon: ReactNode };
export const AGENTS: Record<string, AgentDef> = {
  Scout: { key: "Scout", name: "Scout", desc: "pair 30m–72h · $300k–$20M", color: "#F97316", icon: <Radar className="size-4" /> },
  Filter: { key: "Filter", name: "Filter", desc: "mint, LP, holder, bundle", color: "#3B82F6", icon: <ShieldCheck className="size-4" /> },
  Analyst: { key: "Analyst", name: "Analyst", desc: "score 0–100 · passa ≥ 70", color: "#14B8A6", icon: <Brain className="size-4" /> },
  Executor: { key: "Executor", name: "Executor", desc: "max 5% per trade", color: "#8B5CF6", icon: <Wallet className="size-4" /> },
  Risk: { key: "Risk", name: "Risk", desc: "stop −10% · trailing +12%", color: "#EC4899", icon: <ShieldHalf className="size-4" /> },
  Logger: { key: "Logger", name: "Logger", desc: "ogni kill registrato", color: "#64748B", icon: <ScrollText className="size-4" /> },
};

function Ring({ value, color }: { value: number; color: string }) {
  const c = 2 * Math.PI * 9;
  return (
    <svg viewBox="0 0 24 24" className="size-6 -rotate-90" aria-hidden>
      <circle cx="12" cy="12" r="9" fill="none" stroke="#E5E7EB" strokeWidth="3" />
      <circle cx="12" cy="12" r="9" fill="none" stroke={color} strokeWidth="3" strokeLinecap="round" strokeDasharray={c}
        strokeDashoffset={c * (1 - Math.max(0, Math.min(1, value)))} style={{ transition: "stroke-dashoffset 1s linear" }} />
    </svg>
  );
}

function AgentCard({ a, status, left, right, spark, ring, pop }: {
  a: AgentDef; status: ReactNode; left: ReactNode; right: ReactNode; spark: number[]; ring?: number; pop?: { id: number; pnl: number } | null;
}) {
  return (
    <article className="card-soft relative flex h-[11.5rem] min-w-0 flex-col overflow-hidden p-3 sm:p-3.5" aria-label={`Agente ${a.name}`}>
      <div className="flex items-start gap-2.5">
        <span className="grid size-8 shrink-0 place-items-center rounded-[10px] text-white" style={{ background: a.color }} aria-hidden>{a.icon}</span>
        <div className="min-w-0">
          <h3 className="text-[0.9375rem] font-bold leading-tight text-[#111827]">{a.name}</h3>
          <p className="truncate text-[0.6875rem] text-[#6B7280]">{a.desc}</p>
        </div>
        {ring != null ? <span className="ml-auto"><Ring value={ring} color={a.color} /></span> : null}
      </div>
      <div className="mt-2.5 flex min-h-6 min-w-0 items-center gap-1.5 text-[0.8125rem]">{status}</div>
      <Spark values={spark} color={a.color} height={34} className="mt-auto opacity-90" fillOpacity={0.16} strokeWidth={1.5} />
      <div className="num mt-1.5 flex items-center justify-between gap-2 text-[0.6875rem] text-[#6B7280]">
        <span className="truncate">{left}</span><span className="shrink-0 font-semibold">{right}</span>
      </div>
      <AnimatePresence>
        {pop ? (
          <m.span key={pop.id} className={cn("num pointer-events-none absolute right-3 top-10 rounded-full px-2.5 py-1 text-sm font-bold text-white shadow-lg", pop.pnl >= 0 ? "bg-[#10B981]" : "bg-[#EF4444]")}
            initial={{ opacity: 0, y: 12, scale: 0.8 }} animate={{ opacity: 1, y: -18, scale: 1 }} exit={{ opacity: 0, y: -48 }}
            transition={{ type: "spring", stiffness: 300, damping: 18 }}>
            {sMoney(pop.pnl, 2)}
          </m.span>) : null}
      </AnimatePresence>
    </article>
  );
}

export const Agents = memo(function Agents({ cycles, scores, positions, kills, equity, pop }: {
  cycles: Cycle[]; scores: Score[]; positions: Position[]; kills: Kill[]; equity: Equity[]; pop: { id: number; pnl: number } | null;
}) {
  const now = useNow(1000);
  const c = cycles[0]; const s = c?.stats ?? {};
  const last40 = useMemo(() => cycles.slice(0, 40).reverse(), [cycles]);
  const hour = useMemo(() => cycles.filter((x) => now - Date.parse(x.ts) < 3600e3), [cycles, Math.floor(now / 60000)]); // eslint-disable-line react-hooks/exhaustive-deps
  const open = positions.filter((p) => p.status === "OPEN");
  const closed = useMemo(() => positions.filter((p) => p.status === "CLOSED").sort((a, b) => Date.parse(a.closed_at ?? "") - Date.parse(b.closed_at ?? "")), [positions]);
  const wins = closed.filter((p) => (p.pnl_usd ?? 0) > 0).length;
  const realized = closed.reduce((x, p) => x + (p.pnl_usd ?? 0), 0);
  const cum = useMemo(() => { let r = 0; return [0, ...closed.map((p) => (r += p.pnl_usd ?? 0))]; }, [closed]);
  const elapsed = c ? (now - Date.parse(c.ts)) / 1000 : 0;
  const recent = scores.slice(0, 30);
  const passed = recent.filter((x) => x.passed).length;
  const lastScore = scores[0];
  const fHour = hour.reduce((x, y) => x + (y.kills_by_stage?.Filter ?? 0), 0);
  const kHour = hour.reduce((x, y) => x + Object.values(y.kills_by_stage ?? {}).reduce((a, b) => a + b, 0), 0);
  const lastKills = Object.values(c?.kills_by_stage ?? {}).reduce((a, b) => a + b, 0);
  const exposure = open.reduce((x, p) => x + (p.qty ?? 0) * (p.last_price ?? p.entry_price ?? 0), 0);
  const eqNow = equity[0]?.equity ?? 10000;
  const trailing = open.filter((p) => p.trail_active).length;
  const p0 = open[0];
  const p0pct = p0 && p0.entry_price && p0.last_price ? (100 * (p0.last_price - p0.entry_price)) / p0.entry_price : null;
  return (
    <section aria-label="Agenti" className="grid grid-cols-2 gap-3 lg:grid-cols-3">
      <AgentCard a={AGENTS.Scout} ring={c ? Math.min(1, elapsed / CYCLE_S) : 0}
        status={<span className="truncate text-[#374151]">Scansiona <b className="num">{int(s.discovered)}</b> token · <b className="num">{int(s.in_window)}</b> in finestra</span>}
        spark={last40.map((x) => x.stats.in_window ?? 0)} left={`ciclo #${c?.cycle ?? "—"} · ${s.secs ?? "—"}s`} right={`${int(s.scout_kills)} scartati`} />
      <AgentCard a={AGENTS.Filter}
        status={c ? <><Pill tone="up">pass {s.filter_pass ?? 0}</Pill><Pill tone="down">kill {c.kills_by_stage?.Filter ?? 0}</Pill><span className="truncate text-xs text-[#6B7280]">ultimo ciclo</span></> : <Pill>idle</Pill>}
        spark={last40.map((x) => x.stats.filter_pass ?? 0)} left={`ultima ora: ${int(fHour)} kill`} right={`${int(hour.reduce((x, y) => x + (y.stats.filter_pass ?? 0), 0))} pass`} />
      <AgentCard a={AGENTS.Analyst}
        status={lastScore ? <><b className="truncate text-[#111827]">${lastScore.symbol}</b><Pill tone={lastScore.passed ? "up" : "down"}>{lastScore.score.toFixed(1)}</Pill>
          <span className="truncate text-xs text-[#6B7280]">{tfmt.format(new Date(lastScore.ts))}</span></> : <Pill>idle</Pill>}
        spark={recent.slice().reverse().map((x) => x.score)} left={`${passed}/${recent.length} ≥ 70`} right={recent.length ? `media ${(recent.reduce((x, y) => x + y.score, 0) / recent.length).toFixed(1)}` : "—"} />
      <AgentCard a={AGENTS.Executor} ring={eqNow ? exposure / eqNow : 0} pop={pop}
        status={p0 ? <><b className="truncate text-[#111827]">${p0.symbol}</b><span className="num text-xs text-[#6B7280]">{money(p0.size_usd ?? 0, 0)}</span>
          <Pill tone={toneOf(p0pct)}>{p0pct == null ? "—" : sPct(p0pct)}</Pill>{open.length > 1 ? <span className="text-xs text-[#6B7280]">+{open.length - 1}</span> : null}</>
          : <><Pill>idle</Pill><span className="truncate text-xs text-[#6B7280]">attende un segnale</span></>}
        spark={equity.slice(0, 200).reverse().map((e) => e.equity)} left={`${positions.length} ingressi totali`} right={`esposizione ${money(exposure, 0)}`} />
      <AgentCard a={AGENTS.Risk} pop={pop}
        status={open.length ? <><Pill tone="info">{open.length} stop attivi</Pill>{trailing ? <Pill tone="up">{trailing} trailing</Pill> : null}</>
          : <><Pill>idle</Pill><span className="truncate text-xs text-[#6B7280]">nessuna posizione</span></>}
        spark={cum} left={`${wins}W · ${closed.length - wins}L`} right={<span className={realized >= 0 ? "text-[#10B981]" : "text-[#EF4444]"}>{sMoney(realized)}</span>} />
      <AgentCard a={AGENTS.Logger}
        status={<span className="truncate text-[#374151]"><b className="num">{int(lastKills)}</b> kill nell&apos;ultimo ciclo</span>}
        spark={last40.map((x) => Object.values(x.kills_by_stage ?? {}).reduce((a, b) => a + b, 0))} left={`ultima ora: ${int(kHour)} kill`} right={`${int(kills.length ? hour.length : 0)} cicli/h`} />
    </section>
  );
});

// ------------------------------------------------------------------ activity feed
type Item = { id: string; ts: number; agent: keyof typeof AGENTS; text: ReactNode };
export function buildActivity(trades: Trade[], scores: Score[], kills: Kill[], cycles: Cycle[]): Item[] {
  const it: Item[] = [];
  for (const t of trades.slice(0, 40)) {
    const d = (t.data ?? {}) as Record<string, unknown>;
    if (t.kind === "ENTRY") {
      const o = (d.order ?? {}) as Record<string, number>;
      it.push({ id: "t" + t.id, ts: Date.parse(t.ts), agent: "Executor", text: <><b>ha comprato</b> ${t.symbol} · {money(o.size_usd ?? 0, 0)} · slippage {o.slippage_pct ?? "—"}%</> });
    } else if (t.kind === "EXIT") {
      const p = Number(d.pnl_usd ?? 0);
      it.push({ id: "t" + t.id, ts: Date.parse(t.ts), agent: "Risk", text: <><b>ha chiuso</b> ${t.symbol} · {String(d.reason ?? "")} · <span className={p >= 0 ? "text-[#10B981]" : "text-[#EF4444]"}>{sMoney(p)} ({sPct(Number(d.pnl_pct ?? 0))})</span></> });
    } else if (t.kind === "TRAIL_ACTIVATED") {
      it.push({ id: "t" + t.id, ts: Date.parse(t.ts), agent: "Risk", text: <><b>trailing attivo</b> su ${t.symbol}</> });
    }
  }
  for (const s of scores.slice(0, 25))
    it.push({ id: "s" + s.id, ts: Date.parse(s.ts), agent: "Analyst", text: <><b>score {s.score.toFixed(1)}</b> a ${s.symbol} · <span className={s.passed ? "text-[#10B981]" : "text-[#EF4444]"}>{s.passed ? "passa" : "scartato"}</span></> });
  for (const k of kills.slice(0, 40)) {
    if (k.stage === "Analyst") continue;   // already shown as a score
    it.push({ id: "k" + k.id, ts: Date.parse(k.ts), agent: k.stage as keyof typeof AGENTS, text: <><b>ha scartato</b> ${k.symbol} · {k.reasons[0]}{k.reasons.length > 1 ? ` (+${k.reasons.length - 1})` : ""}</> });
  }
  for (const c of cycles.slice(0, 15)) {
    const kl = Object.values(c.kills_by_stage ?? {}).reduce((a, b) => a + b, 0);
    it.push({ id: "c" + c.cycle, ts: Date.parse(c.ts), agent: "Scout", text: <><b>ciclo #{c.cycle}</b> · {int(c.stats.discovered)} token, {int(c.stats.in_window)} in finestra · {c.stats.secs}s</> });
    it.push({ id: "l" + c.cycle, ts: Date.parse(c.ts) - 1, agent: "Logger", text: <><b>{int(kl)} kill</b> registrati nel ciclo #{c.cycle}</> });
  }
  return it.filter((x) => AGENTS[x.agent] && !Number.isNaN(x.ts)).sort((a, b) => b.ts - a.ts).slice(0, 40);
}

export const ActivityFeed = memo(function ActivityFeed({ items, wins, losses, className }: { items: Item[]; wins: number; losses: number; className?: string }) {
  return (
    <section aria-label="Attività" className={cn("card-soft flex min-h-[19rem] flex-col p-4 sm:p-5", className)}>
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-bold text-[#111827]">Attività</h2>
        <span className="num text-[0.8125rem] text-[#6B7280]">{wins} {wins === 1 ? "vittoria" : "vittorie"} • {losses} {losses === 1 ? "perdita" : "perdite"}</span>
      </div>
      <div className="fade-top relative mt-2 h-[17.5rem] overflow-hidden">
        <span aria-hidden className="absolute bottom-0 left-[0.3125rem] top-0 w-px bg-[#E5E7EB]" />
        {items.length === 0 ? <p className="pl-6 pt-8 text-sm text-[#6B7280]">nessuna attività ancora</p> : (
          <ol className="relative flex flex-col gap-2 pt-2">
            <AnimatePresence initial={false}>
              {items.map((x) => {
                const a = AGENTS[x.agent];
                return (
                  <m.li key={x.id} initial={{ opacity: 0, x: -10 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0 }} transition={{ duration: 0.3 }}
                    className="grid grid-cols-[0.75rem_3.75rem_1fr] items-baseline gap-2 text-[0.8125rem] leading-snug">
                    <span aria-hidden className="relative top-[1px] size-[0.6875rem] rounded-full ring-2 ring-white" style={{ background: a.color }} />
                    <span className="num text-[0.6875rem] text-[#9CA3AF]">{tfmt.format(new Date(x.ts))}</span>
                    <span className="min-w-0 text-[#374151]"><span className="font-semibold" style={{ color: a.color }}>{a.name}</span> {x.text}</span>
                  </m.li>);
              })}
            </AnimatePresence>
          </ol>)}
      </div>
    </section>
  );
});

// ------------------------------------------------------------------ dynamic-island toast
export type Toast = { id: number; kind: string; symbol: string | null; title: string; sub: string; amount: number | null };
export function toastFromTrade(t: Trade): Toast | null {
  const d = (t.data ?? {}) as Record<string, unknown>;
  if (t.kind === "ENTRY") {
    const o = (d.order ?? {}) as Record<string, number>;
    return { id: t.id, kind: "ENTRY", symbol: t.symbol, title: `Executor ha comprato $${t.symbol}`, sub: `${money(o.size_usd ?? 0, 0)} · stop −10% · score ${o.score ?? "—"}`, amount: null };
  }
  if (t.kind === "EXIT") {
    const p = Number(d.pnl_usd ?? 0);
    const stop = String(d.reason ?? "").includes("stop");
    return { id: t.id, kind: stop ? "STOP" : "EXIT", symbol: t.symbol, title: `Risk ha venduto $${t.symbol}`, sub: `${sPct(Number(d.pnl_pct ?? 0))} · ${String(d.reason ?? "")}`, amount: p };
  }
  return null;
}
export function Island({ toast }: { toast: Toast | null }) {
  return (
    <div className="pointer-events-none fixed inset-x-0 top-2 z-50 flex justify-center px-3" role="status" aria-live="polite">
      <AnimatePresence>
        {toast ? (
          <m.div key={toast.id} initial={{ y: -90, scale: 0.85, opacity: 0 }} animate={{ y: 0, scale: 1, opacity: 1 }} exit={{ y: -90, scale: 0.9, opacity: 0 }}
            transition={{ type: "spring", stiffness: 420, damping: 24, mass: 0.9 }}
            className="flex w-full max-w-[26rem] items-center gap-3 rounded-[1.75rem] bg-black px-3 py-2.5 text-white shadow-[0_12px_32px_rgb(0_0_0/0.35)]">
            <span className={cn("grid size-10 shrink-0 place-items-center rounded-full", toast.kind === "ENTRY" ? "bg-[#8B5CF6]" : (toast.amount ?? 0) >= 0 ? "bg-[#10B981]" : "bg-[#EF4444]")}>
              {toast.kind === "ENTRY" ? <Zap className="size-5" /> : (toast.amount ?? 0) >= 0 ? <TrendingUp className="size-5" /> : <TrendingDown className="size-5" />}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[0.9375rem] font-semibold">{toast.title}</span>
              <span className="num block truncate text-xs text-white/60">{toast.sub}</span>
            </span>
            {toast.amount != null ? <span className={cn("num shrink-0 text-xl font-bold", toast.amount >= 0 ? "text-[#34D399]" : "text-[#F87171]")}>{sMoney(toast.amount, 0)}</span> : null}
          </m.div>) : null}
      </AnimatePresence>
    </div>
  );
}

// ------------------------------------------------------------------ goal bar
const TICKS = [1, 1.25, 1.5, 2, 3, 4, 5];
export const GoalBar = memo(function GoalBar({ equity, start }: { equity: number | null; start: number }) {
  const mult = equity == null ? null : equity / start;
  const posOf = (x: number) => {   // ticks evenly spaced; linear inside each segment
    if (x <= TICKS[0]) return 0;
    for (let i = 0; i < TICKS.length - 1; i++)
      if (x <= TICKS[i + 1]) return ((i + (x - TICKS[i]) / (TICKS[i + 1] - TICKS[i])) / (TICKS.length - 1)) * 100;
    return 100;
  };
  const pos = mult == null ? 0 : posOf(mult);
  const next = TICKS.find((t) => (mult ?? 0) < t) ?? null;
  const missing = next != null && equity != null ? next * start - equity : null;
  return (
    <section aria-label="Obiettivo" className="card-soft p-4 sm:p-5">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-bold text-[#111827]">Obiettivo: 2x</h2>
        <span className="num text-[0.8125rem] text-[#6B7280]">{equity == null ? "in attesa dei dati" : next ? `prossimo ${next}x • ${money(missing ?? 0, 0)} mancanti` : "obiettivi superati"}</span>
      </div>
      <div className="relative mx-3 mb-6 mt-9 h-2.5 rounded-full bg-[#E5E7EB]" role="meter" aria-valuemin={1} aria-valuemax={5} aria-valuenow={mult ?? 1} aria-label="Moltiplicatore equity">
        <m.div className="absolute inset-y-0 left-0 w-full rounded-full bg-[#3B82F6]" style={{ originX: 0 }} initial={false} animate={{ scaleX: pos / 100 }} transition={{ type: "spring", stiffness: 90, damping: 20 }} />
        {TICKS.map((t, i) => {
          const at = (i / (TICKS.length - 1)) * 100; const done = (mult ?? 0) >= t;
          return (
            <span key={t} className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2" style={{ left: `${at}%` }}>
              <span className={cn("block size-4 rounded-full border-2", done ? "border-white bg-[#10B981] ring-2 ring-[#10B981]" : t === 2 ? "border-[#3B82F6] bg-white" : "border-[#D1D5DB] bg-white")} />
              <span className={cn("num absolute left-1/2 top-5 -translate-x-1/2 text-[0.6875rem] font-semibold", done ? "text-[#10B981]" : t === 2 ? "text-[#3B82F6]" : "text-[#9CA3AF]")}>{t}x</span>
            </span>);
        })}
        {mult != null ? (
          <m.span className="num absolute -top-8 -translate-x-1/2 rounded-full bg-[#111827] px-2 py-0.5 text-[0.6875rem] font-bold text-white"
            initial={false} animate={{ left: `${pos}%` }} transition={{ type: "spring", stiffness: 90, damping: 20 }}>{mult.toFixed(3)}x</m.span>) : null}
      </div>
    </section>
  );
});
