-- MemeDesk realtime schema (PAPER desk dashboard).
-- RLS: anon and authenticated may SELECT only. Writes go through the
-- desk-ingest Edge Function using the service role (not stored in this repo).
-- Apply on a new Supabase project, then add all tables to supabase_realtime.

create table if not exists public.desk_status (
  id integer primary key default 1 check (id = 1),
  heartbeat_at timestamptz default now(),
  mode text default 'PAPER',
  desk_pid integer,
  desk_alive boolean,
  cycle integer,
  last_cycle_at timestamptz,
  cash double precision,
  equity double precision,
  start_equity double precision,
  open_positions integer,
  cfg jsonb
);

create table if not exists public.cycles (
  cycle integer primary key,
  ts timestamptz not null,
  stats jsonb not null,
  kill_reasons jsonb not null default '{}'::jsonb,
  kills_by_stage jsonb not null default '{}'::jsonb
);

create table if not exists public.kills (
  id bigint primary key,
  ts timestamptz not null,
  cycle integer,
  stage text not null,
  mint text,
  symbol text,
  pair text,
  reasons jsonb not null,
  metrics jsonb
);

create table if not exists public.scores (
  id bigint primary key,
  ts timestamptz not null,
  cycle integer,
  mint text,
  symbol text,
  score double precision not null,
  passed boolean not null,
  components jsonb
);

create table if not exists public.positions (
  id bigint primary key,
  mint text,
  symbol text,
  pair text,
  status text not null,
  opened_at timestamptz,
  ref_price double precision,
  entry_price double precision,
  size_usd double precision,
  qty double precision,
  entry_slip_pct double precision,
  initial_stop double precision,
  stop_price double precision,
  peak_price double precision,
  trail_active boolean,
  last_price double precision,
  last_at timestamptz,
  score double precision,
  closed_at timestamptz,
  exit_ref double precision,
  exit_price double precision,
  exit_reason text,
  pnl_usd double precision,
  pnl_pct double precision,
  updated_at timestamptz not null default now()
);

create table if not exists public.trades (
  id bigint primary key,
  ts timestamptz not null,
  cycle integer,
  kind text not null,
  mint text,
  symbol text,
  data jsonb
);

create table if not exists public.equity_snapshots (
  ts timestamptz primary key,
  cash double precision not null,
  equity double precision not null,
  open_positions integer not null default 0
);

create table if not exists public.smart_summary (
  id integer primary key,
  updated_at timestamptz not null default now(),
  list_size integer,
  tracked integer,
  tokens_done integer,
  tokens_skipped integer,
  blacklisted integer,
  payload jsonb
);

create index if not exists kills_cycle_idx on public.kills (cycle);
create index if not exists kills_ts_idx on public.kills (ts desc);
create index if not exists positions_status_idx on public.positions (status);
create index if not exists scores_ts_idx on public.scores (ts desc);
create index if not exists trades_ts_idx on public.trades (ts desc);

alter table public.desk_status enable row level security;
alter table public.cycles enable row level security;
alter table public.kills enable row level security;
alter table public.scores enable row level security;
alter table public.positions enable row level security;
alter table public.trades enable row level security;
alter table public.equity_snapshots enable row level security;
alter table public.smart_summary enable row level security;

create policy desk_status_public_read on public.desk_status for select to anon, authenticated using (true);
create policy cycles_public_read on public.cycles for select to anon, authenticated using (true);
create policy kills_public_read on public.kills for select to anon, authenticated using (true);
create policy scores_public_read on public.scores for select to anon, authenticated using (true);
create policy positions_public_read on public.positions for select to anon, authenticated using (true);
create policy trades_public_read on public.trades for select to anon, authenticated using (true);
create policy equity_snapshots_public_read on public.equity_snapshots for select to anon, authenticated using (true);
create policy smart_summary_public_read on public.smart_summary for select to anon, authenticated using (true);

alter publication supabase_realtime add table
  public.desk_status,
  public.cycles,
  public.kills,
  public.scores,
  public.positions,
  public.trades,
  public.equity_snapshots,
  public.smart_summary;
