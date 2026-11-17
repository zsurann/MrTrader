"use client";
import { memo, type ReactNode } from "react";
import { m } from "motion/react";
import { Brain, Ghost, Layers, LogOut, Scale, ShieldCheck, Split, TrendingDown } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { Smart } from "@/lib/types";
import { Panel } from "./panels";
import { int, signedUsd, tfmt, tone } from "./format";

function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-lg border bg-muted/30 px-3 py-2">
      <span className="eyebrow text-[0.6875rem] text-muted-foreground">{label}</span>
      <span className="num text-lg font-semibold leading-tight">{value}</span>
      {hint ? <span className="truncate text-[0.6875rem] text-muted-foreground">{hint}</span> : null}
    </div>
  );
}

function VsRow({ actual, shadow }: { actual: number; shadow: number }) {
  const diff = shadow - actual;
  return (
    <div className="num grid grid-cols-3 gap-2 text-[0.8125rem]">
      <div><div className="text-[0.6875rem] text-muted-foreground">PnL reale</div><div className={cn("font-semibold", tone(actual))}>{signedUsd(actual)}</div></div>
      <div><div className="text-[0.6875rem] text-muted-foreground">PnL ombra</div><div className={cn("font-semibold", tone(shadow))}>{signedUsd(shadow)}</div></div>
      <div><div className="text-[0.6875rem] text-muted-foreground">differenza</div><div className={cn("font-semibold", tone(diff))}>{signedUsd(diff)}</div></div>
    </div>
  );
}

function Rule({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <m.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25 }} className="flex min-w-0 flex-col gap-2.5 rounded-lg border p-3">
      <div className="flex items-center gap-2 text-sm font-semibold">{icon}{title}
        <Badge variant="outline" className="ml-auto border-violet/30 bg-violet/10 text-violet">solo log</Badge>
      </div>
      {children}
    </m.div>
  );
}

