"use client";
import { memo, useMemo, useState, type ReactNode } from "react";
import { AnimatePresence, m } from "motion/react";
import { Activity, ArrowDownRight, ArrowUpRight, CircleDot, ExternalLink, Radio, Timer } from "lucide-react";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";
import type { LiveState } from "@/lib/useDesk";
import type { Cycle, Kill, Position, Score, Status, Trade } from "@/lib/types";
import { Flash } from "./Flash";
import { ago, dexUrl, dtfmt, int, pct, px, signedUsd, tfmt, tone, usd, useNow } from "./format";

const CYCLE_S = 180;
const spring = { type: "spring", stiffness: 260, damping: 32, mass: 0.8 } as const;

// ------------------------------------------------------------------ shared bits
export function Panel({ title, desc, action, footer, className, children, contentClassName }: {
  title: ReactNode; desc?: ReactNode; action?: ReactNode; footer?: ReactNode; className?: string; contentClassName?: string; children: ReactNode;
}) {
  return (
    <Card size="sm" className={cn("min-w-0", className)}>
      <CardHeader className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-col gap-1">
          <CardTitle className="eyebrow">{title}</CardTitle>
          {desc ? <CardDescription className="text-xs">{desc}</CardDescription> : null}
        </div>
        {action ? <CardAction className="col-start-auto row-span-1 row-start-auto">{action}</CardAction> : null}
      </CardHeader>
      <CardContent className={cn("min-w-0 flex-1", contentClassName)}>{children}</CardContent>
      {footer ? <CardFooter className="text-xs text-muted-foreground">{footer}</CardFooter> : null}
    </Card>
  );
}

function Empty({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("flex h-full min-h-20 items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground", className)}>{children}</div>;
}

const STAGE_TONE: Record<string, string> = {
  Filter: "border-warn/30 bg-warn/10 text-warn",
  Analyst: "border-violet/30 bg-violet/10 text-violet",
  Executor: "border-info/30 bg-info/10 text-info",
  Scout: "border-border bg-muted text-muted-foreground",
};
function StageBadge({ stage }: { stage: string }) {
  return <Badge variant="outline" className={cn("w-[4.75rem] justify-center font-semibold", STAGE_TONE[stage])}>{stage}</Badge>;
}

function Pill({ ok, warn, children, label }: { ok?: boolean; warn?: boolean; children: ReactNode; label?: string }) {
  return (
    <Badge variant="outline" aria-label={label}
      className={cn("h-6 gap-1.5 px-2.5", ok ? "border-profit/30 text-profit" : warn ? "border-warn/30 text-warn" : ok === false ? "border-loss/40 text-loss" : "text-muted-foreground")}>
      {children}
    </Badge>
  );
}

