export type Status = {
  id: number; heartbeat_at: string; mode: string; desk_pid: number | null; desk_alive: boolean | null;
  cycle: number | null; last_cycle_at: string | null; cash: number | null; equity: number | null;
  start_equity: number | null; open_positions: number | null; cfg: Record<string, unknown> | null;
};
export type CycleStats = {
  discovered?: number; priced?: number; scout_kills?: number; in_window?: number; filter_pass?: number;
  analyst_pass?: number; entries?: number; cash?: number; equity?: number; secs?: number;
};
export type Cycle = { cycle: number; ts: string; stats: CycleStats; kill_reasons: Record<string, number>; kills_by_stage: Record<string, number> };
export type Kill = { id: number; ts: string; cycle: number | null; stage: string; mint: string | null; symbol: string | null; pair: string | null; reasons: string[]; metrics: Record<string, unknown> | null };
export type Score = { id: number; ts: string; cycle: number | null; mint: string | null; symbol: string | null; score: number; passed: boolean; components: Record<string, unknown> | null };
export type Position = {
  id: number; mint: string | null; symbol: string | null; pair: string | null; status: string; opened_at: string | null;
  entry_price: number | null; size_usd: number | null; qty: number | null; initial_stop: number | null; stop_price: number | null;
  peak_price: number | null; trail_active: boolean | null; last_price: number | null; last_at: string | null; score: number | null;
  closed_at: string | null; exit_price: number | null; exit_reason: string | null; pnl_usd: number | null; pnl_pct: number | null;
};
export type Trade = { id: number; ts: string; cycle: number | null; kind: string; mint: string | null; symbol: string | null; data: Record<string, unknown> | null };
export type Equity = { ts: string; cash: number; equity: number; open_positions: number };
export type SmartFactor = { ts: string; symbol: string | null; score: number | null; factor: number | null; count: number | null; status: string | null };
export type ShadowA = { passes: number; would_block: number; undecidable: number; entries: number; entries_blocked: number; closed: number; actual_pnl_usd: number; shadow_pnl_usd: number };
export type ShadowB = { watched: number; triggered: number; compared: number; actual_pnl_usd: number; shadow_pnl_usd: number;
  rows: { symbol: string | null; actual: number | null; shadow: number | null; triggered: boolean | null }[] };
export type BoardRow = { kind: string; rule: string; n: number; blocked: number | null; live: number; shadow: number; delta: number;
  ci_lo?: number | null; ci_hi?: number | null };
export type BoardStats = { trades: number; win_rate: number; avg_win: number | null; avg_loss: number | null; profit_factor: number | null;
  expectancy: number | null; max_drawdown: number; entry_slip_pct: number | null; exit_slip_pct: number | null; mfe_pct: number | null; mae_pct: number | null };
export type Board = { positions: number; live_pnl: number; rows: BoardRow[]; stats?: BoardStats | null };
export type Smart = {
  id: number; updated_at: string; list_size: number; tracked: number; tokens_done: number; tokens_skipped: number; blacklisted: number;
  payload: { top: { a: string; score: number; wins: number; losses: number; tokens: string[] }[];
    winners: { symbol: string | null; status: string; buyers: number | null; wins: number | null }[];
    factors: SmartFactor[]; rule_a: ShadowA; rule_b: ShadowB; min_list_for_factor: number;
    rule_skeptic?: { passes: number; would_block: number; entries: number; entries_blocked: number; closed: number; actual_pnl_usd: number; shadow_pnl_usd: number; last: { symbol: string | null; fails: string[] } | null };
    rule_ptp?: { compared: number; tp_hit: number; actual_pnl_usd: number; shadow_pnl_usd: number; rows: ShadowB["rows"] };
    rule_trail4?: ShadowB;
    rule_combo?: { watched?: number; triggered?: number; compared: number; tp_hit?: number; trail_hit?: number; actual_pnl_usd: number; shadow_pnl_usd: number; rows: ShadowB["rows"] };
    fills?: { symbol: string | null; side: string | null; modeled: number | null; quoted: number | null; used: string | null }[];
    helius_calls_today?: number;
    board?: { ruleset: string | null; current: Board | null; all: Board | null; error?: string } } | null;
};
