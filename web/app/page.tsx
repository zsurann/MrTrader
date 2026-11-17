"use client";
import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState } from "react";
import { LazyMotion, MotionConfig, domAnimation } from "motion/react";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useDesk } from "@/lib/useDesk";
import { Funnel, KillFeed, KillReasons, OpenPositions, Panel, Scores, TradesPanel } from "@/components/desk/panels";
import { SmartPanel } from "@/components/desk/smart";
import { ActivityFeed, Agents, GoalBar, Header, Hero, Island, Ticker, buildActivity, toastFromTrade, useMarket, type Toast } from "@/components/live/live";

// Recharts only lives in the detail tabs: code-split, never on the first-paint path.
const EquityChart = dynamic(() => import("@/components/desk/charts").then((m) => m.EquityChart), { ssr: false, loading: () => <Skeleton className="h-56 w-full" /> });
const CycleChart = dynamic(() => import("@/components/desk/charts").then((m) => m.CycleChart), { ssr: false, loading: () => <Skeleton className="h-32 w-full" /> });

export default function Page() {
  const d = useDesk();
  const start = d.status?.start_equity ?? 10000;
  const eq = d.status?.equity ?? d.equity[0]?.equity ?? null;

  // ticker: the desk's own coins (open positions first, then latest analyses / filter decisions), real DexScreener prices
  const mints = useMemo(() => {
    const out: string[] = [];
    const add = (x: string | null | undefined) => { if (x && !out.includes(x) && out.length < 30) out.push(x); };
    d.positions.filter((p) => p.status === "OPEN").forEach((p) => add(p.mint));
    d.scores.slice(0, 40).forEach((s) => add(s.mint));
    d.kills.filter((k) => k.stage !== "Scout").slice(0, 60).forEach((k) => add(k.mint));
    d.positions.forEach((p) => add(p.mint));
    return out;
  }, [d.positions, d.scores, d.kills]);
  const ticks = useMarket(mints);

  const activity = useMemo(() => buildActivity(d.trades, d.scores, d.kills, d.cycles), [d.trades, d.scores, d.kills, d.cycles]);
  const closed = d.positions.filter((p) => p.status === "CLOSED");
  const wins = closed.filter((p) => (p.pnl_usd ?? 0) > 0).length;

  // island toast + floating PnL: only for trade events that arrive LIVE after the snapshot (never replay history)
  const seen = useRef<Set<number> | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const [pop, setPop] = useState<{ id: number; pnl: number } | null>(null);
  useEffect(() => {
    if (!d.loaded) return;
    if (!seen.current) { seen.current = new Set(d.trades.map((t) => t.id)); return; }
    const fresh = d.trades.filter((t) => !seen.current!.has(t.id)).sort((a, b) => a.id - b.id);
    fresh.forEach((t) => seen.current!.add(t.id));
    const t = fresh.map(toastFromTrade).filter(Boolean).at(-1);
    if (t) {
      setToast(t);
      if (t.amount != null) setPop({ id: t.id, pnl: t.amount });
    }
  }, [d.trades, d.loaded]);
  useEffect(() => { if (!toast) return; const h = setTimeout(() => setToast(null), 3200); return () => clearTimeout(h); }, [toast]);
  useEffect(() => { if (!pop) return; const h = setTimeout(() => setPop(null), 1800); return () => clearTimeout(h); }, [pop]);

  return (
    <LazyMotion features={domAnimation} strict>
      <MotionConfig reducedMotion="user">
        <Island toast={toast} />
        <main className="mx-auto flex max-w-[1200px] flex-col gap-4 px-4 pb-10 pt-4 sm:px-6 sm:pt-6">
          <Header status={d.status} live={d.live} />
          <Ticker ticks={ticks} />
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
            <div className="lg:col-span-7"><Hero status={d.status} equity={d.equity} positions={d.positions} loading={!d.loaded} /></div>
            <div className="order-3 lg:order-none lg:col-span-5 lg:flex"><ActivityFeed className="w-full" items={activity} wins={wins} losses={closed.length - wins} /></div>
            <div className="order-2 lg:order-none lg:col-span-12">
              <Agents cycles={d.cycles} scores={d.scores} positions={d.positions} kills={d.kills} equity={d.equity} pop={pop} />
            </div>
            <div className="order-4 lg:order-none lg:col-span-12"><GoalBar equity={eq} start={start} /></div>
          </div>

          <section aria-label="Dettagli" className="mt-2">
            <Tabs defaultValue="positions" className="gap-3">
              <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
                <TabsList className="h-10 rounded-full bg-white p-1 shadow-[0_4px_12px_rgb(0_0_0/0.05)]">
                  {[["positions", "Posizioni"], ["scores", "Score"], ["kills", "Scarti"], ["smart", "Smart & ombra"], ["charts", "Grafici"]].map(([v, l]) => (
                    <TabsTrigger key={v} value={v} className="rounded-full px-3.5 data-[state=active]:bg-[#111827] data-[state=active]:text-white">{l}</TabsTrigger>))}
                </TabsList>
              </div>
              <TabsContent value="positions" className="flex flex-col gap-4">
                <OpenPositions positions={d.positions} />
                <TradesPanel positions={d.positions} trades={d.trades} />
              </TabsContent>
              <TabsContent value="scores"><Scores scores={d.scores} /></TabsContent>
              <TabsContent value="kills" className="grid grid-cols-1 gap-4 lg:grid-cols-12">
                <KillFeed className="lg:col-span-7" kills={d.kills} />
                <KillReasons className="lg:col-span-5" cycles={d.cycles} />
                <Funnel className="lg:col-span-12" c={d.cycles[0]} />
              </TabsContent>
              <TabsContent value="smart"><SmartPanel smart={d.smart} /></TabsContent>
              <TabsContent value="charts" className="flex flex-col gap-4">
                <Panel title="Curva equity" desc="linea tratteggiata = capitale iniziale">
                  {d.equity.length < 2 ? <Skeleton className="h-56 w-full" /> : <EquityChart equity={d.equity} start={start} />}
                </Panel>
                <Panel title="Durata scansione per ciclo" desc="secondi · verde = ciclo con ingressi">
                  {d.cycles.length === 0 ? <Skeleton className="h-32 w-full" /> : <CycleChart cycles={d.cycles} />}
                </Panel>
              </TabsContent>
            </Tabs>
          </section>
          <footer className="text-center text-xs text-[#6B7280]">
            Dati reali: DexScreener · RugCheck · Solana RPC · GeckoTerminal · Jupiter · live via Supabase Realtime · solo paper trading, nessun wallet.
          </footer>
        </main>
      </MotionConfig>
    </LazyMotion>
  );
}