export const SmartPanel = memo(function SmartPanel({ smart, className }: { smart: Smart | null; className?: string }) {
  const p = smart?.payload;
  const minList = p?.min_list_for_factor ?? 10;
  const active = (smart?.list_size ?? 0) >= minList;
  return (
    <Panel className={className} title="Smart wallet, fill reali & regole ombra"
      desc="Lista smart wallet da dati on-chain gratuiti · fill paper con quote reali Jupiter · le regole ombra registrano soltanto, non toccano i trade"
      action={<Badge variant="outline" className={cn(active ? "border-profit/30 bg-profit/10 text-profit" : "border-warn/30 bg-warn/10 text-warn")}>
        {active ? "fattore attivo · peso 15%" : `fattore in attesa · servono ≥ ${minList} wallet`}</Badge>}>
      {!smart || !p ? (
        <div className="flex min-h-20 items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">in attesa del primo riepilogo…</div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <div className="flex min-w-0 flex-col gap-3 lg:col-span-4">
            <div className="grid grid-cols-2 gap-2">
              <Stat label="Smart wallet" value={int(smart.list_size)} hint={`${int(smart.tracked)} wallet osservati`} />
              <Stat label="Token vincenti" value={int(smart.tokens_done)} hint={`${int(smart.tokens_skipped)} saltati (troppe tx)`} />
              <Stat label="Esclusi" value={int(smart.blacklisted)} hint="dev, bundle, sniper al lancio" />
              <Stat label="Aggiornato" value={tfmt.format(new Date(smart.updated_at))} hint="ora di Roma" />
            </div>
            <div className="min-w-0">
              <div className="eyebrow mb-1.5 text-[0.6875rem] text-muted-foreground">Migliori wallet</div>
              {p.top.length === 0 ? <p className="text-xs text-muted-foreground">nessun wallet ha ancora ≥ 2 ingressi vincenti</p> : (
                <ul className="flex flex-col gap-1">
                  {p.top.slice(0, 5).map((w) => (
                    <li key={w.a} className="num flex items-center gap-2 text-[0.8125rem]">
                      <span className="font-semibold">{w.a}</span>
                      <span className="truncate text-xs text-muted-foreground">{w.tokens.join(" · ")}</span>
                      <span className="ml-auto text-profit">{w.wins}W</span>{w.losses ? <span className="text-loss">{w.losses}L</span> : null}
                    </li>))}
                </ul>)}
            </div>
          </div>
          <div className="min-w-0 lg:col-span-4">
            <div className="eyebrow mb-1.5 flex items-center gap-1.5 text-[0.6875rem] text-muted-foreground"><Brain className="size-3.5" aria-hidden />Fattore smart sulle ultime analisi</div>
            <div tabIndex={0} role="region" aria-label="Fattore smart recente" className="max-h-[15rem] overflow-auto outline-none focus-visible:ring-2 focus-visible:ring-ring">
              {p.factors.length === 0 ? <p className="text-xs text-muted-foreground">nessuna analisi ancora con il nuovo fattore</p> : (
                <Table className="num text-[0.8125rem]">
                  <TableHeader className="sticky top-0 z-10 bg-card"><TableRow>
                    <TableHead>Ora</TableHead><TableHead>Coin</TableHead><TableHead className="text-right">Smart</TableHead><TableHead className="text-right">Fattore</TableHead><TableHead className="text-right">Score</TableHead>
                  </TableRow></TableHeader>
                  <TableBody>
                    {p.factors.map((f, i) => (
                      <TableRow key={f.ts + i} title={f.status ?? undefined}>
                        <TableCell className="text-muted-foreground">{tfmt.format(new Date(f.ts))}</TableCell>
                        <TableCell className="font-sans font-semibold">{f.symbol}</TableCell>
                        <TableCell className="text-right">{f.count ?? "—"}</TableCell>
                        <TableCell className="text-right">{f.factor == null ? <span className="text-muted-foreground">n/d</span> : Math.round(f.factor)}</TableCell>
                        <TableCell className="text-right">{f.score?.toFixed(1) ?? "—"}</TableCell>
                      </TableRow>))}
                  </TableBody>
                </Table>)}
            </div>
          </div>
          <div className="flex min-w-0 flex-col gap-2 lg:col-span-4">
            <div className="eyebrow flex items-center gap-1.5 text-[0.6875rem] text-muted-foreground"><Scale className="size-3.5" aria-hidden />Fill paper: slippage quotata Jupiter vs modello</div>
            {!p.fills?.length ? <p className="text-xs text-muted-foreground">nessun ingresso/uscita ancora con quote Jupiter</p> : (
              <ul className="num flex flex-col gap-1 text-[0.8125rem]">
                {p.fills.slice(0, 6).map((f, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <Badge variant="outline" className={cn("w-14 justify-center", f.side === "buy" ? "text-info" : "text-warn")}>{f.side === "buy" ? "buy" : "sell"}</Badge>
                    <span className="font-sans font-semibold">{f.symbol}</span>
                    <span className="ml-auto text-muted-foreground">modello {f.modeled?.toFixed(2) ?? "—"}%</span>
                    <span className={cn(f.used === "jupiter" ? "text-foreground" : "text-muted-foreground")}>quote {f.quoted == null ? "n/d" : f.quoted.toFixed(2) + "%"}</span>
                  </li>))}
              </ul>)}
            <p className="text-[0.6875rem] text-muted-foreground">Helius oggi: {int(p.helius_calls_today ?? 0)} chiamate (budget 2.400/giorno)</p>
          </div>
          <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2 lg:col-span-12 xl:grid-cols-3">
            <Rule icon={<ShieldCheck className="size-4 text-info" aria-hidden />} title="Compra solo con ≥ 2 smart wallet">
              <p className="num text-xs text-muted-foreground">
                {int(p.rule_a.passes)} passaggi Analyst · {int(p.rule_a.would_block)} bloccati · {int(p.rule_a.undecidable)} senza dati ·
                {" "}{int(p.rule_a.entries_blocked)}/{int(p.rule_a.entries)} ingressi bloccati
              </p>
              <VsRow actual={p.rule_a.actual_pnl_usd} shadow={p.rule_a.shadow_pnl_usd} />
            </Rule>
            <Rule icon={<LogOut className="size-4 text-warn" aria-hidden />} title="Esci quando escono gli smart">
              <p className="num text-xs text-muted-foreground">{int(p.rule_b.watched)} posizioni osservate · {int(p.rule_b.triggered)} uscite ombra · {int(p.rule_b.compared)} confronti chiusi</p>
              <VsRow actual={p.rule_b.actual_pnl_usd} shadow={p.rule_b.shadow_pnl_usd} />
              {p.rule_b.rows.length ? (
                <ul className="num flex flex-col gap-0.5 text-xs">
                  {p.rule_b.rows.map((r, i) => (
                    <li key={i} className="flex items-center gap-2">
                      <Ghost className={cn("size-3", r.triggered ? "text-warn" : "text-muted-foreground")} aria-hidden />
                      <span className="font-sans font-semibold">{r.symbol}</span>
                      <span className={cn("ml-auto", tone(r.actual))}>{signedUsd(r.actual)}</span>
                      <span className="text-muted-foreground">→</span>
                      <span className={tone(r.shadow)}>{signedUsd(r.shadow)}</span>
                    </li>))}
                </ul>) : null}
            </Rule>
            <Rule icon={<Scale className="size-4 text-violet" aria-hidden />} title="Skeptic: checklist pre-ingresso">
              <p className="num text-xs text-muted-foreground">
                {int(p.rule_skeptic?.passes ?? 0)} controlli · {int(p.rule_skeptic?.would_block ?? 0)} bocciati · {int(p.rule_skeptic?.entries_blocked ?? 0)}/{int(p.rule_skeptic?.entries ?? 0)} ingressi bocciati
              </p>
              <VsRow actual={p.rule_skeptic?.actual_pnl_usd ?? 0} shadow={p.rule_skeptic?.shadow_pnl_usd ?? 0} />
              {p.rule_skeptic?.last ? <p className="text-xs text-muted-foreground"><span className="font-semibold text-foreground">{p.rule_skeptic.last.symbol}</span>: {p.rule_skeptic.last.fails.join(" · ")}</p> : null}
            </Rule>
            <Rule icon={<Split className="size-4 text-profit" aria-hidden />} title="Take-profit parziale 40/60">
              <p className="num text-xs text-muted-foreground">40% venduto a +15%, 60% esce come il trade reale · {int(p.rule_ptp?.compared ?? 0)} confronti · {int(p.rule_ptp?.tp_hit ?? 0)} target toccati</p>
              <VsRow actual={p.rule_ptp?.actual_pnl_usd ?? 0} shadow={p.rule_ptp?.shadow_pnl_usd ?? 0} />
              {p.rule_ptp?.rows.length ? (
                <ul className="num flex flex-col gap-0.5 text-xs">
                  {p.rule_ptp.rows.map((r, i) => (
                    <li key={i} className="flex items-center gap-2">
                      <span className="font-sans font-semibold">{r.symbol}</span>
                      <span className={cn("ml-auto", tone(r.actual))}>{signedUsd(r.actual)}</span>
                      <span className="text-muted-foreground">→</span>
                      <span className={tone(r.shadow)}>{signedUsd(r.shadow)}</span>
                    </li>))}
                </ul>) : null}
            </Rule>
            <Rule icon={<TrendingDown className="size-4 text-warn" aria-hidden />} title="Trailing stop al 4%">
              <p className="num text-xs text-muted-foreground">Si attiva a +12% come la regola reale, poi lo stop sta al 4% sotto il picco (la reale usa il 6%) · {int(p.rule_trail4?.watched ?? 0)} osservate · {int(p.rule_trail4?.triggered ?? 0)} uscite ombra · {int(p.rule_trail4?.compared ?? 0)} confronti</p>
              <VsRow actual={p.rule_trail4?.actual_pnl_usd ?? 0} shadow={p.rule_trail4?.shadow_pnl_usd ?? 0} />
              {p.rule_trail4?.rows.length ? (
                <ul className="num flex flex-col gap-0.5 text-xs">
                  {p.rule_trail4.rows.map((r, i) => (
                    <li key={i} className="flex items-center gap-2">
                      <Ghost className={cn("size-3", r.triggered ? "text-warn" : "text-muted-foreground")} aria-hidden />
                      <span className="font-sans font-semibold">{r.symbol}</span>
                      <span className={cn("ml-auto", tone(r.actual))}>{signedUsd(r.actual)}</span>
                      <span className="text-muted-foreground">→</span>
                      <span className={tone(r.shadow)}>{signedUsd(r.shadow)}</span>
                    </li>))}
                </ul>) : null}
            </Rule>
            <Rule icon={<Layers className="size-4 text-info" aria-hidden />} title="Combo: 40% a +15% + trailing 4%">
              <p className="num text-xs text-muted-foreground">40% venduto a +15% se toccato, sul resto trailing al 4% · {int(p.rule_combo?.compared ?? 0)} confronti · {int(p.rule_combo?.tp_hit ?? 0)} take-profit · {int(p.rule_combo?.trail_hit ?? 0)} trailing</p>
              <VsRow actual={p.rule_combo?.actual_pnl_usd ?? 0} shadow={p.rule_combo?.shadow_pnl_usd ?? 0} />
              {p.rule_combo?.rows.length ? (
                <ul className="num flex flex-col gap-0.5 text-xs">
                  {p.rule_combo.rows.map((r, i) => (
                    <li key={i} className="flex items-center gap-2">
                      <Ghost className={cn("size-3", r.triggered ? "text-info" : "text-muted-foreground")} aria-hidden />
                      <span className="font-sans font-semibold">{r.symbol}</span>
                      <span className={cn("ml-auto", tone(r.actual))}>{signedUsd(r.actual)}</span>
                      <span className="text-muted-foreground">→</span>
                      <span className={tone(r.shadow)}>{signedUsd(r.shadow)}</span>
                    </li>))}
                </ul>) : null}
            </Rule>
          </div>
        </div>)}
    </Panel>
  );
});
