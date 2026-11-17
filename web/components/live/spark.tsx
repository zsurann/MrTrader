"use client";
import { useId, useMemo } from "react";

/** Smooth (Catmull-Rom -> cubic bezier) sparkline path in a 100 x h box. */
export function smooth(vals: number[], h: number, pad = 3, lo?: number, hi?: number) {
  const n = vals.length;
  if (n < 2) return null;
  let min = Math.min(...vals, lo ?? Infinity), max = Math.max(...vals, hi ?? -Infinity);
  if (max - min < 1e-9) { min -= 1; max += 1; }
  const p = vals.map((v, i) => [(i / (n - 1)) * 100, pad + (1 - (v - min) / (max - min)) * (h - 2 * pad)] as const);
  let d = `M${p[0][0].toFixed(2)},${p[0][1].toFixed(2)}`;
  for (let i = 0; i < n - 1; i++) {
    const p0 = p[i - 1] ?? p[i], p1 = p[i], p2 = p[i + 1], p3 = p[i + 2] ?? p2;
    const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C${c1x.toFixed(2)},${c1y.toFixed(2)} ${c2x.toFixed(2)},${c2y.toFixed(2)} ${p2[0].toFixed(2)},${p2[1].toFixed(2)}`;
  }
  const yOf = (v: number) => pad + (1 - (v - min) / (max - min)) * (h - 2 * pad);
  return { line: d, area: `${d} L100,${h} L0,${h} Z`, last: p[n - 1], yOf };
}

/** Responsive sparkline: SVG stretched to the box (stroke stays crisp), gradient fill to transparent, optional pulsing
 *  dot at the leading edge (HTML, so it never gets squashed) and optional dashed baseline (e.g. starting equity). */
export function Spark({ values, color, height = 56, dot = false, baseline, strokeWidth = 2, fillOpacity = 0.22, className }: {
  values: number[]; color: string; height?: number | string; dot?: boolean; baseline?: number; strokeWidth?: number; fillOpacity?: number; className?: string;
}) {
  const gid = useId().replace(/:/g, "");
  const H = 40;
  const g = useMemo(() => smooth(values, H, 3, baseline, baseline), [values, baseline]);
  return (
    <div className={className} style={{ height, position: "relative" }} aria-hidden>
      {g ? (
        <>
          <svg viewBox={`0 0 100 ${H}`} preserveAspectRatio="none" className="absolute inset-0 h-full w-full overflow-visible">
            <defs>
              <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor={color} stopOpacity={fillOpacity} />
                <stop offset="1" stopColor={color} stopOpacity={0} />
              </linearGradient>
            </defs>
            {baseline != null ? <line x1="0" x2="100" y1={g.yOf(baseline)} y2={g.yOf(baseline)} stroke="#9CA3AF" strokeDasharray="3 3" strokeWidth={1} vectorEffect="non-scaling-stroke" /> : null}
            <path d={g.area} fill={`url(#${gid})`} />
            <path d={g.line} fill="none" stroke={color} strokeWidth={strokeWidth} vectorEffect="non-scaling-stroke" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          {dot ? (
            <span className="pulse-dot absolute size-2.5 rounded-full ring-2 ring-white"
              style={{ left: `${g.last[0]}%`, top: `${(g.last[1] / H) * 100}%`, background: color, color, transform: "translate(-50%,-50%)" }} />
          ) : null}
        </>
      ) : null}
    </div>
  );
}