// ------------------------------------------------------------------ top bar
export const TopBar = memo(function TopBar({ status, live, lastCycle }: { status: Status | null; live: LiveState; lastCycle: Cycle | undefined }) {
  const now = useNow();
  const hbAge = status ? (now - Date.parse(status.heartbeat_at)) / 1000 : Infinity;
  const deskOk = !!status?.desk_alive && hbAge < 75;
  const elapsed = lastCycle?.ts ? (now - Date.parse(lastCycle.ts)) / 1000 : null;
  const nextIn = elapsed != null ? Math.round(CYCLE_S - elapsed) : null;
  return (
    <header className="z-20 border-b bg-background/85 sm:sticky sm:top-0 backdrop-blur supports-[backdrop-filter]:bg-background/70">
      <div className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 sm:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <div aria-hidden className="grid size-8 place-items-center rounded-lg bg-primary/15 text-primary ring-1 ring-primary/30"><Activity className="size-4" /></div>
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-base font-semibold tracking-tight">MemeDesk <span className="text-muted-foreground">· Solana</span></h1>
            <p className="hidden truncate text-xs text-muted-foreground sm:block">Scout → Filter → Analyst → Executor → Risk → Logger · orari Europe/Rome</p>
          </div>
          <Badge variant="outline" className="border-warn/40 bg-warn/10 text-warn">PAPER · niente soldi veri</Badge>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2" role="status" aria-live="polite">
          <Pill ok={live === "live"} warn={live === "connecting"} label="stato realtime">
            <Radio className="size-3" aria-hidden />{live === "live" ? "realtime" : live === "offline" ? "realtime offline" : "connessione…"}
          </Pill>
          <Pill ok={status ? deskOk : undefined} label="stato desk">
            <CircleDot className="size-3" aria-hidden />{status ? (deskOk ? `desk attivo · pid ${status.desk_pid}` : "desk fermo / sync assente") : "in attesa dati"}
          </Pill>
        </div>
      </div>
      <div className="mx-auto flex max-w-[1440px] items-center gap-3 px-4 pb-2.5 text-xs text-muted-foreground sm:px-6">
        <Timer className="size-3.5 shrink-0" aria-hidden />
        <span className="num shrink-0">ciclo #{status?.cycle ?? "—"}</span>
        <Progress aria-label="avanzamento verso il prossimo ciclo" value={elapsed == null ? 0 : Math.min(100, (100 * elapsed) / CYCLE_S)} className="h-1 max-w-64 flex-1" />
        <span className="num shrink-0">{nextIn == null ? "—" : nextIn > 0 ? `prossimo ~${nextIn}s` : "scansione in corso"}</span>
        <span className="hidden shrink-0 sm:inline">· heartbeat {status ? ago(status.heartbeat_at, now) : "—"}</span>
      </div>
    </header>
  );
});

// ------------------------------------------------------------------ KPI strip
function Kpi({ label, value, sub, flashKey, dir, loading }: { label: string; value: ReactNode; sub?: ReactNode; flashKey?: unknown; dir?: number | null; loading?: boolean }) {
  return (
    <Card size="sm" className="min-w-0 gap-1.5">
      <CardHeader><CardTitle className="eyebrow">{label}</CardTitle></CardHeader>
      <CardContent className="flex min-h-14 flex-col gap-1">
        {loading ? <Skeleton className="h-7 w-28" /> : (
          <div className="num text-2xl font-semibold tracking-tight sm:text-[1.65rem]">
            {flashKey !== undefined ? <Flash value={flashKey} dir={dir}>{value}</Flash> : value}
          </div>)}
        <div className="truncate text-xs text-muted-foreground">{sub}</div>
      </CardContent>
    </Card>
  );
}

export const Kpis = memo(function Kpis({ status, lastCycle, positions, loading }: { status: Status | null; lastCycle: Cycle | undefined; positions: Position[]; loading: boolean }) {
  const start = status?.start_equity ?? 10000;
  const pnl = status?.equity != null ? status.equity - start : null;
  const closed = useMemo(() => positions.filter((p) => p.status === "CLOSED"), [positions]);
  const wins = closed.filter((p) => (p.pnl_usd ?? 0) > 0).length;
  const realized = closed.reduce((a, p) => a + (p.pnl_usd ?? 0), 0);
  return (
    <section aria-label="Indicatori principali" className="grid grid-cols-2 gap-3 lg:grid-cols-5">
      <div className="col-span-2 lg:col-span-1">
        <Kpi label="Equity (paper)" loading={loading} flashKey={status?.equity} dir={pnl}
          value={usd(status?.equity, 2)}
          sub={pnl == null ? "da $10.000" : <span className={tone(pnl)}>{signedUsd(pnl)} · {pct((100 * pnl) / start, 2)} da {usd(start)}</span>} />
      </div>
      <Kpi label="Cassa" loading={loading} value={usd(status?.cash, 2)} sub="max 5% per trade (≤0,25% liq)" />
      <Kpi label="Posizioni aperte" loading={loading} flashKey={status?.open_positions} value={status?.open_positions ?? "—"} sub="stop −10% dal mercato · trailing dopo +12%" />
      <Kpi label="Realizzato" loading={loading} flashKey={realized} dir={realized}
        value={<span className={tone(realized)}>{signedUsd(realized)}</span>} sub={`${closed.length} trade · win ${wins}/${closed.length}`} />
      <Kpi label="Ultimo ciclo" loading={loading} value={lastCycle?.stats.secs != null ? `${lastCycle.stats.secs.toLocaleString("it-IT")}s` : "—"} sub={`scansione ogni ${CYCLE_S}s`} />
    </section>
  );
});

