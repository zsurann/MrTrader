import { useEffect, useState } from "react";

export const TZ = "Europe/Rome";
export const tfmt = new Intl.DateTimeFormat("it-IT", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit" });
export const hmfmt = new Intl.DateTimeFormat("it-IT", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
export const dtfmt = new Intl.DateTimeFormat("it-IT", { timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const nf = new Map<number, Intl.NumberFormat>();
const num = (d: number) => {
  let f = nf.get(d);
  if (!f) { f = new Intl.NumberFormat("it-IT", { minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: "always" }); nf.set(d, f); }
  return f;
};
export const usd = (n: number | null | undefined, d = 0) => (n == null ? "—" : (n < 0 ? "−$" : "$") + num(d).format(Math.abs(n)));
export const signedUsd = (n: number | null | undefined, d = 2) => (n == null ? "—" : (n >= 0 ? "+" : "") + usd(n, d));
export const px = (n: number | null | undefined) => (n == null ? "—" : n < 0.01 ? n.toExponential(3) : n.toPrecision(5));
export const pct = (n: number | null | undefined, d = 1) => (n == null ? "—" : (n >= 0 ? "+" : "−") + Math.abs(n).toFixed(d) + "%");
export const int = (n: number | null | undefined) => (n == null ? "—" : num(0).format(n));
export const ago = (iso: string | null | undefined, now: number) => {
  if (!iso) return "—";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  return s < 60 ? `${s}s fa` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s fa` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m fa`;
};
export const dexUrl = (pair: string | null) => (pair ? `https://dexscreener.com/solana/${pair}` : undefined);
export const tone = (n: number | null | undefined) => (n == null || n === 0 ? "text-muted-foreground" : n > 0 ? "text-profit" : "text-loss");

/** Shared 1 Hz clock: one interval for the whole page instead of one per component. */
const subs = new Set<(n: number) => void>();
let timer: ReturnType<typeof setInterval> | null = null;
export function useNow(stepMs = 1000) {
  const [n, setN] = useState(() => Date.now());
  useEffect(() => {
    let last = 0;
    const fn = (t: number) => { if (t - last >= stepMs) { last = t; setN(t); } };
    subs.add(fn);
    if (!timer) timer = setInterval(() => { const t = Date.now(); subs.forEach((f) => f(t)); }, 1000);
    return () => { subs.delete(fn); if (!subs.size && timer) { clearInterval(timer); timer = null; } };
  }, [stepMs]);
  return n;
}
