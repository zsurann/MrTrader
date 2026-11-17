"use client";
// Recharts-based charts (shadcn/ui Chart). Loaded lazily from page.tsx so they never block first paint.
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, ReferenceLine, XAxis, YAxis } from "recharts";
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart";
import type { Cycle, Equity } from "@/lib/types";
import { hmfmt, tfmt, usd } from "./format";

const eqConfig = { equity: { label: "Equity", color: "var(--primary)" } } satisfies ChartConfig;

export function EquityChart({ equity, start }: { equity: Equity[]; start: number }) {
  const data = equity.slice().reverse().map((e) => ({ t: Date.parse(e.ts), equity: e.equity }));
  const last = data.at(-1)?.equity ?? start;
  const color = last >= start ? "var(--profit)" : "var(--loss)";
  return (
    <ChartContainer config={eqConfig} className="aspect-auto h-56 w-full">
      <AreaChart data={data} margin={{ left: 4, right: 8, top: 8, bottom: 0 }} accessibilityLayer>
        <defs>
          <linearGradient id="fillEq" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={color} stopOpacity={0.35} />
            <stop offset="95%" stopColor={color} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid vertical={false} strokeDasharray="3 3" />
        <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickLine={false} axisLine={false}
          tickMargin={8} minTickGap={48} tickFormatter={(v: number) => hmfmt.format(new Date(v))} />
        <YAxis width={64} tickLine={false} axisLine={false} domain={[(min: number) => Math.min(min, start) - 20, (max: number) => Math.max(max, start) + 20]}
          tickFormatter={(v: number) => usd(v, 0)} />
        <ReferenceLine y={start} stroke="var(--muted-foreground)" strokeDasharray="4 4" strokeOpacity={0.6} />
        <ChartTooltip cursor={{ stroke: "var(--border)" }}
          content={<ChartTooltipContent labelFormatter={(_, p) => (p?.[0] ? tfmt.format(new Date(p[0].payload.t as number)) : "")}
            formatter={(v) => <span className="num">{usd(Number(v), 2)}</span>} />} />
        <Area dataKey="equity" type="linear" stroke={color} strokeWidth={2} fill="url(#fillEq)" isAnimationActive={false} dot={false} />
      </AreaChart>
    </ChartContainer>
  );
}

const cyConfig = { secs: { label: "Durata (s)", color: "var(--info)" } } satisfies ChartConfig;

export function CycleChart({ cycles }: { cycles: Cycle[] }) {
  const data = cycles.slice(0, 60).reverse().map((c) => ({
    cycle: `#${c.cycle}`, secs: c.stats.secs ?? 0, entries: c.stats.entries ?? 0, inw: c.stats.in_window ?? 0, pass: c.stats.filter_pass ?? 0,
  }));
  return (
    <ChartContainer config={cyConfig} className="aspect-auto h-32 w-full">
      <BarChart data={data} margin={{ left: 0, right: 0, top: 4, bottom: 0 }} accessibilityLayer>
        <CartesianGrid vertical={false} strokeDasharray="3 3" />
        <XAxis dataKey="cycle" tickLine={false} axisLine={false} minTickGap={24} tickMargin={6} />
        <YAxis width={32} tickLine={false} axisLine={false} />
        <ChartTooltip cursor={{ fill: "var(--muted)" }} content={<ChartTooltipContent
          formatter={(v, _n, item) => (
            <div className="flex flex-col gap-0.5">
              <span className="num">{String(v)}s</span>
              <span className="text-muted-foreground">{item.payload.inw} in finestra · {item.payload.pass} passano Filter · {item.payload.entries} ingressi</span>
            </div>
          )} />} />
        <Bar dataKey="secs" radius={[3, 3, 0, 0]} isAnimationActive={false}>
          {data.map((d) => <Cell key={d.cycle} fill={d.entries > 0 ? "var(--profit)" : "var(--info)"} fillOpacity={0.85} />)}
        </Bar>
      </BarChart>
    </ChartContainer>
  );
}