// ------------------------------------------------------------------ funnel
export const Funnel = memo(function Funnel({ c, className }: { c: Cycle | undefined; className?: string }) {
  const s = c?.stats ?? {};
  const rows: [string, number | undefined, string][] = [
    ["Scoperte", s.discovered, "bg-muted-foreground/50"], ["Con prezzo", s.priced, "bg-muted-foreground/50"],
    ["In finestra (Scout)", s.in_window, "bg-info"], ["Passano Filter", s.filter_pass, "bg-warn"],
    ["Score ≥ 70", s.analyst_pass, "bg-violet"], ["Ingressi", s.entries, "bg-profit"],
  ];
  const max = Math.max(1, s.discovered ?? 1);
  return (
    <Panel className={className} title="Funnel ultimo ciclo" action={<span className="num text-xs text-muted-foreground">#{c?.cycle ?? "—"}</span>}
      footer="Barre in scala logaritmica">
      <ol className="flex flex-col gap-2.5">
        {rows.map(([k, v, col]) => {
          const w = v ? Math.max(0.01, Math.log10(1 + v) / Math.log10(1 + max)) : 0;
          return (
            <li key={k} className="grid grid-cols-[8.5rem_1fr_3rem] items-center gap-3 text-sm">
              <span className="truncate text-muted-foreground">{k}</span>
              <div className="h-2.5 overflow-hidden rounded-full bg-muted">
                <m.div className={cn("h-full w-full rounded-full", col)} style={{ originX: 0 }} initial={{ scaleX: 0 }} animate={{ scaleX: w }} transition={spring} />
              </div>
              <span className="num text-right font-medium"><Flash value={v}>{v ?? "—"}</Flash></span>
            </li>);
        })}
      </ol>
    </Panel>
  );
});

// ------------------------------------------------------------------ kills by reason
type Win = "last" | "1h" | "all";
export const KillReasons = memo(function KillReasons({ cycles, className }: { cycles: Cycle[]; className?: string }) {
  const [win, setWin] = useState<Win>("1h");
  const now = useNow(15000);
  const { rows, n } = useMemo(() => {
    const pick = win === "last" ? cycles.slice(0, 1) : win === "1h" ? cycles.filter((c) => now - Date.parse(c.ts) < 3600e3) : cycles;
    const agg = new Map<string, number>();
    for (const c of pick) for (const [k, v] of Object.entries(c.kill_reasons ?? {})) agg.set(k, (agg.get(k) ?? 0) + v);
    return { rows: [...agg.entries()].sort((a, b) => b[1] - a[1]), n: pick.length };
  }, [cycles, win, now]);
  const max = rows.length ? rows[0][1] : 1;
  return (
    <Panel className={className} title="Scarti per motivo"
      action={
        <ToggleGroup type="single" size="sm" variant="outline" value={win} onValueChange={(v) => v && setWin(v as Win)} aria-label="Finestra temporale">
          <ToggleGroupItem value="last">ultimo</ToggleGroupItem>
          <ToggleGroupItem value="1h">1h</ToggleGroupItem>
          <ToggleGroupItem value="all">tutto</ToggleGroupItem>
        </ToggleGroup>}
      footer={`${n} cicli · una coin può avere più motivi · ogni scarto è loggato sul box`}>
      <div tabIndex={0} role="region" aria-label="Elenco motivi di scarto" className="relative h-[19rem] overflow-y-auto pr-1 outline-none focus-visible:ring-2 focus-visible:ring-ring">
        {rows.length === 0 ? <Empty>nessun dato</Empty> : (
          <ul className="flex flex-col gap-1.5">
            {rows.map(([k, v]) => (
              <li key={k} className="grid grid-cols-[minmax(0,1fr)_minmax(5rem,1fr)_3.5rem] items-center sm:grid-cols-[minmax(0,13rem)_1fr_3.5rem] gap-3 text-[0.8125rem]">
                <span className="truncate" title={k}>{k}</span>
                <div className="h-2 overflow-hidden rounded-full bg-muted">
                  <m.div className="h-full w-full rounded-full bg-loss/80" style={{ originX: 0 }} initial={{ scaleX: 0 }} animate={{ scaleX: v / max }} transition={spring} />
                </div>
                <span className="num text-right text-muted-foreground">{int(v)}</span>
              </li>))}
          </ul>)}
      </div>
    </Panel>
  );
});

// ------------------------------------------------------------------ live kill feed
const STAGES = ["tutti", "Filter", "Analyst", "Executor"] as const;
export const KillFeed = memo(function KillFeed({ kills, className }: { kills: Kill[]; className?: string }) {
  const [stage, setStage] = useState<string>("tutti");
  const now = useNow(10000);
  const list = useMemo(() => (stage === "tutti" ? kills : kills.filter((k) => k.stage === stage)).slice(0, 150), [kills, stage]);
  return (
    <Panel className={className} title={<span className="flex items-center gap-2"><span className="relative flex size-2"><span className="absolute inline-flex size-full animate-ping rounded-full bg-loss/60 motion-reduce:animate-none" /><span className="relative inline-flex size-2 rounded-full bg-loss" /></span>Feed scarti live</span>}
      action={
        <ToggleGroup type="single" size="sm" variant="outline" value={stage} onValueChange={(v) => v && setStage(v)} aria-label="Filtra per fase">
          {STAGES.map((s) => <ToggleGroupItem key={s} value={s}>{s}</ToggleGroupItem>)}
        </ToggleGroup>}
      footer="Scout scarta ~700 coin/ciclo per età/mcap: sono aggregate in “Scarti per motivo”">
      <div tabIndex={0} role="region" aria-label="Feed scarti" className="relative h-[26rem] overflow-y-auto pr-1 outline-none focus-visible:ring-2 focus-visible:ring-ring">
        {list.length === 0 ? <Empty>nessuno scarto</Empty> : (
          <ul className="flex flex-col">
            <AnimatePresence initial={false} key={stage}>
              {list.map((k) => {
                const fresh = now - Date.parse(k.ts) < 120e3;
                return (
                  <m.li key={k.id} initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.28, ease: "easeOut" }}
                    className={cn("flex flex-col gap-1 border-b border-l-2 border-b-border py-2 pr-1 pl-2.5 last:border-b-0", fresh ? "border-l-loss" : "border-l-transparent")}>
                    <div className="flex min-w-0 items-center gap-2">
                      <StageBadge stage={k.stage} />
                      {k.pair ? (
                        <a href={dexUrl(k.pair)} target="_blank" rel="noreferrer" className="inline-flex min-w-0 items-center gap-1 truncate font-semibold hover:text-primary hover:underline">
                          {k.symbol ?? "?"}<ExternalLink className="size-3 shrink-0 opacity-60" aria-hidden /><span className="sr-only">(apre DexScreener)</span>
                        </a>) : <span className="font-semibold">{k.symbol ?? "?"}</span>}
                      <span className="num ml-auto shrink-0 text-xs text-muted-foreground">{tfmt.format(new Date(k.ts))} · #{k.cycle}</span>
                    </div>
                    <p className="text-[0.8125rem] leading-snug text-foreground/80">{(k.reasons ?? []).join(" · ")}</p>
                  </m.li>);
              })}
            </AnimatePresence>
          </ul>)}
      </div>
    </Panel>
  );
});

