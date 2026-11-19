"use client";
import { memo, type ReactNode } from "react";
import { m } from "motion/react";
import { Brain, Scale, Trophy } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { Board, Smart } from "@/lib/types";
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

// v10: rules promoted from shadow to live (desk.py CFG); the board below lists only rules still in shadow
const LIVE = ["smart exit", "trailing 4%", "breakeven +8%→+1%", "età ≤ 6 h", "pressione d'acquisto", "score vecchio ≥ 70"];
const KIND: Record<string, string> = { uscita: "uscita", ingresso: "filtro", "ghost-usc": "uscita (ghost)", "ombra-ingr": "ingresso extra" };

function BoardTable({ title, b }: { title: string; b: Board | null }) {
  return (
    <m.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25 }} className="flex min-w-0 flex-col gap-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">
        <Trophy className="size-4 text-violet" aria-hidden />{title}
        <Badge variant="outline" className="ml-auto border-violet/30 bg-violet/10 text-violet">solo log</Badge>
      </div>
      {!b ? <p className="text-xs text-muted-foreground">in attesa dei dati…</p> : (<>
        <p className="num text-xs text-muted-foreground">{int(b.positions)} trade chiusi · PnL live <span className={tone(b.live_pnl)}>{signedUsd(b.live_pnl)}</span> · differenza &gt; 0 = la regola ombra avrebbe fatto meglio</p>
        {!b.rows.length ? <p className="text-xs text-muted-foreground">nessun confronto ancora</p> : (
          <div className="max-h-96 overflow-auto">
            <Table>
              <TableHeader><TableRow>
                <TableHead>Regola</TableHead><TableHead className="hidden sm:table-cell">Tipo</TableHead>
                <TableHead className="text-right">Trade</TableHead><TableHead className="hidden text-right sm:table-cell">Live</TableHead>
                <TableHead className="hidden text-right sm:table-cell">Ombra</TableHead><TableHead className="text-right">Differenza</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {b.rows.map((r) => (
                  <TableRow key={r.kind + r.rule}>
                    <TableCell className="font-medium">{r.rule}{r.blocked != null ? <span className="text-muted-foreground"> · blocca {int(r.blocked)}</span> : null}</TableCell>
                    <TableCell className="hidden text-muted-foreground sm:table-cell">{KIND[r.kind] ?? r.kind}</TableCell>
                    <TableCell className="num text-right">{int(r.n)}</TableCell>
                    <TableCell className={cn("num hidden text-right sm:table-cell", tone(r.live))}>{signedUsd(r.live)}</TableCell>
                    <TableCell className={cn("num hidden text-right sm:table-cell", tone(r.shadow))}>{signedUsd(r.shadow)}</TableCell>
                    <TableCell className={cn("num text-right font-semibold", tone(r.delta))}>{signedUsd(r.delta)}</TableCell>
                  </TableRow>))}
              </TableBody>
            </Table>
          </div>)}
      </>)}
    </m.div>
  );
}

export const SmartPanel = memo(function SmartPanel({ smart, className }: { smart: Smart | null; className?: string }) {
  const p = smart?.payload;
  const minList = p?.min_list_for_factor ?? 10;
  const active = (smart?.list_size ?? 0) >= minList;
  return (
    <Panel className={className} title="Smart wallet, fill reali & regole ombra"
      desc="Lista smart wallet da dati on-chain gratuiti · fill paper con quote reali Jupiter · le regole ombra registrano soltanto, non toccano i trade; quelle migliori diventano live"
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
            <p className="text-[0.6875rem] text-muted-foreground">Helius oggi: {int(p.helius_calls_today ?? 0)} chiamate</p>
          </div>
          <div className="flex min-w-0 flex-col gap-3 lg:col-span-12">
            <div className="flex flex-wrap items-center gap-1.5 text-xs">
              <span className="text-muted-foreground">Regole live {p.board?.ruleset ?? ""}:</span>
              {LIVE.map((r) => <Badge key={r} variant="outline" className="border-profit/30 bg-profit/10 text-profit">{r}</Badge>)}
            </div>
            {p.board?.error ? <p className="text-xs text-loss">classifica non disponibile: {p.board.error}</p> : null}
            <BoardTable title={`Regole ombra da ${p.board?.ruleset ?? "—"}`} b={p.board?.current ?? null} />
            <BoardTable title="Regole ombra · storico completo" b={p.board?.all ?? null} />
          </div>
        </div>)}
    </Panel>
  );
});
