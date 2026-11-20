"use client";
import { useEffect, useMemo, useState } from "react";
import { RealtimeClient, type RealtimeChannel } from "@supabase/realtime-js";
import { PostgrestClient } from "@supabase/postgrest-js";
import { SUPABASE_ANON_KEY, SUPABASE_URL } from "./config";
import type { Cycle, Equity, Kill, Position, Score, Smart, Status, Trade } from "./types";

// Public anon key only (SELECT via RLS). We only need REST reads + Realtime, so we use those two small clients
// instead of the full supabase-js bundle (which also ships Auth, Storage and Functions): ~-35 KB gzip.
const rest = new PostgrestClient(`${SUPABASE_URL}/rest/v1`, { headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${SUPABASE_ANON_KEY}` } });
const rt = new RealtimeClient(`${SUPABASE_URL.replace(/^http/, "ws")}/realtime/v1`, { params: { apikey: SUPABASE_ANON_KEY } });
rt.setAuth(SUPABASE_ANON_KEY);
const sb = { from: (t: string) => rest.from(t), channel: (n: string) => rt.channel(n), removeChannel: (c: RealtimeChannel) => rt.removeChannel(c) };

export type LiveState = "connecting" | "live" | "offline";
export type DeskState = {
  status: Status | null; smart: Smart | null; cycles: Cycle[]; kills: Kill[]; scores: Score[]; positions: Position[];
  trades: Trade[]; equity: Equity[]; live: LiveState; lastEventAt: number | null; loaded: boolean;
};
const LIMITS = { cycles: 480, kills: 300, scores: 200, positions: 1000, trades: 200, equity: 2000 };

type Row = Record<string, unknown>;
// Upsert by key, newest first, capped. Fast path: a brand-new row that is newer than the head is just prepended
// (the normal Realtime case), so we only pay for a sort when an older/updated row arrives.
function merge<T>(list: T[], row: T, key: keyof T, cap: number, sortKey: keyof T): T[] {
  const i = list.findIndex((r) => r[key] === row[key]);
  if (i >= 0) { const next = list.slice(); next[i] = row; return next; }
  if (list.length === 0 || row[sortKey] >= list[0][sortKey]) return [row, ...list].slice(0, cap);
  const next = [row, ...list];
  next.sort((a, b) => (a[sortKey] < b[sortKey] ? 1 : a[sortKey] > b[sortKey] ? -1 : 0));
  return next.slice(0, cap);
}

type Change = { table: string; row: Row };
function apply(st: DeskState, batch: Change[]): DeskState {
  const n = { ...st, lastEventAt: Date.now() };
  for (const { table, row } of batch) {
    switch (table) {
      case "desk_status": n.status = row as Status; break;
      case "smart_summary": n.smart = row as Smart; break;
      case "cycles": n.cycles = merge(n.cycles, row as Cycle, "cycle", LIMITS.cycles, "cycle"); break;
      case "kills": n.kills = merge(n.kills, row as Kill, "id", LIMITS.kills, "id"); break;
      case "scores": n.scores = merge(n.scores, row as Score, "id", LIMITS.scores, "id"); break;
      case "positions": n.positions = merge(n.positions, row as Position, "id", LIMITS.positions, "id"); break;
      case "trades": n.trades = merge(n.trades, row as Trade, "id", LIMITS.trades, "id"); break;
      case "equity_snapshots": n.equity = merge(n.equity, row as Equity, "ts", LIMITS.equity, "ts"); break;
    }
  }
  return n;
}

export function useDesk(): DeskState {
  const [s, set] = useState<DeskState>({
    status: null, smart: null, cycles: [], kills: [], scores: [], positions: [], trades: [], equity: [],
    live: "connecting", lastEventAt: null, loaded: false,
  });

  useEffect(() => {
    let cancelled = false;
    let liveNow: LiveState | null = null;
    // The phone suspends the realtime socket in the background and it does not come back: reload when the tab returns offline.
    const onVis = () => { if (document.visibilityState === "visible" && liveNow === "offline") window.location.reload(); };
    document.addEventListener("visibilitychange", onVis);
    // Realtime events arrive in bursts at the end of each scan; coalesce them into one render per animation frame.
    let queue: Change[] = [];
    let raf = 0;
    const flush = () => { raf = 0; const b = queue; queue = []; if (!cancelled && b.length) set((st) => apply(st, b)); };

    // Subscribe FIRST, then load the snapshot, so nothing landing in between is lost (merge is idempotent).
    const channel: RealtimeChannel = sb
      .channel("memedesk")
      .on("postgres_changes", { event: "*", schema: "public" }, (p) => {
        const row = p.new as Row;
        if (!row || Object.keys(row).length === 0) return;
        queue.push({ table: p.table, row });
        if (!raf) raf = requestAnimationFrame(flush);
      })
      .subscribe((state) => {
        if (cancelled) return;
        const live: LiveState | null = state === "SUBSCRIBED" ? "live"
          : state === "CLOSED" || state === "CHANNEL_ERROR" || state === "TIMED_OUT" ? "offline" : null;
        if (live) { liveNow = live; set((st) => (st.live === live ? st : { ...st, live })); }
      });

    (async () => {
      const [st, cy, ki, sc, po, tr, eq, sm] = await Promise.all([
        sb.from("desk_status").select("*").eq("id", 1).maybeSingle(),
        sb.from("cycles").select("*").order("cycle", { ascending: false }).limit(LIMITS.cycles),
        sb.from("kills").select("*").order("id", { ascending: false }).limit(LIMITS.kills),
        sb.from("scores").select("*").order("id", { ascending: false }).limit(LIMITS.scores),
        sb.from("positions").select("*").order("id", { ascending: false }).limit(LIMITS.positions),
        sb.from("trades").select("*").order("id", { ascending: false }).limit(LIMITS.trades),
        sb.from("equity_snapshots").select("*").order("ts", { ascending: false }).limit(LIMITS.equity),
        sb.from("smart_summary").select("*").eq("id", 1).maybeSingle(),
      ]);
      if (cancelled) return;
      set((cur) => {
        // snapshot first, then re-apply anything that arrived live while it was loading
        const keep = <T,>(snap: T[] | null, live: T[], key: keyof T, cap: number, sk: keyof T) =>
          live.reduceRight((acc, r) => merge(acc, r, key, cap, sk), (snap ?? []) as T[]);
        return {
          ...cur, loaded: true,
          status: cur.status ?? (st.data as Status | null),
          smart: cur.smart ?? (sm.data as Smart | null),
          cycles: keep(cy.data as Cycle[] | null, cur.cycles, "cycle", LIMITS.cycles, "cycle"),
          kills: keep(ki.data as Kill[] | null, cur.kills, "id", LIMITS.kills, "id"),
          scores: keep(sc.data as Score[] | null, cur.scores, "id", LIMITS.scores, "id"),
          positions: keep(po.data as Position[] | null, cur.positions, "id", LIMITS.positions, "id"),
          trades: keep(tr.data as Trade[] | null, cur.trades, "id", LIMITS.trades, "id"),
          equity: keep(eq.data as Equity[] | null, cur.equity, "ts", LIMITS.equity, "ts"),
        };
      });
    })();

    return () => { cancelled = true; document.removeEventListener("visibilitychange", onVis); if (raf) cancelAnimationFrame(raf); sb.removeChannel(channel); };
  }, []);

  // v11: the paper ledger restarts at each epoch (desk ledger 'epoch_ts', sent in desk_status.cfg); older trades stay in the database
  const ep = Number((s.status?.cfg as { epoch_ts?: number } | null)?.epoch_ts ?? 0) * 1000;
  return useMemo(() => {
    if (!ep) return s;
    const after = (t: string | null | undefined) => !t || Date.parse(t) >= ep;
    return { ...s, positions: s.positions.filter((p) => after(p.opened_at)), trades: s.trades.filter((t) => after(t.ts)),
      equity: s.equity.filter((e) => after(e.ts)) };
  }, [s, ep]);
}