// ------------------------------------------------------------------ positions
function posMath(p: Position) {
  const last = p.last_price ?? p.entry_price ?? 0, ep = p.entry_price ?? 0;
  const pnlPct = ep ? (100 * (last - ep)) / ep : null;
  const pnlUsd = p.qty != null ? p.qty * last - (p.size_usd ?? 0) : null;
  const dist = p.stop_price && last ? (100 * (last - p.stop_price)) / last : null;
  const toTrail = ep ? (100 * (ep * 1.12 - (p.peak_price ?? ep))) / ep : null;
  return { last, ep, pnlPct, pnlUsd, dist, toTrail };
}
function TrailCell({ p, toTrail }: { p: Position; toTrail: number | null }) {
  return p.trail_active
    ? <Badge variant="outline" className="border-profit/30 bg-profit/10 text-profit">trailing attivo</Badge>
    : <span className="text-xs text-muted-foreground">attiva a +12% (manca {Math.max(0, toTrail ?? 0).toFixed(1)}%)</span>;
}

export const OpenPositions = memo(function OpenPositions({ positions, className }: { positions: Position[]; className?: string }) {
  const now = useNow(5000);
  const open = useMemo(() => positions.filter((p) => p.status === "OPEN"), [positions]);
  return (
    <Panel className={className} title="Posizioni aperte" action={<Badge variant="secondary" className="num">{open.length}</Badge>} contentClassName="min-h-24">
      {open.length === 0 ? <Empty className="min-h-24">Nessuna posizione aperta · il desk entra con score ≥ 55, score vecchio ≥ 70 e pressione d'acquisto</Empty> : (
        <>
          {/* mobile: cards */}
          <ul className="flex flex-col gap-2 md:hidden">
            {open.map((p) => { const x = posMath(p); return (
              <li key={p.id} className="rounded-lg border bg-muted/30 p-3">
                <div className="flex items-center justify-between gap-2">
                  <a href={dexUrl(p.pair)} target="_blank" rel="noreferrer" className="font-semibold hover:text-primary hover:underline">{p.symbol}</a>
                  <span className={cn("num font-semibold", tone(x.pnlPct))}><Flash value={x.pnlPct} dir={x.pnlPct}>{pct(x.pnlPct, 2)}</Flash></span>
                </div>
                <dl className="num mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                  <dt className="text-muted-foreground">Size</dt><dd className="text-right">{usd(p.size_usd, 2)}</dd>
                  <dt className="text-muted-foreground">PnL $</dt><dd className={cn("text-right", tone(x.pnlUsd))}>{signedUsd(x.pnlUsd)}</dd>
                  <dt className="text-muted-foreground">Ingresso / ultimo</dt><dd className="text-right">{px(x.ep)} / {px(x.last)}</dd>
                  <dt className="text-muted-foreground">Stop (distanza)</dt><dd className="text-right">{px(p.stop_price)} ({pct(x.dist == null ? null : -x.dist, 2)})</dd>
                </dl>
                <div className="mt-2 flex items-center justify-between gap-2"><TrailCell p={p} toTrail={x.toTrail} /><span className="text-xs text-muted-foreground">{ago(p.last_at, now)}</span></div>
              </li>); })}
          </ul>
          {/* desktop: table */}
          <div className="hidden md:block">
            <Table className="num text-[0.8125rem]">
              <TableHeader><TableRow>
                <TableHead>Coin</TableHead><TableHead>Aperta</TableHead><TableHead className="text-right">Size</TableHead>
                <TableHead className="text-right">Ingresso</TableHead><TableHead className="text-right">Ultimo</TableHead><TableHead className="text-right">PnL</TableHead>
                <TableHead className="text-right">Stop</TableHead><TableHead className="text-right">Dist. stop</TableHead><TableHead className="text-right">Picco</TableHead>
                <TableHead>Trailing</TableHead><TableHead>Prezzo agg.</TableHead><TableHead className="text-right">Score</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {open.map((p) => { const x = posMath(p); return (
                  <TableRow key={p.id}>
                    <TableCell className="font-sans font-semibold"><a href={dexUrl(p.pair)} target="_blank" rel="noreferrer" className="hover:text-primary hover:underline">{p.symbol}</a></TableCell>
                    <TableCell className="text-muted-foreground">{p.opened_at ? dtfmt.format(new Date(p.opened_at)) : "—"}</TableCell>
                    <TableCell className="text-right">{usd(p.size_usd, 2)}</TableCell>
                    <TableCell className="text-right">{px(x.ep)}</TableCell>
                    <TableCell className="text-right"><Flash value={x.last} dir={x.pnlPct}>{px(x.last)}</Flash></TableCell>
                    <TableCell className={cn("text-right font-semibold", tone(x.pnlPct))}>
                      <span className="inline-flex items-center gap-1">{(x.pnlPct ?? 0) >= 0 ? <ArrowUpRight className="size-3.5" aria-hidden /> : <ArrowDownRight className="size-3.5" aria-hidden />}{pct(x.pnlPct, 2)}</span>
                      <span className="ml-1 text-xs font-normal">({signedUsd(x.pnlUsd)})</span>
                    </TableCell>
                    <TableCell className="text-right">{px(p.stop_price)}</TableCell>
                    <TableCell className="text-right">{pct(x.dist == null ? null : -x.dist, 2)}</TableCell>
                    <TableCell className="text-right">{px(p.peak_price)}</TableCell>
                    <TableCell className="font-sans"><TrailCell p={p} toTrail={x.toTrail} /></TableCell>
                    <TableCell className="text-muted-foreground">{ago(p.last_at, now)}</TableCell>
                    <TableCell className="text-right">{p.score?.toFixed(1)}</TableCell>
                  </TableRow>); })}
              </TableBody>
            </Table>
          </div>
        </>)}
    </Panel>
  );
});

// ------------------------------------------------------------------ analyst scores
const COMPS: [string, string][] = [["momentum", "Mom"], ["unique_buyers", "Buyers"], ["buy_sell", "B/S"], ["holder_growth", "Holders"], ["liq_mcap", "Liq/MC"], ["smart_wallets", "Smart"]];
function Meter({ v }: { v: number | null }) {
  const col = v == null ? "bg-muted" : v >= 70 ? "bg-profit" : v >= 40 ? "bg-info" : "bg-loss";
  return (
    <span className="inline-flex items-center gap-1.5" title={v == null ? "n/d" : v.toFixed(1)}>
      <span className="h-1.5 w-10 overflow-hidden rounded-full bg-muted" aria-hidden><span className={cn("block h-full", col)} style={{ width: `${v ?? 0}%` }} /></span>
      <span className="w-6 text-right text-xs">{v == null ? "—" : Math.round(v)}</span>
    </span>
  );
}
export const Scores = memo(function Scores({ scores, className }: { scores: Score[]; className?: string }) {
  const rows = scores.slice(0, 100);
  return (
    <Panel className={className} title="Score Analyst" desc="0–100 · passa ≥ 70 · Smart = smart wallet in acquisto (peso 15%; “—” = dato non disponibile, pesi ridistribuiti)">
      <div tabIndex={0} role="region" aria-label="Tabella score" className="relative h-[22rem] overflow-auto outline-none focus-visible:ring-2 focus-visible:ring-ring">
        {rows.length === 0 ? <Empty>ancora nessuno score</Empty> : (
          <Table className="num text-[0.8125rem]">
            <TableHeader className="sticky top-0 z-10 bg-card"><TableRow>
              <TableHead>Ora</TableHead><TableHead>Coin</TableHead><TableHead className="text-right">Score</TableHead>
              {COMPS.map(([, l]) => <TableHead key={l}>{l}</TableHead>)}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((s) => (
                <TableRow key={s.id}>
                  <TableCell className="text-muted-foreground">{tfmt.format(new Date(s.ts))}</TableCell>
                  <TableCell className="font-sans font-semibold">{s.symbol}</TableCell>
                  <TableCell className="text-right">
                    <Badge variant="outline" className={cn("num", s.passed ? "border-profit/30 bg-profit/10 text-profit" : "border-loss/30 bg-loss/10 text-loss")}>{s.score.toFixed(1)}</Badge>
                  </TableCell>
                  {COMPS.map(([k]) => <TableCell key={k}><Meter v={(s.components?.[k] as number | null | undefined) ?? null} /></TableCell>)}
                </TableRow>))}
            </TableBody>
          </Table>)}
      </div>
    </Panel>
  );
});

// ------------------------------------------------------------------ closed trades + trade events (tabs)
const KIND: Record<string, [string, string]> = {
  ENTRY: ["ingresso", "border-info/30 bg-info/10 text-info"],
  EXIT: ["uscita", "border-border bg-muted text-foreground"],
  TRAIL_ACTIVATED: ["trailing on", "border-profit/30 bg-profit/10 text-profit"],
};
export const TradesPanel = memo(function TradesPanel({ positions, trades, className }: { positions: Position[]; trades: Trade[]; className?: string }) {
  const closed = useMemo(() => positions.filter((p) => p.status === "CLOSED"), [positions]);
  const tot = closed.reduce((a, p) => a + (p.pnl_usd ?? 0), 0);
  const wins = closed.filter((p) => (p.pnl_usd ?? 0) > 0).length;
  return (
    <Panel className={className} title="Trade" action={<span className={cn("num text-sm font-semibold", tone(tot))}>{signedUsd(tot)}</span>}>
      <Tabs defaultValue="closed" className="gap-3">
        <TabsList>
          <TabsTrigger value="closed">Chiusi <span className="num ml-1 text-muted-foreground">{closed.length}</span></TabsTrigger>
          <TabsTrigger value="events">Eventi <span className="num ml-1 text-muted-foreground">{trades.length}</span></TabsTrigger>
        </TabsList>
        <TabsContent value="closed">
          <div tabIndex={0} role="region" aria-label="Trade chiusi" className="relative h-[19rem] overflow-auto outline-none focus-visible:ring-2 focus-visible:ring-ring">
            {closed.length === 0 ? <Empty>nessun trade chiuso</Empty> : (
              <Table className="num text-[0.8125rem]">
                <TableHeader className="sticky top-0 z-10 bg-card"><TableRow>
                  <TableHead>Coin</TableHead><TableHead>Uscita</TableHead><TableHead className="text-right">PnL</TableHead><TableHead>Motivo</TableHead>
                </TableRow></TableHeader>
                <TableBody>
                  {closed.map((p) => (
                    <TableRow key={p.id}>
                      <TableCell className="font-sans font-semibold">{p.symbol}</TableCell>
                      <TableCell className="text-muted-foreground">{p.closed_at ? dtfmt.format(new Date(p.closed_at)) : "—"}</TableCell>
                      <TableCell className={cn("text-right", tone(p.pnl_usd))}>{signedUsd(p.pnl_usd)} <span className="text-xs">({pct(p.pnl_pct, 1)})</span></TableCell>
                      <TableCell className="font-sans text-xs text-muted-foreground">{p.exit_reason}</TableCell>
                    </TableRow>))}
                </TableBody>
              </Table>)}
          </div>
          <p className="mt-2 text-xs text-muted-foreground">Win {wins}/{closed.length}</p>
        </TabsContent>
        <TabsContent value="events">
          <div tabIndex={0} role="region" aria-label="Eventi trade" className="relative h-[19rem] overflow-auto outline-none focus-visible:ring-2 focus-visible:ring-ring">
            {trades.length === 0 ? <Empty>nessun evento</Empty> : (
              <ul className="flex flex-col">
                {trades.map((t) => { const [lbl, cls] = KIND[t.kind] ?? [t.kind, ""]; return (
                  <li key={t.id} className="flex items-center gap-2 border-b py-1.5 text-[0.8125rem] last:border-b-0">
                    <Badge variant="outline" className={cn("w-[5.5rem] justify-center", cls)}>{lbl}</Badge>
                    <span className="font-semibold">{t.symbol}</span>
                    <span className="num ml-auto text-xs text-muted-foreground">{dtfmt.format(new Date(t.ts))}</span>
                  </li>); })}
              </ul>)}
          </div>
        </TabsContent>
      </Tabs>
    </Panel>
  );
});


