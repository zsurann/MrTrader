#!/usr/bin/env python3
"""
MemeDesk — PAPER-TRADING ONLY Solana memecoin desk.
No wallets, no keys, no signing, no real transactions. Every trade is simulated.

Pipeline (hard, typed handoffs):
  Scout -> ScoutCandidate -> Filter -> FilterPass -> Analyst -> AnalystPass
        -> Executor -> PaperOrder -> Risk (manages PaperPosition) -> Logger
Every stage reports to Logger; a coin only advances via the typed record of the previous stage.
"""
import json, math, os, sqlite3, sys, threading, time, traceback, random, http.client, ssl, urllib.parse, hashlib, fcntl
from concurrent.futures import ThreadPoolExecutor
import urllib.request, urllib.error
from dataclasses import dataclass, asdict
from typing import Optional
import smartwallets as SWL   # free on-chain smart-wallet list + shadow rules (log-only)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("MEMEDESK_DATA", BASE)   # override for isolated test runs
DB_PATH = os.path.join(DATA, "desk.sqlite")
JSONL_PATH = os.path.join(DATA, "events.jsonl")
SUMMARY_PATH = os.path.join(DATA, "summary.txt")

CFG = dict(
    # Scout
    min_age_h=0.5, max_age_h=72.0, min_mcap=300_000, max_mcap=20_000_000,
    # Filter
    min_liq=40_000, min_vol_h1=50_000, max_top10_pct=25.0, max_dev_pct=3.0,
    max_bundle_pct=10.0, max_h1_change=80.0, min_primary_lp_locked=90.0,
    min_weighted_lp_locked=80.0, past_rug_dead_mcap=10_000, past_rug_min_dead=2,
    # Analyst
    pass_score=70.0,
    weights=dict(momentum=0.20, unique_buyers=0.15, buy_sell=0.20, holder_growth=0.15,
                 liq_mcap=0.15, smart_wallets=0.15),  # intended weights (sum 1.0); if smart data unavailable -> renormalised over the other 5
    # Executor
    start_equity=10_000.0, max_pos_frac=0.08, max_open=10, reentry_cooldown_h=6.0,
    base_slippage=0.005, swap_fee=0.0025,  # + price impact = size / (liq/2)
    # Risk
    stop_pct=0.06, trail_activate=0.12, trail_dist=0.06, risk_poll_s=30,
    # Loop
    scan_interval_s=180, rugcheck_cache_s=600,
    # Perf (no effect on rules): parallel I/O, background GeckoTerminal discovery, RPC cache for revoked mints
    http_workers=8, filter_workers=4, gt_refresh_s=150, rpc_revoked_cache_s=3600,
    old_mint_cache_s=1800,  # skip re-pricing mints already killed as pair-too-old; TTL so a new pair is still seen
    hard_filter_cache_s=1800,  # skip RugCheck for permanent hard kills (rugged / past rugs / transfer fee / permanent delegate)
    jsonl_rotate_bytes=15 * 1024 * 1024,
    # API pacing / caches (no effect on rules). Jupiter lite list TTLs by interval; 5m stays fresh each cycle.
    jup_list_ttl={"5m": 90, "1h": 180, "6h": 480, "24h": 600},
    jup_search_cache_s=45,   # only long enough to dedupe decimals+analyst in the same cycle
    jup_quote_cache_s=8,     # shadow/live reuse of the same paper quote
    gt_page_cache_s=150,     # shared with smartwallets so both processes don't re-fetch the same GT page
)
# Shadow-only exit variants. Log only: never read by live Executor/Risk. Do not put these in CFG.
SHADOW_TRAIL4 = dict(activate=0.12, dist=0.04)  # same +12% activation as the live trail, then 4% under the peak (live uses 6%)

UA = {"User-Agent": "memedesk-paper/1.0", "Accept": "application/json"}
BURN_ADDRS = {"1nc1nerator11111111111111111111111111111111", "11111111111111111111111111111111"}
SOL_MINT = "So11111111111111111111111111111111111111112"

def now(): return time.time()
def iso(ts=None): return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or now()))

# ---------------------------------------------------------------- HTTP w/ backoff
class Http:
    """Thread-safe GET/POST with keep-alive, a shared inter-process per-host limiter (desk + smartwallets
    must not independently exceed a free-tier cap), 429 backoff with jitter, and an optional GET cache.
    `priority=True` (open-position price monitoring) waits only the base gap, not a 429-widened one."""
    # Gaps are minimum seconds between *process-wide* calls. lite-api.jup.ag ~1 RPS free; GT ~30 req/min;
    # api.jup.ag free key = 1 RPS; Helius free = 10 RPS; DexScreener is generous.
    BASE_GAP = {"api.dexscreener.com": 0.22, "api.rugcheck.xyz": 1.2, "api.geckoterminal.com": 2.15,
                "api.mainnet-beta.solana.com": 0.5, "lite-api.jup.ag": 1.05, "api.jup.ag": 1.1,
                "mainnet.helius-rpc.com": 0.12}
    POOL_MAX = 6
    RATE_DIR = os.path.join(DATA, ".api_rate")
    CACHE_DIR = os.path.join(DATA, ".api_cache")
    def __init__(self):
        self.last, self.gap, self.locks = {}, dict(self.BASE_GAP), {}
        self.meta = threading.Lock()
        self.stats = {}
        self.pool, self.pool_lock = {}, threading.Lock()
        self.ctx = ssl.create_default_context()
        self._prio = threading.local()
        self._mem_cache = {}
        try:
            os.makedirs(self.RATE_DIR, exist_ok=True)
            os.makedirs(self.CACHE_DIR, exist_ok=True)
        except Exception:
            pass
    class _Live:
        def __init__(self, http): self.http = http
        def __enter__(self): self.http._prio.on = True
        def __exit__(self, *a): self.http._prio.on = False
    def prefer_live(self):
        """Context manager: Risk.poll / open-position quotes take the next base-gap slot."""
        return Http._Live(self)
    def _priority(self, priority):
        return bool(priority or getattr(self._prio, "on", False))
    def _lock(self, host):
        with self.meta: return self.locks.setdefault(host, threading.Lock())
    def _rate_path(self, host):
        safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in host)
        return os.path.join(self.RATE_DIR, safe + ".rate")
    def _write_rate(self, host, last, gap):
        try:
            path = self._rate_path(host)
            fd = open(path, "a+")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                fd.seek(0); fd.truncate()
                fd.write(f"{last:.6f} {gap:.6f}\n"); fd.flush()
            finally:
                try: fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception: pass
                fd.close()
        except Exception:
            pass
    def _throttle(self, host, priority=False):
        base = self.BASE_GAP.get(host, 0.5)
        prio = self._priority(priority)
        with self._lock(host):
            last, gap = self.last.get(host, 0.0), self.gap.get(host, base)
            path = self._rate_path(host)
            fd = None
            try:
                fd = open(path, "a+")
                fcntl.flock(fd, fcntl.LOCK_EX)
                fd.seek(0)
                raw = fd.read().strip()
                if raw:
                    parts = raw.split()
                    last = max(last, float(parts[0]))
                    if len(parts) > 1:
                        gap = float(parts[1]); self.gap[host] = gap
                use = base if prio else max(gap, base)
                wait = last + use - now()
                if wait > 0:
                    time.sleep(wait + random.uniform(0, min(0.08, use * 0.15)))
                last = now()
                fd.seek(0); fd.truncate()
                fd.write(f"{last:.6f} {self.gap.get(host, base):.6f}\n"); fd.flush()
            except Exception:
                use = base if prio else max(self.gap.get(host, base), base)
                wait = self.last.get(host, 0) + use - now()
                if wait > 0: time.sleep(wait)
                last = now()
            finally:
                if fd is not None:
                    try: fcntl.flock(fd, fcntl.LOCK_UN)
                    except Exception: pass
                    try: fd.close()
                    except Exception: pass
            self.last[host] = last
    def _ok(self, host):
        base = self.BASE_GAP.get(host, 0.5); g = self.gap.get(host, base)
        if g > base:
            self.gap[host] = max(base, g * 0.97)  # slow decay so we do not bounce back into 429s
            self._write_rate(host, self.last.get(host, now()), self.gap[host])
    def _slow(self, host):
        base = self.BASE_GAP.get(host, 0.5)
        self.gap[host] = min(base * 4, self.gap.get(host, base) * 1.6)
        self.stats[host + ":429"] = self.stats.get(host + ":429", 0) + 1
        self._write_rate(host, now(), self.gap[host])
    def _hit(self, key, n=1):
        self.stats[key] = self.stats.get(key, 0) + n
    def snapshot(self):
        return dict(self.stats)
    def _checkout(self, host, timeout=25):
        with self.pool_lock:
            stack = self.pool.setdefault(host, [])
            while stack:
                conn = stack.pop()
                self._hit(host + ":reuse")
                return conn
        self._hit(host + ":new")
        return http.client.HTTPSConnection(host, timeout=timeout, context=self.ctx)
    def _checkin(self, host, conn, reuse=True):
        if not reuse or conn is None:
            if conn is not None:
                try: conn.close()
                except Exception: pass
            return
        with self.pool_lock:
            stack = self.pool.setdefault(host, [])
            if len(stack) < self.POOL_MAX:
                stack.append(conn)
                return
        try: conn.close()
        except Exception: pass
    def _cache_key(self, url, body):
        """Hash of method+URL with secrets stripped. Never store a raw API key."""
        parts = urllib.parse.urlsplit(url)
        q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in ("api-key", "apikey", "authorization", "x-api-key")]
        safe = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(q), ""))
        raw = safe + ("|" + json.dumps(body, sort_keys=True, default=str) if body is not None else "")
        return hashlib.sha256(raw.encode()).hexdigest()
    def _cache_get(self, key, cache_s):
        hit = self._mem_cache.get(key)
        if hit and now() - hit[0] < cache_s:
            return hit[1]
        path = os.path.join(self.CACHE_DIR, key[:32] + ".json")
        try:
            rec = json.load(open(path))
            if now() - rec["ts"] < cache_s:
                body = rec["body"]
                self._mem_cache[key] = (rec["ts"], body)
                return body
        except Exception:
            return None
        return None
    def _cache_put(self, key, body):
        ts = now()
        self._mem_cache[key] = (ts, body)
        if len(self._mem_cache) > 800:
            cutoff = ts - 3600
            self._mem_cache = {k: v for k, v in self._mem_cache.items() if v[0] >= cutoff}
        try:
            path = os.path.join(self.CACHE_DIR, key[:32] + ".json")
            tmp = path + ".tmp"
            json.dump({"ts": ts, "body": body}, open(tmp, "w"))
            os.replace(tmp, path)
        except Exception:
            pass
    def get(self, url, body=None, tries=5, headers=None, cache_s=0, priority=False):
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or parts.netloc.split("@")[-1].split(":")[0]
        path = parts.path or "/"
        if parts.query: path += "?" + parts.query
        if cache_s and body is None:
            hit = self._cache_get(self._cache_key(url, None), cache_s)
            if hit is not None:
                self._hit(host + ":cache")
                return hit
        delay = 2.0
        data = json.dumps(body).encode() if body is not None else None
        method = "POST" if data is not None else "GET"
        h = dict(UA)
        if data: h["Content-Type"] = "application/json"
        if headers: h.update(headers)   # e.g. API keys: sent as headers only, never part of the URL/logs
        h["Connection"] = "keep-alive"
        stale = (BrokenPipeError, ConnectionError, http.client.BadStatusLine,
                 http.client.RemoteDisconnected, http.client.CannotSendRequest, OSError)
        i = 0
        while i < tries:
            self._throttle(host, priority=priority)
            conn = None
            try:
                conn = self._checkout(host, timeout=25)
                conn.request(method, path, body=data, headers=h)
                resp = conn.getresponse()
                raw = resp.read()
                code = resp.status
                reuse = (getattr(resp, "version", 11) == 11
                         and (resp.getheader("Connection") or "keep-alive").lower() != "close")
                self._checkin(host, conn, reuse=reuse)
                conn = None
                if 200 <= code < 300:
                    out = json.loads(raw.decode()) if raw else {}
                    self._ok(host); self._hit(host)
                    if cache_s and body is None and out is not None:
                        self._cache_put(self._cache_key(url, None), out)
                    return out
                if code == 404:
                    return None
                if code in (429, 500, 502, 503, 504):
                    if code == 429: self._slow(host)
                    ra = resp.getheader("Retry-After") if resp else None
                    extra = float(ra) if ra and str(ra).replace(".", "", 1).isdigit() else delay
                    time.sleep(extra + random.random() * 0.4)
                    delay = min(delay * 2, 60); i += 1; continue
                return None
            except stale:
                self._checkin(host, conn, reuse=False)
                self._hit(host + ":stale")
                i += 1
                continue
            except Exception:
                self._checkin(host, conn, reuse=False)
                time.sleep(delay + random.random() * 0.3); delay = min(delay * 2, 60); i += 1
        return None
    def rpc_batch(self, url, calls, tries=3, priority=False, batch_size=10):
        """JSON-RPC batch POST. `calls` is a list of (method, params); returns a list of results (None on item error)."""
        if not calls: return []
        out = [None] * len(calls)
        for off in range(0, len(calls), batch_size):
            chunk = calls[off:off + batch_size]
            body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(chunk)]
            d = self.get(url, body=body, tries=tries, priority=priority)
            if isinstance(d, list):
                by_id = {item.get("id"): item for item in d if isinstance(item, dict)}
                for i in range(len(chunk)):
                    item = by_id.get(i)
                    if item and "result" in item and "error" not in item:
                        out[off + i] = item["result"]
            elif isinstance(d, dict) and len(chunk) == 1 and "error" not in d:
                out[off] = d.get("result")
        return out
    def map(self, fn, items, workers=None):
        """Run fn over items concurrently (order preserved). Per-host throttling still applies."""
        items = list(items)
        if not items: return []
        with ThreadPoolExecutor(max_workers=min(workers or CFG["http_workers"], len(items))) as ex:
            return list(ex.map(fn, items))
HTTP = Http()

def helius_rpc_url():
    """Helius JSON-RPC URL. The key is only in the query string and is never logged."""
    k = os.environ.get("HELIUS_API_KEY")
    return f"https://mainnet.helius-rpc.com/?api-key={k}" if k else None

# ---------------------------------------------------------------- Jupiter quotes (paper fills only; nothing is ever signed)
class Jup:
    """Real Jupiter quotes for the exact paper size. Quote-only: no wallet, no swap transaction is ever built.
    Any failure -> None and the caller keeps the original fill model."""
    def __init__(self):
        self.key = os.environ.get("JUPITER_API_KEY")
        self.dec, self.sol, self.sol_ts, self._qc = {}, None, 0.0, {}
    def sol_usd(self):
        if not self.sol or now() - self.sol_ts > 60:
            d = HTTP.get(f"https://lite-api.jup.ag/price/v3?ids={SOL_MINT}", tries=2, cache_s=60) or {}
            p = (d.get(SOL_MINT) or {}).get("usdPrice")
            if p: self.sol, self.sol_ts = float(p), now()
        return self.sol
    def decimals(self, mint):
        if mint not in self.dec:
            d = HTTP.get(f"https://lite-api.jup.ag/tokens/v2/search?query={mint}", tries=2,
                         cache_s=CFG.get("jup_search_cache_s") or 45) or []
            t = next((x for x in d if isinstance(x, dict) and x.get("id") == mint), None) if isinstance(d, list) else None
            if t and t.get("decimals") is not None: self.dec[mint] = int(t["decimals"])
        return self.dec.get(mint)
    def _quote(self, inp, out, amount):
        if not self.key or amount <= 0: return None
        amount = int(amount)
        key = (inp, out, amount)
        hit = self._qc.get(key) if hasattr(self, "_qc") else None
        if hit and now() - hit[0] < (CFG.get("jup_quote_cache_s") or 8):
            return hit[1]
        d = HTTP.get(f"https://api.jup.ag/swap/v1/quote?inputMint={inp}&outputMint={out}&amount={amount}&slippageBps=500"
                     f"&restrictIntermediateTokens=true", tries=2, headers={"x-api-key": self.key})
        out_q = d if d and d.get("outAmount") else None
        if out_q:
            if not hasattr(self, "_qc"): self._qc = {}
            self._qc[key] = (now(), out_q)
        return out_q
    def buy_price(self, mint, usd):
        """Effective USD price per token for buying `usd` worth with SOL (includes DEX fees + price impact)."""
        try:
            su, dec = self.sol_usd(), self.decimals(mint)
            if not su or dec is None: return None
            q = self._quote(SOL_MINT, mint, usd / su * 1e9)
            if not q: return None
            toks = int(q["outAmount"]) / 10 ** dec
            return dict(price=usd / toks, impact_pct=round(100 * float(q.get("priceImpactPct") or 0), 3),
                        route="+".join((r.get("swapInfo") or {}).get("label") or "?" for r in q.get("routePlan") or [])) if toks else None
        except Exception: return None
    def sell_price(self, mint, qty):
        """Effective USD price per token for selling `qty` tokens into SOL."""
        try:
            su, dec = self.sol_usd(), self.decimals(mint)
            if not su or dec is None: return None
            q = self._quote(mint, SOL_MINT, qty * 10 ** dec)
            if not q: return None
            return dict(price=int(q["outAmount"]) / 1e9 * su / qty, impact_pct=round(100 * float(q.get("priceImpactPct") or 0), 3),
                        route="+".join((r.get("swapInfo") or {}).get("label") or "?" for r in q.get("routePlan") or []))
        except Exception: return None
JUP = Jup()

def paper_sell_fill(mint, qty, ref, liq, reuse=None):
    """Same paper exit fill as a live Risk sell: Jupiter quote with the live sanity band, else the model.
    `reuse` lets a shadow exit at the same ref+qty reuse a fill already quoted for the live close (saves a 1 RPS slot)."""
    if reuse and reuse.get("ref") == ref and reuse.get("qty") == qty and reuse.get("exit_px") is not None:
        return reuse["exit_px"], reuse["src"], reuse["model_px"], reuse.get("q")
    slip = CFG["base_slippage"] + CFG["swap_fee"] + (ref * qty) / max(liq / 2, 1)
    model_px = ref * (1 - slip); exit_px = model_px; src = "model"
    q = JUP.sell_price(mint, qty)
    if q and ref and 0.5 <= q["price"] / ref <= 1.05:
        exit_px, src = min(q["price"], ref * (1 - 0.0005)), "jupiter"
    return exit_px, src, model_px, q


# ---------------------------------------------------------------- Typed handoff records
@dataclass(frozen=True)
class ScoutCandidate:
    mint: str; symbol: str; name: str; pair_address: str; dex: str
    age_h: float; mcap: float; liq_usd: float; price_usd: float
    vol: dict; txns: dict; price_change: dict; sources: tuple; scouted_at: float

@dataclass(frozen=True)
class FilterPass:
    cand: ScoutCandidate
    checks: dict          # every check with measured value
    rugcheck: dict        # trimmed facts used by Analyst
    filtered_at: float

@dataclass(frozen=True)
class AnalystPass:
    fp: FilterPass; score: float; components: dict; analysed_at: float

@dataclass(frozen=True)
class PaperOrder:
    mint: str; symbol: str; pair_address: str; ref_price: float; fill_price: float
    size_usd: float; qty: float; slippage_pct: float; stop_price: float
    score: float; equity_at_entry: float; created_at: float

# ---------------------------------------------------------------- Logger (stage 6)
class Logger:
    def __init__(self):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")   # WAL+NORMAL: durable on checkpoint, no fsync per row
        self.db.execute("PRAGMA busy_timeout=8000")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.execute("PRAGMA cache_size=-8000")     # 8 MB page cache, modest on the e2-micro
        self.db.execute("PRAGMA wal_autocheckpoint=1000")
        self.jsonl = open(JSONL_PATH, "a", buffering=1)  # one handle, line-buffered
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, ts_iso TEXT, cycle INT, stage TEXT, kind TEXT, mint TEXT, symbol TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS kills(id INTEGER PRIMARY KEY, ts REAL, ts_iso TEXT, cycle INT, stage TEXT, mint TEXT, symbol TEXT, pair TEXT, reasons TEXT, metrics TEXT);
        CREATE TABLE IF NOT EXISTS scores(id INTEGER PRIMARY KEY, ts REAL, ts_iso TEXT, cycle INT, mint TEXT, symbol TEXT, score REAL, passed INT, components TEXT);
        CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, mint TEXT, symbol TEXT, pair TEXT, status TEXT,
            opened_ts REAL, opened_iso TEXT, ref_price REAL, entry_price REAL, size_usd REAL, qty REAL, entry_slip_pct REAL,
            initial_stop REAL, stop_price REAL, peak_price REAL, trail_active INT, last_price REAL, last_ts REAL,
            score REAL, closed_ts REAL, closed_iso TEXT, exit_ref REAL, exit_price REAL, exit_reason TEXT, pnl_usd REAL, pnl_pct REAL);
        CREATE TABLE IF NOT EXISTS ledger(k TEXT PRIMARY KEY, v REAL);
        CREATE TABLE IF NOT EXISTS holders(mint TEXT, ts REAL, holders INT);
        CREATE TABLE IF NOT EXISTS cycles(cycle INT PRIMARY KEY, ts REAL, ts_iso TEXT, stats TEXT);
        CREATE INDEX IF NOT EXISTS kills_cycle ON kills(cycle);
        CREATE INDEX IF NOT EXISTS kills_ts ON kills(ts);
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        CREATE INDEX IF NOT EXISTS scores_ts ON scores(ts);
        CREATE INDEX IF NOT EXISTS positions_status ON positions(status);
        CREATE INDEX IF NOT EXISTS holders_mint_ts ON holders(mint, ts);
        """)
        SWL.ensure_schema(self.db)
        if self.db.execute("SELECT v FROM ledger WHERE k='cash'").fetchone() is None:
            self.db.execute("INSERT INTO ledger VALUES('cash',?)", (CFG["start_equity"],))
        self.cycle = (self.db.execute("SELECT MAX(cycle) FROM cycles").fetchone()[0] or 0)

    def event(self, stage, kind, mint=None, symbol=None, **data):
        ts = now()
        rec = dict(ts=iso(ts), cycle=self.cycle, stage=stage, kind=kind, mint=mint, symbol=symbol, **data)
        with self.lock:
            self.jsonl.write(json.dumps(rec, default=str) + "\n")
            self.db.execute("INSERT INTO events(ts,ts_iso,cycle,stage,kind,mint,symbol,data) VALUES(?,?,?,?,?,?,?,?)",
                            (ts, iso(ts), self.cycle, stage, kind, mint, symbol, json.dumps(data, default=str)))

    def kill(self, stage, mint, symbol, pair, reasons, metrics):
        ts = now()
        with self.lock:
            self.db.execute("INSERT INTO kills(ts,ts_iso,cycle,stage,mint,symbol,pair,reasons,metrics) VALUES(?,?,?,?,?,?,?,?,?)",
                            (ts, iso(ts), self.cycle, stage, mint, symbol, pair, json.dumps(reasons), json.dumps(metrics, default=str)))
        self.event(stage, "KILL", mint, symbol, reasons=reasons, metrics=metrics)

    def kill_many(self, stage, rows):
        """Bulk kill logging, one transaction. Scout kills are stored in `kills` (the dashboard
        aggregates them per cycle) but not copied into events / events.jsonl — that duplication
        was ~700 rows every cycle and dominated log growth, without changing any trade."""
        if not rows: return
        ts = now(); ti = iso(ts)
        also_events = stage != "Scout"
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany("INSERT INTO kills(ts,ts_iso,cycle,stage,mint,symbol,pair,reasons,metrics) VALUES(?,?,?,?,?,?,?,?,?)",
                    [(ts, ti, self.cycle, stage, m, sym, pair, json.dumps(r), json.dumps(mt, default=str)) for m, sym, pair, r, mt in rows])
                if also_events:
                    self.db.executemany("INSERT INTO events(ts,ts_iso,cycle,stage,kind,mint,symbol,data) VALUES(?,?,?,?,?,?,?,?)",
                        [(ts, ti, self.cycle, stage, "KILL", m, sym, json.dumps(dict(reasons=r, metrics=mt), default=str)) for m, sym, pair, r, mt in rows])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK"); raise
            if also_events:
                self.jsonl.write("".join(json.dumps(dict(ts=ti, cycle=self.cycle, stage=stage, kind="KILL", mint=m, symbol=sym,
                                                         reasons=r, metrics=mt), default=str) + "\n" for m, sym, pair, r, mt in rows))
            self._maybe_rotate_jsonl()

    def score(self, mint, symbol, score, passed, components):
        ts = now()
        with self.lock:
            self.db.execute("INSERT INTO scores(ts,ts_iso,cycle,mint,symbol,score,passed,components) VALUES(?,?,?,?,?,?,?,?)",
                            (ts, iso(ts), self.cycle, mint, symbol, score, int(passed), json.dumps(components)))
        self.event("Analyst", "SCORE_PASS" if passed else "SCORE_FAIL", mint, symbol, score=score, components=components)

    def _maybe_rotate_jsonl(self):
        """Size-cap events.jsonl; the process reopens the file so later writes go to a fresh log."""
        try:
            limit = CFG.get("jsonl_rotate_bytes") or 15 * 1024 * 1024
            if self.jsonl.tell() < limit: return
            self.jsonl.flush(); self.jsonl.close()
            ts_tag = time.strftime("%Y%m%d-%H%M%S")
            dest = JSONL_PATH + "." + ts_tag
            os.rename(JSONL_PATH, dest)
            self.jsonl = open(JSONL_PATH, "a", buffering=1)
            # keep at most 5 rotated files
            olds = sorted(p for p in os.listdir(os.path.dirname(JSONL_PATH) or ".") if p.startswith(os.path.basename(JSONL_PATH) + ".") and not p.endswith(".tmp"))
            # also match events.jsonl.YYYYMMDD-HHMMSS
            rotated = sorted(os.path.join(os.path.dirname(JSONL_PATH) or ".", p) for p in os.listdir(os.path.dirname(JSONL_PATH) or ".")
                             if p.startswith("events.jsonl."))
            for extra in rotated[:-5]:
                try: os.remove(extra)
                except Exception: pass
        except Exception:
            try:
                if self.jsonl.closed: self.jsonl = open(JSONL_PATH, "a", buffering=1)
            except Exception: pass

    def cycle_done(self, stats):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO cycles VALUES(?,?,?,?)", (self.cycle, now(), iso(), json.dumps(stats)))
            if self.cycle % 5 == 0:
                try: self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception: pass
            self._maybe_rotate_jsonl()
        self.event("Logger", "CYCLE_DONE", **stats)
        self.write_summary()

    def write_summary(self):
        try:
            import report
            txt = report.build_report(DB_PATH, since_hours=None)
            with open(SUMMARY_PATH, "w") as f: f.write(txt)
        except Exception as e:
            self.event("Logger", "SUMMARY_ERROR", error=repr(e))

# ---------------------------------------------------------------- Scout (stage 1)
class Scout:
    SEARCH_TERMS = ["pump", "sol", "solana", "meme", "pepe", "dog", "cat", "ai", "trump", "bonk", "moon", "inu",
                    "pumpswap", "raydium", "meteora", "frog", "elon", "baby", "coin", "token", "the", "agent", "wif", "chill"]
    GT_PATHS = ([f"trending_pools?duration={d}&page={p}" for d in ("1h", "6h", "24h") for p in (1, 2, 3)]
                + [f"pools?sort=h24_volume_usd_desc&page={p}" for p in range(1, 6)]
                + [f"pools?sort=h24_tx_count_desc&page={p}" for p in range(1, 4)])
    def __init__(self, log: Logger):
        self.log = log
        self.gt = None; self.gt_ts = 0.0; self.gt_ready = threading.Event()
        self.old_mints = {}  # mint -> (cached_at, pairCreatedAt, sym, pair, reasons, metrics)
        threading.Thread(target=self._gt_loop, daemon=True, name="gt-discovery").start()

    def _gt_sweep(self):
        found = {}
        for path in self.GT_PATHS:   # sequential on purpose: GT allows ~30 req/min
            d = HTTP.get(f"https://api.geckoterminal.com/api/v2/networks/solana/{path}",
                         cache_s=CFG.get("gt_page_cache_s") or 150) or {}
            for p in d.get("data") or []:
                bt = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                if bt.startswith("solana_"): found.setdefault(bt[7:], set()).add("gt_" + path.split("?")[0])
        return found
    def _gt_loop(self):
        while True:
            t0 = now()
            try:
                self.gt = self._gt_sweep(); self.gt_ts = now(); self.gt_ready.set()
            except Exception as e:
                self.log.event("Scout", "GT_ERROR", error=repr(e))
            time.sleep(max(5, CFG["gt_refresh_s"] - (now() - t0)))

    def discover_mints(self):
        mints, src = {}, {}
        def add(m, s):
            if not m or m == SOL_MINT: return
            mints[m] = 1; src.setdefault(m, set()).add(s)
        jobs = [("ds_list", f"https://api.dexscreener.com/{path}", s, 0) for path, s in [
                    ("token-profiles/latest/v1", "ds_profiles"), ("token-boosts/latest/v1", "ds_boosts_latest"),
                    ("token-boosts/top/v1", "ds_boosts_top"), ("community-takeovers/latest/v1", "ds_cto")]]
        jobs += [("ds_search", f"https://api.dexscreener.com/latest/dex/search?q={q}", "ds_search", 0) for q in self.SEARCH_TERMS]
        ttl = CFG.get("jup_list_ttl") or {"5m": 90, "1h": 180, "6h": 480, "24h": 600}
        jobs += [("jup", f"https://lite-api.jup.ag/tokens/v2/{cat}/{iv}?limit=100", "jup_" + cat, int(ttl.get(iv, 120)))
                 for cat in ("toptrending", "toptraded", "toporganicscore") for iv in ("5m", "1h", "6h", "24h")]
        results = HTTP.map(lambda j: HTTP.get(j[1], cache_s=j[3]), jobs)
        for j, d in zip(jobs, results):   # merged in the same order as the sequential version
            kind, _, s = j[0], j[1], j[2]
            if kind == "ds_search":
                for p in (d or {}).get("pairs") or []:
                    if p.get("chainId") == "solana": add(p["baseToken"]["address"], s)
            else:
                for x in d if isinstance(d, list) else []:
                    add(x.get("tokenAddress") if kind == "ds_list" and x.get("chainId") == "solana" else
                        (x.get("id") if kind == "jup" else None), s)
        # GeckoTerminal / Jupiter used ONLY for discovery of addresses; all metrics come from DexScreener
        # Don't stall the first cycle for a full GT sweep (~40-90s); later cycles see a fresh background snapshot.
        self.gt_ready.wait(timeout=8)
        for m, ss in (self.gt or {}).items():
            for s_ in ss: add(m, s_)
        self.gt_age_s = round(now() - self.gt_ts, 0) if self.gt_ts else None
        return list(mints), src

    def run(self):
        mints, src = self.discover_mints()
        # Tokens already known to be older than max_age_h cannot become younger. Re-price them only
        # after a TTL so a *new* higher-liq pair on the same mint is still picked up.
        ttl = CFG.get("old_mint_cache_s") or 1800
        to_price, cached_rows = [], []
        for m in mints:
            hit = self.old_mints.get(m)
            if hit and now() - hit[0] < ttl:
                cached_at, created, sym, pair, reasons, metrics = hit
                age_h = (now()*1000 - created) / 3.6e6 if created else None
                mt = dict(metrics)
                if age_h is not None: mt["age_h"] = age_h
                mt["cached_too_old"] = True
                cached_rows.append((m, sym, pair, list(reasons), mt))
            else:
                to_price.append(m)
        best = {}
        batches = [to_price[i:i+30] for i in range(0, len(to_price), 30)]   # DexScreener tokens/v1 max 30 addrs
        if batches:
            for d in HTTP.map(lambda b: HTTP.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(b)) or [], batches):
                for p in d if isinstance(d, list) else []:
                    if p.get("chainId") != "solana": continue
                    m = p["baseToken"]["address"]
                    if m not in src: continue
                    liq = (p.get("liquidity") or {}).get("usd") or 0
                    if m not in best or liq > ((best[m].get("liquidity") or {}).get("usd") or 0): best[m] = p
        out, scout_kills, kill_rows = [], 0, list(cached_rows)
        scout_kills += len(cached_rows)
        for m, p in best.items():
            age_h = (now()*1000 - (p.get("pairCreatedAt") or 0)) / 3.6e6 if p.get("pairCreatedAt") else None
            mcap = p.get("marketCap") or p.get("fdv") or 0
            reasons = []
            if age_h is None: reasons.append("data unavailable: pair age")
            elif age_h < CFG["min_age_h"]: reasons.append(f"pair too young: {age_h*60:.0f}m < 30m")
            elif age_h > CFG["max_age_h"]: reasons.append(f"pair too old: {age_h:.1f}h > 72h")
            if not mcap: reasons.append("data unavailable: market cap")
            elif mcap < CFG["min_mcap"]: reasons.append(f"mcap ${mcap:,.0f} < $300k")
            elif mcap > CFG["max_mcap"]: reasons.append(f"mcap ${mcap:,.0f} > $20M")
            sym = p["baseToken"].get("symbol")
            if reasons:
                scout_kills += 1
                metrics = dict(age_h=age_h, mcap=mcap, sources=sorted(src[m]))
                kill_rows.append((m, sym, p.get("pairAddress"), reasons, metrics))
                # Only "too old" is a hard scout kill (age only grows). mcap / too-young can newly qualify.
                if age_h is not None and age_h > CFG["max_age_h"] and p.get("pairCreatedAt"):
                    self.old_mints[m] = (now(), p.get("pairCreatedAt"), sym, p.get("pairAddress"), tuple(reasons), metrics)
                else:
                    self.old_mints.pop(m, None)
                continue
            self.old_mints.pop(m, None)
            out.append(ScoutCandidate(
                mint=m, symbol=sym, name=p["baseToken"].get("name"), pair_address=p["pairAddress"], dex=p.get("dexId"),
                age_h=round(age_h, 2), mcap=mcap, liq_usd=(p.get("liquidity") or {}).get("usd") or 0,
                price_usd=float(p.get("priceUsd") or 0), vol=p.get("volume") or {}, txns=p.get("txns") or {},
                price_change=p.get("priceChange") or {}, sources=tuple(sorted(src[m])), scouted_at=now()))
        # Drop stale cache entries for mints that disappeared from discovery
        live = set(mints)
        for m in list(self.old_mints):
            if m not in live: self.old_mints.pop(m, None)
        self.log.kill_many("Scout", kill_rows)
        self.log.event("Scout", "SCOUT_DONE", discovered=len(mints), priced=len(best), priced_cached=len(cached_rows),
                       in_window=len(out), killed=scout_kills, gt_age_s=getattr(self, "gt_age_s", None))
        return out, dict(discovered=len(mints), priced=len(best), priced_cached=len(cached_rows),
                         scout_kills=scout_kills, in_window=len(out))

_SENTINEL = object()
# ---------------------------------------------------------------- Filter (stage 2)
class Filter:
    def __init__(self, log: Logger):
        self.log = log; self.cache = {}; self.rpc_cache = {}; self.hard_kills = {}
    def rugcheck(self, mint):
        c = self.cache.get(mint)
        if c and now() - c[0] < CFG["rugcheck_cache_s"]: return c[1]
        d = HTTP.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
        if d: self.cache[mint] = (now(), d)
        return d
    def _parse_mint_info(self, result):
        try: return result["value"]["data"]["parsed"]["info"]
        except Exception: return None
    def rpc_mint(self, mint):
        pref = getattr(self, "_prefetched", {}).get(mint, _SENTINEL)
        if pref is not _SENTINEL: return pref
        c = self.rpc_cache.get(mint)
        if c and now() - c[0] < CFG["rpc_revoked_cache_s"]: return c[1]
        info = self._rpc_mint(mint)
        if info is not None and not info.get("mintAuthority") and not info.get("freezeAuthority"):
            self.rpc_cache[mint] = (now(), info)
        return info
    def _rpc_url(self):
        return helius_rpc_url() or "https://api.mainnet-beta.solana.com"
    def _note_helius(self, n):
        if n <= 0 or not helius_rpc_url(): return
        day = time.strftime("%Y-%m-%d")
        try:
            with self.log.lock:
                self.log.db.execute(
                    "INSERT INTO helius_usage(day,calls,purpose) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET calls=calls+?",
                    (day, n, "desk_rpc", n))
        except Exception:
            pass
    def prefetch_rpc(self, mints):
        """One JSON-RPC batch (Helius if configured) for mints that already passed cheap market checks."""
        self._prefetched = {}
        need = []
        for m in mints:
            c = self.rpc_cache.get(m)
            if c and now() - c[0] < CFG["rpc_revoked_cache_s"]:
                self._prefetched[m] = c[1]
            else:
                need.append(m)
        if not need: return
        url = self._rpc_url()
        calls = [("getAccountInfo", [m, {"encoding": "jsonParsed"}]) for m in need]
        results = HTTP.rpc_batch(url, calls, tries=3, batch_size=20 if "helius" in url else 10)
        if "helius" in url: self._note_helius(len(need))
        for m, raw in zip(need, results):
            info = self._parse_mint_info(raw)
            self._prefetched[m] = info
            if info is not None and not info.get("mintAuthority") and not info.get("freezeAuthority"):
                self.rpc_cache[m] = (now(), info)
    def _rpc_mint(self, mint):
        url = self._rpc_url()
        batch = HTTP.rpc_batch(url, [("getAccountInfo", [mint, {"encoding": "jsonParsed"}])], tries=3,
                               batch_size=1)
        if "helius" in url: self._note_helius(1)
        return self._parse_mint_info(batch[0] if batch else None)

    def cheap_reasons(self, c: ScoutCandidate):
        """Market checks only (no I/O). Same reasons/checks as the first block of run()."""
        reasons, checks = [], {}
        liq = c.liq_usd; v1 = (c.vol or {}).get("h1"); ch1 = (c.price_change or {}).get("h1")
        checks["liquidity_usd"] = liq; checks["vol_h1"] = v1; checks["price_change_h1"] = ch1
        if not liq: reasons.append("data unavailable: liquidity")
        elif liq < CFG["min_liq"]: reasons.append(f"liquidity ${liq:,.0f} < $40k")
        if v1 is None: reasons.append("data unavailable: 1h volume")
        elif v1 < CFG["min_vol_h1"]: reasons.append(f"1h volume ${v1:,.0f} < $50k")
        if ch1 is None: reasons.append("data unavailable: 1h price change")
        elif ch1 > CFG["max_h1_change"]: reasons.append(f"already +{ch1:.0f}% in 1h (> +80%)")
        return reasons, checks

    def run(self, c: ScoutCandidate) -> Optional[FilterPass]:
        assert isinstance(c, ScoutCandidate)
        reasons, checks = self.cheap_reasons(c)
        if reasons:   # cheap market checks failed -> kill without spending RugCheck quota
            checks["onchain_checks"] = "skipped (market checks already failed)"
            self.log.kill("Filter", c.mint, c.symbol, c.pair_address, reasons, checks); return None

        hard = self.hard_kills.get(c.mint)
        if hard and now() - hard[0] < CFG.get("hard_filter_cache_s", 1800):
            checks = dict(hard[2])
            checks["hard_kill_cached"] = True
            self.log.kill("Filter", c.mint, c.symbol, c.pair_address, list(hard[1]), checks); return None

        rc = self.rugcheck(c.mint)
        if not rc:
            self.log.kill("Filter", c.mint, c.symbol, c.pair_address, ["data unavailable: RugCheck report"], checks); return None
        tok = rc.get("token") or {}
        supply = float(tok.get("supply") or 0)
        # --- authorities (RugCheck + Solana RPC cross-check)
        ma, fa = tok.get("mintAuthority", rc.get("mintAuthority")), tok.get("freezeAuthority", rc.get("freezeAuthority"))
        if "mintAuthority" not in tok and "mintAuthority" not in rc: reasons.append("data unavailable: mint authority")
        if "freezeAuthority" not in tok and "freezeAuthority" not in rc: reasons.append("data unavailable: freeze authority")
        rpc = self.rpc_mint(c.mint)
        if rpc is not None:
            checks["rpc_crosscheck"] = "ok"
            ma = ma or rpc.get("mintAuthority"); fa = fa or rpc.get("freezeAuthority")
        else: checks["rpc_crosscheck"] = "rpc unavailable (RugCheck value used)"
        checks["mint_authority"] = ma; checks["freeze_authority"] = fa
        if ma: reasons.append(f"mint authority NOT revoked ({ma[:8]}…)")
        if fa: reasons.append(f"freeze authority NOT revoked ({fa[:8]}…)")
        # --- LP burned/locked
        markets = rc.get("markets") or []
        prim = next((m for m in markets if m.get("pubkey") == c.pair_address), None)
        tot_usd = locked_usd = 0.0
        for m in markets:
            lp = m.get("lp") or {}
            u = float(lp.get("baseUSD") or 0) + float(lp.get("quoteUSD") or 0)
            tot_usd += u; locked_usd += u * float(lp.get("lpLockedPct") or 0) / 100
        wpct = 100 * locked_usd / tot_usd if tot_usd else None
        ppct = ((prim or {}).get("lp") or {}).get("lpLockedPct")
        checks.update(lp_primary_market=(prim or {}).get("marketType"), lp_primary_locked_pct=ppct, lp_weighted_locked_pct=wpct)
        if ppct is None: reasons.append("data unavailable: LP lock status of primary pair")
        elif ppct < CFG["min_primary_lp_locked"]: reasons.append(f"LP not burned/locked: primary pair {ppct:.0f}% locked")
        if wpct is not None and wpct < CFG["min_weighted_lp_locked"]:
            reasons.append(f"LP not burned/locked: only {wpct:.0f}% of total pool liquidity locked/burned")
        # --- top-10 excluding pools/lockers/burn
        known = rc.get("knownAccounts") or {}
        excl = set(BURN_ADDRS)
        for a, k in known.items():
            if (k.get("type") or "").upper() in ("AMM", "LOCKER"): excl.add(a)
        for m in markets:
            for k in ("pubkey", "liquidityA", "liquidityB"):
                if m.get(k): excl.add(m[k])
            for k in ("liquidityAAccount", "liquidityBAccount"):
                if (m.get(k) or {}).get("owner"): excl.add(m[k]["owner"])
        holders = rc.get("topHolders")
        if not holders: reasons.append("data unavailable: top holders")
        else:
            real = [h for h in holders if h.get("address") not in excl and h.get("owner") not in excl]
            top10 = sum(float(h.get("pct") or 0) for h in real[:10])
            checks["top10_pct_ex_pools"] = round(top10, 2); checks["holders_excluded_as_pool_or_burn"] = len(holders) - len(real)
            if top10 > CFG["max_top10_pct"]: reasons.append(f"top-10 holders {top10:.1f}% > 25%")
        # --- dev/creator holdings + past rugs
        creator = rc.get("creator")
        cb = rc.get("creatorBalance")
        dev_pct = None
        if cb is not None and supply: dev_pct = 100 * float(cb) / supply
        if creator and holders:
            hp = sum(float(h.get("pct") or 0) for h in holders if h.get("owner") == creator or h.get("address") == creator)
            dev_pct = max(dev_pct or 0, hp)
        checks["creator"] = creator; checks["dev_pct"] = None if dev_pct is None else round(dev_pct, 3)
        if dev_pct is None: reasons.append("data unavailable: creator balance")
        elif dev_pct > CFG["max_dev_pct"]: reasons.append(f"dev holds {dev_pct:.1f}% > 3%")
        ctoks = rc.get("creatorTokens") or []
        dead = [t for t in ctoks if t.get("mint") != c.mint and float(t.get("marketCap") or 0) < CFG["past_rug_dead_mcap"]]
        rug_risks = [r.get("name") for r in (rc.get("risks") or []) if "rug" in (r.get("name") or "").lower()
                     or "rug" in (r.get("description") or "").lower()]
        checks["creator_prior_tokens"] = len([t for t in ctoks if t.get("mint") != c.mint]); checks["creator_dead_tokens"] = len(dead)
        checks["rugcheck_rug_risks"] = rug_risks; checks["rugcheck_rugged_flag"] = rc.get("rugged")
        if rc.get("rugged"): reasons.append("RugCheck flags token as rugged")
        if rug_risks: reasons.append(f"dev past rugs (RugCheck risk: {', '.join(rug_risks)})")
        if len(dead) >= CFG["past_rug_min_dead"]:
            reasons.append(f"dev past rugs (proxy): creator has {len(dead)} prior tokens now < ${CFG['past_rug_dead_mcap']:,} mcap")
        # --- bundled launch / insiders
        ins_hold = sum(float(h.get("pct") or 0) for h in (holders or []) if h.get("insider"))
        net_pct = 0.0
        for n in rc.get("insiderNetworks") or []:
            if supply: net_pct += 100 * float(n.get("tokenAmount") or 0) / supply
        bundle = max(ins_hold, net_pct)
        checks["bundle_pct_proxy"] = round(bundle, 2); checks["insider_networks"] = len(rc.get("insiderNetworks") or [])
        checks["graph_insiders_detected"] = rc.get("graphInsidersDetected")
        if bundle > CFG["max_bundle_pct"]: reasons.append(f"bundled/insider supply {bundle:.1f}% > 10%")
        # --- RugCheck danger-level risks (e.g. mutable metadata / transfer fee / permanent delegate)
        danger = [r.get("name") for r in (rc.get("risks") or []) if (r.get("level") or "") == "danger"]
        checks["rugcheck_danger_risks"] = danger
        if (rc.get("token_extensions") or {}).get("permanentDelegate"): reasons.append("token-2022 permanent delegate set")
        tf = (rc.get("transferFee") or {}).get("pct") or 0
        if tf > 0: reasons.append(f"transfer fee {tf}%")

        if reasons:
            # Permanent (or nearly so) filter kills: do not re-hit RugCheck every cycle. Authorities
            # that are NOT yet revoked can later be revoked, so those are NOT cached here.
            hard_needles = ("RugCheck flags token as rugged", "dev past rugs", "token-2022 permanent delegate",
                            "transfer fee")
            if any(any(n in r for n in hard_needles) for r in reasons):
                self.hard_kills[c.mint] = (now(), tuple(reasons), dict(checks))
            self.log.kill("Filter", c.mint, c.symbol, c.pair_address, reasons, checks); return None
        self.hard_kills.pop(c.mint, None)
        facts = dict(totalHolders=rc.get("totalHolders"), top10=checks.get("top10_pct_ex_pools"), score=rc.get("score_normalised"))
        self.log.event("Filter", "FILTER_PASS", c.mint, c.symbol, checks=checks)
        return FilterPass(cand=c, checks=checks, rugcheck=facts, filtered_at=now())

# ---------------------------------------------------------------- Analyst (stage 3)
def clamp(x, a=0.0, b=100.0): return max(a, min(b, x))
class Analyst:
    def __init__(self, log: Logger): self.log = log; self.smart = SWL.SmartBook(log.db, log.lock)
    def run(self, fp: FilterPass) -> Optional[AnalystPass]:
        assert isinstance(fp, FilterPass)
        c = fp.cand; pc = c.price_change; tx = c.txns
        comp, notes = {}, {}
        # momentum: reward sustained positive trend, penalise dumps; h1 sweet spot 5-60%
        m5, h1, h6 = (pc.get("m5") or 0), (pc.get("h1") or 0), (pc.get("h6") or 0)
        s = 50 + 1.5 * clamp(m5, -15, 15) + 0.6 * clamp(h1, -40, 50) + 0.15 * clamp(h6, -100, 150)
        if m5 < -5 and h1 < 0: s -= 15
        comp["momentum"] = clamp(s); notes["momentum"] = dict(m5=m5, h1=h1, h6=h6)
        jd = HTTP.get(f"https://lite-api.jup.ag/tokens/v2/search?query={c.mint}", cache_s=CFG.get("jup_search_cache_s") or 45) or []
        jt = next((t for t in jd if isinstance(jd, list) and t.get("id") == c.mint), {}) if isinstance(jd, list) else {}
        j1 = jt.get("stats1h") or {}
        b1 = ((tx.get("h1") or {}).get("buys") or 0); s1 = ((tx.get("h1") or {}).get("sells") or 0)
        b5 = ((tx.get("m5") or {}).get("buys") or 0); s5 = ((tx.get("m5") or {}).get("sells") or 0)
        if j1.get("numNetBuyers") is not None:
            nb = j1["numNetBuyers"]
            comp["unique_buyers"] = clamp(100 * math.log10(1 + max(nb, 0)) / math.log10(1 + 1000))
            notes["unique_buyers"] = dict(src="jupiter stats1h", net_buyers_1h=nb, traders_1h=j1.get("numTraders"), organic_buyers_1h=j1.get("numOrganicBuyers"))
        else:   # fallback proxy: DexScreener 1h buy-tx count
            comp["unique_buyers"] = clamp(100 * math.log10(1 + b1) / math.log10(1 + 3000))
            notes["unique_buyers"] = dict(src="dexscreener buy txns (proxy)", h1_buy_txns=b1)
        notes["jupiter_organic_score"] = jt.get("organicScore")
        # buy/sell ratio: blend 1h and 5m; 1.0 -> 40, 1.5 -> 80, >=1.75 -> 100
        r1 = b1 / max(s1, 1); r5 = b5 / max(s5, 1)
        r = 0.6 * r1 + 0.4 * min(r5, 3)
        comp["buy_sell"] = clamp(40 + 80 * (r - 1.0)); notes["buy_sell"] = dict(r_h1=round(r1, 2), r_m5=round(r5, 2))
        # holder growth: RugCheck totalHolders tracked across cycles; first sight -> holders/age
        H = fp.rugcheck.get("totalHolders")
        with self.log.lock:
            prev = self.log.db.execute("SELECT ts,holders FROM holders WHERE mint=? AND ts<? ORDER BY ts ASC LIMIT 1",
                                       (c.mint, now() - 600)).fetchone()
            if H:
                self.log.db.execute("INSERT INTO holders VALUES(?,?,?)", (c.mint, now(), int(H)))
        if H and prev and prev[1]:
            hrs = (now() - prev[0]) / 3600; g = (H - prev[1]) / prev[1] / max(hrs, 1/6) * 100   # % per hour
            comp["holder_growth"] = clamp(30 + 5 * g); notes["holder_growth"] = dict(method="tracked across cycles (RugCheck totalHolders)", holders=H, prev=prev[1], pct_per_h=round(g, 2))
        elif j1.get("holderChange") is not None:
            g = j1["holderChange"]
            comp["holder_growth"] = clamp(30 + 5 * g); notes["holder_growth"] = dict(method="jupiter stats1h.holderChange (first sight)", holders=H, pct_1h=round(g, 2))
        elif H:
            rate = H / max(c.age_h, 0.5)
            comp["holder_growth"] = clamp(100 * math.log10(1 + rate) / math.log10(1 + 500)); notes["holder_growth"] = dict(method="first-sight holders/age", holders=H, per_h=round(rate, 1))
        else:
            comp["holder_growth"] = 0.0; notes["holder_growth"] = "no holder data (scored 0)"
        # liq/mcap: 0.05 -> 25, 0.10 -> 50, >=0.20 -> 100
        lm = c.liq_usd / c.mcap if c.mcap else 0
        comp["liq_mcap"] = clamp(500 * lm); notes["liq_mcap"] = round(lm, 4)
        # smart wallets: distinct tracked smart wallets (free on-chain list, see smartwallets.py) that bought recently
        # 0 -> 0, 1 -> 45, 2 -> 75, 3+ -> 100. Lookup failure / list too small -> None -> renormalised (never penalises)
        try: sv, sinfo = self.smart.factor(lambda u: HTTP.get(u, tries=2), c.mint, c.pair_address)
        except Exception as e: sv, sinfo = None, dict(status=f"error {e!r}: factor skipped, weights renormalised")
        comp["smart_wallets"] = sv; notes["smart_wallets"] = sinfo
        W = CFG["weights"]; tw = sum(w for k, w in W.items() if comp.get(k) is not None and w > 0)
        score = round(sum(comp[k] * w for k, w in W.items() if comp.get(k) is not None and w > 0) / tw, 1)
        components = {k: (None if v is None else round(v, 1)) for k, v in comp.items()}
        components["_weights_effective"] = {k: round(w / tw, 3) for k, w in W.items() if w > 0}
        components["_inputs"] = notes
        passed = score >= CFG["pass_score"]
        self.log.score(c.mint, c.symbol, score, passed, components)
        if not passed:
            self.log.kill("Analyst", c.mint, c.symbol, c.pair_address, [f"score {score} < 70"],
                          {k: v for k, v in components.items() if not k.startswith("_")})
            return None
        return AnalystPass(fp=fp, score=score, components=components, analysed_at=now())

# ---------------------------------------------------------------- Executor (stage 4)
class Executor:
    def __init__(self, log: Logger, risk): self.log = log; self.risk = risk
    def equity(self):
      with self.log.lock:
        cash = self.log.db.execute("SELECT v FROM ledger WHERE k='cash'").fetchone()[0]
        mtm = sum(q * (lp or ep) for q, lp, ep in self.log.db.execute("SELECT qty,last_price,entry_price FROM positions WHERE status='OPEN'"))
        return cash, cash + mtm
    def run(self, ap: AnalystPass) -> Optional[PaperOrder]:
        assert isinstance(ap, AnalystPass)
        c = ap.fp.cand
        with self.log.lock:
            db = self.log.db
            if db.execute("SELECT 1 FROM positions WHERE mint=? AND status='OPEN'", (c.mint,)).fetchone():
                self.log.kill("Executor", c.mint, c.symbol, c.pair_address, ["already holding open paper position"], {}); return None
            last = db.execute("SELECT MAX(closed_ts) FROM positions WHERE mint=?", (c.mint,)).fetchone()[0]
            if last and now() - last < CFG["reentry_cooldown_h"] * 3600:
                self.log.kill("Executor", c.mint, c.symbol, c.pair_address, [f"re-entry cooldown ({CFG['reentry_cooldown_h']}h)"], {}); return None
            nopen = db.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
            if nopen >= CFG["max_open"]:
                self.log.kill("Executor", c.mint, c.symbol, c.pair_address, [f"max open positions ({CFG['max_open']}) reached"], {}); return None
            # fresh price right before the (paper) fill
            p = HTTP.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{c.pair_address}") or {}
            pr = ((p.get("pairs") or [None])[0] or {})
            ref = float(pr.get("priceUsd") or 0); liq = (pr.get("liquidity") or {}).get("usd") or c.liq_usd
            if not ref:
                self.log.kill("Executor", c.mint, c.symbol, c.pair_address, ["data unavailable: fresh price at entry"], {}); return None
            cash, eq = self.equity()
            size = min(CFG["max_pos_frac"] * eq, cash)
            if size < 50:
                self.log.kill("Executor", c.mint, c.symbol, c.pair_address, ["insufficient paper cash"], dict(cash=cash)); return None
            slip = CFG["base_slippage"] + CFG["swap_fee"] + size / (liq / 2)
            model_fill = ref * (1 + slip); fill = model_fill; fill_src = "model"
            q = JUP.buy_price(c.mint, size)   # real Jupiter quote for the exact size (quote only, nothing signed)
            if q and 0.9 <= q["price"] / ref <= 1.15:   # sanity band vs fresh DexScreener price; else keep model
                fill, fill_src = max(q["price"], ref * (1 + 0.0005)), "jupiter"
                slip = fill / ref - 1
            self.log.event("Executor", "FILL_QUOTE", c.mint, c.symbol, side="buy", size_usd=round(size, 2), ref_price=ref,
                           modeled_slip_pct=round(100 * (model_fill / ref - 1), 3),
                           quoted_slip_pct=(round(100 * (q["price"] / ref - 1), 3) if q else None),
                           jup_impact_pct=(q or {}).get("impact_pct"), route=(q or {}).get("route"), used=fill_src)
            order = PaperOrder(mint=c.mint, symbol=c.symbol, pair_address=c.pair_address, ref_price=ref, fill_price=fill,
                               size_usd=round(size, 2), qty=size / fill, slippage_pct=round(slip * 100, 3),
                               stop_price=fill * (1 - CFG["stop_pct"]), score=ap.score, equity_at_entry=round(eq, 2), created_at=now())
            self.risk.open(order)   # stop is attached atomically with the order inside Risk.open
            return order

# ---------------------------------------------------------------- Risk (stage 5)
class Risk:
    def __init__(self, log: Logger):
        self.log = log
        self._shadow_booted = False
        self._t4w = self._t4x = self._t4c = set()
        self._cw = self._cp = self._cx = self._cc = set()

    def open(self, o: PaperOrder):
        assert isinstance(o, PaperOrder) and o.stop_price < o.fill_price
        db = self.log.db
        with self.log.lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""INSERT INTO positions(mint,symbol,pair,status,opened_ts,opened_iso,ref_price,entry_price,size_usd,qty,entry_slip_pct,
                    initial_stop,stop_price,peak_price,trail_active,last_price,last_ts,score) VALUES(?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                    (o.mint, o.symbol, o.pair_address, o.created_at, iso(o.created_at), o.ref_price, o.fill_price, o.size_usd, o.qty,
                     o.slippage_pct, o.stop_price, o.stop_price, o.fill_price, o.ref_price, now(), o.score))
                db.execute("UPDATE ledger SET v=v-? WHERE k='cash'", (o.size_usd,))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK"); raise
        self.log.event("Executor", "ENTRY", o.mint, o.symbol, order={k: v for k, v in asdict(o).items() if k not in ("mint", "symbol")})

    # ---- shadow exits (log only; a failure here must never block a live fill or close)
    def _shadow_ids(self, rule, kind):
        with self.log.lock:
            return {r[0] for r in self.log.db.execute(
                "SELECT DISTINCT position_id FROM shadow_events WHERE rule=? AND kind=?", (rule, kind)).fetchall() if r[0] is not None}

    def _shadow_row(self, rule, kind, pid):
        with self.log.lock:
            r = self.log.db.execute(
                "SELECT ts,ts_iso,data FROM shadow_events WHERE rule=? AND kind=? AND position_id=? ORDER BY id DESC LIMIT 1",
                (rule, kind, pid)).fetchone()
        if not r: return None
        d = json.loads(r[2] or "{}"); d["_ts"], d["_ts_iso"] = r[0], r[1]
        return d

    def _sev(self, rule, kind, mint, symbol, pid, **data):
        SWL.shadow_event(self.log.db, self.log.lock, self.log.jsonl, rule, kind, mint, symbol, pid, **data)

    def _shadow_boot(self):
        if self._shadow_booted: return
        self._t4w, self._t4x, self._t4c = self._shadow_ids("trail_4pct", "WATCH"), self._shadow_ids("trail_4pct", "SHADOW_EXIT"), self._shadow_ids("trail_4pct", "COMPARE")
        self._cw, self._cp, self._cx, self._cc = (self._shadow_ids("combo_tp_trail4", "WATCH"), self._shadow_ids("combo_tp_trail4", "PARTIAL"),
                                                  self._shadow_ids("combo_tp_trail4", "SHADOW_EXIT"), self._shadow_ids("combo_tp_trail4", "COMPARE"))
        self._shadow_booted = True

    def _shadow_fill_payload(self, ref, exit_px, src, model_px, q, qty):
        return dict(ref_price=ref, exit_price=exit_px, fill_src=src,
                    modeled_slip_pct=(round(100 * (1 - model_px / ref), 3) if ref else None),
                    quoted_slip_pct=(round(100 * (1 - q["price"] / ref), 3) if q and ref else None),
                    jup_impact_pct=(q or {}).get("impact_pct"), route=(q or {}).get("route"), qty=qty)

    def shadow_open_tick(self, pid, mint, sym, ep, qty, size, px, liq, peak, reuse=None):
        """Track shadow stops for one open position on every Risk poll. Writes WATCH / PARTIAL / SHADOW_EXIT only."""
        if not ep or not qty or not px: return
        act_lvl = ep * (1 + SHADOW_TRAIL4["activate"])
        trail_on = peak >= act_lvl
        shadow_stop = peak * (1 - SHADOW_TRAIL4["dist"]) if trail_on else None

        if pid not in self._t4w:
            self._sev("trail_4pct", "WATCH", mint, sym, pid, entry_price=ep, activate=SHADOW_TRAIL4["activate"],
                      dist=SHADOW_TRAIL4["dist"], peak=peak)
            self._t4w.add(pid)
        if pid not in self._t4x and trail_on and px <= shadow_stop:
            exit_px, src, model_px, q = paper_sell_fill(mint, qty, px, liq, reuse)
            pnl = exit_px * qty - size
            self._sev("trail_4pct", "SHADOW_EXIT", mint, sym, pid, entry_price=ep, peak=peak, shadow_stop=shadow_stop,
                      size_usd=size, hyp_pnl_usd=round(pnl, 2), hyp_pnl_pct=round(100 * pnl / size, 2) if size else None,
                      **self._shadow_fill_payload(px, exit_px, src, model_px, q, qty))
            self._t4x.add(pid)

        frac, tp_pct = SWL.PTP["frac"], SWL.PTP["tp_pct"]
        if pid not in self._cw:
            self._sev("combo_tp_trail4", "WATCH", mint, sym, pid, entry_price=ep, frac=frac, tp_pct=tp_pct,
                      activate=SHADOW_TRAIL4["activate"], dist=SHADOW_TRAIL4["dist"], peak=peak)
            self._cw.add(pid)
        # Partial first (only when we actually observe px at/above +15%). Then the 4% trail applies to whatever is still held.
        if pid not in self._cp and pid not in self._cx and px >= ep * (1 + tp_pct):
            pq = frac * qty
            exit_px, src, model_px, q = paper_sell_fill(mint, pq, px, liq)
            proceeds = exit_px * pq
            self._sev("combo_tp_trail4", "PARTIAL", mint, sym, pid, entry_price=ep, tp_price=ep * (1 + tp_pct), peak=peak,
                      frac=frac, proceeds=proceeds, **self._shadow_fill_payload(px, exit_px, src, model_px, q, pq))
            self._cp.add(pid)
        if pid not in self._cx and trail_on and px <= shadow_stop:
            rem_frac = (1 - frac) if pid in self._cp else 1.0
            rq = rem_frac * qty
            rreuse = reuse if rem_frac == 1.0 else None
            exit_px, src, model_px, q = paper_sell_fill(mint, rq, px, liq, rreuse)
            proceeds = exit_px * rq
            self._sev("combo_tp_trail4", "SHADOW_EXIT", mint, sym, pid, entry_price=ep, peak=peak, shadow_stop=shadow_stop,
                      rem_frac=rem_frac, proceeds=proceeds, size_usd=size,
                      **self._shadow_fill_payload(px, exit_px, src, model_px, q, rq))
            self._cx.add(pid)

    def shadow_compare_close(self, pid, mint, sym, qty, size, actual_pnl, actual_pct, reason, exit_px):
        """Once the live paper position closes, record actual vs shadow PnL for trail_4pct and the combo variant."""
        if pid not in self._t4c:
            hyp = self._shadow_row("trail_4pct", "SHADOW_EXIT", pid)
            if hyp:
                spnl, spct = hyp.get("hyp_pnl_usd"), hyp.get("hyp_pnl_pct")
                triggered = True
            else:
                spnl, spct, triggered = round(actual_pnl or 0, 2), round(actual_pct or 0, 2), False
            self._sev("trail_4pct", "COMPARE", mint, sym, pid, triggered=triggered,
                      actual_pnl_usd=round(actual_pnl or 0, 2), actual_pnl_pct=round(actual_pct or 0, 2),
                      actual_reason=reason, actual_exit_price=exit_px,
                      shadow_pnl_usd=spnl, shadow_pnl_pct=spct,
                      shadow_exit_ts=(hyp or {}).get("_ts"), shadow_exit_iso=(hyp or {}).get("_ts_iso"),
                      shadow_ref=(hyp or {}).get("ref_price"), shadow_exit_price=(hyp or {}).get("exit_price"),
                      shadow_fill_src=(hyp or {}).get("fill_src"))
            self._t4c.add(pid)
        if pid not in self._cc:
            part = self._shadow_row("combo_tp_trail4", "PARTIAL", pid)
            trail = self._shadow_row("combo_tp_trail4", "SHADOW_EXIT", pid)
            proceeds = 0.0
            if part: proceeds += float(part.get("proceeds") or 0)
            rem_frac = (1 - SWL.PTP["frac"]) if part else 1.0
            if trail: proceeds += float(trail.get("proceeds") or 0)
            elif qty: proceeds += rem_frac * qty * (exit_px or 0)
            spnl = proceeds - (size or 0)
            self._sev("combo_tp_trail4", "COMPARE", mint, sym, pid,
                      tp_hit=bool(part), trail_hit=bool(trail),
                      actual_pnl_usd=round(actual_pnl or 0, 2), actual_pnl_pct=round(actual_pct or 0, 2),
                      actual_reason=reason, actual_exit_price=exit_px,
                      shadow_pnl_usd=round(spnl, 2), shadow_pnl_pct=(round(100 * spnl / size, 2) if size else None),
                      partial_exit_iso=(part or {}).get("_ts_iso"), partial_exit_price=(part or {}).get("exit_price"),
                      shadow_exit_ts=(trail or {}).get("_ts"), shadow_exit_iso=(trail or {}).get("_ts_iso"),
                      shadow_ref=(trail or {}).get("ref_price"), shadow_exit_price=(trail or {}).get("exit_price"),
                      rem_frac=(trail or {}).get("rem_frac", rem_frac if trail or part else 1.0))
            self._cc.add(pid)

    def shadow_reconcile(self):
        """Backfill COMPARE for watched positions that closed without one (e.g. desk restarted mid-write)."""
        with self.log.lock:
            rows = self.log.db.execute(
                "SELECT id,mint,symbol,qty,size_usd,pnl_usd,pnl_pct,exit_reason,exit_price FROM positions WHERE status='CLOSED'").fetchall()
        for pid, mint, sym, qty, size, pnl, pct, reason, xp in rows:
            if (pid in self._t4w and pid not in self._t4c) or (pid in self._cw and pid not in self._cc):
                self.shadow_compare_close(pid, mint, sym, qty, size, pnl, pct, reason, xp)

    def poll(self):
        with HTTP.prefer_live():
            try:
                self._shadow_boot(); self.shadow_reconcile()
            except Exception as e:
                self.log.event("Shadow", "ERROR", error=repr(e), tb=traceback.format_exc()[-600:])
            with self.log.lock:
              rows = self.log.db.execute("SELECT id,mint,symbol,pair,entry_price,qty,size_usd,stop_price,peak_price,trail_active,initial_stop FROM positions WHERE status='OPEN'").fetchall()
            if not rows: return
            prices = {}
            pairs = list({r[3] for r in rows})
            for i in range(0, len(pairs), 30):
                d = HTTP.get("https://api.dexscreener.com/latest/dex/pairs/solana/" + ",".join(pairs[i:i+30])) or {}
                for p in d.get("pairs") or []:
                    prices[p["pairAddress"]] = (float(p.get("priceUsd") or 0), (p.get("liquidity") or {}).get("usd") or 0)
            for pid, mint, sym, pair, ep, qty, size, stop, peak, trail, istop in rows:
                if pair not in prices or not prices[pair][0]:
                    self.log.event("Risk", "PRICE_MISSING", mint, sym, pair=pair); continue
                px, liq = prices[pair]
                new_peak = max(peak, px)
                new_trail = trail or new_peak >= ep * (1 + CFG["trail_activate"])
                new_stop = stop
                if new_trail: new_stop = max(stop, new_peak * (1 - CFG["trail_dist"]))
                if new_trail and not trail:
                    self.log.event("Risk", "TRAIL_ACTIVATED", mint, sym, peak=new_peak, entry=ep, stop=new_stop)
                live_reuse = None
                if px <= new_stop:
                    # live fill first (unchanged model), then shadow can reuse it when the size matches
                    exit_px, fill_src, model_px, q = paper_sell_fill(mint, qty, px, liq)
                    live_reuse = dict(ref=px, qty=qty, exit_px=exit_px, src=fill_src, model_px=model_px, q=q)
                    self.log.event("Risk", "FILL_QUOTE", mint, sym, side="sell", qty=qty, ref_price=px,
                                   modeled_slip_pct=round(100 * (1 - model_px / px), 3),
                                   quoted_slip_pct=(round(100 * (1 - q["price"] / px), 3) if q else None),
                                   jup_impact_pct=(q or {}).get("impact_pct"), route=(q or {}).get("route"), used=fill_src)
                    proceeds = exit_px * qty; pnl = proceeds - size
                    reason = "trailing stop" if new_trail else "stop-loss -6%"
                    with self.log.lock:
                        db = self.log.db; db.execute("BEGIN IMMEDIATE")
                        try:   # same all-or-nothing pattern as Risk.open: a failed write must never leave the shared connection mid-transaction
                            db.execute("""UPDATE positions SET status='CLOSED',last_price=?,last_ts=?,peak_price=?,stop_price=?,trail_active=?,
                                closed_ts=?,closed_iso=?,exit_ref=?,exit_price=?,exit_reason=?,pnl_usd=?,pnl_pct=? WHERE id=?""",
                                (px, now(), new_peak, new_stop, int(new_trail), now(), iso(), px, exit_px, reason, pnl, 100 * pnl / size, pid))
                            db.execute("UPDATE ledger SET v=v+? WHERE k='cash'", (proceeds,)); db.execute("COMMIT")
                        except Exception:
                            db.execute("ROLLBACK"); raise
                    self.log.event("Risk", "EXIT", mint, sym, reason=reason, ref_price=px, exit_price=exit_px, entry=ep,
                                   pnl_usd=round(pnl, 2), pnl_pct=round(100 * pnl / size, 2), peak=new_peak, stop=new_stop)
                    try:
                        self.shadow_open_tick(pid, mint, sym, ep, qty, size, px, liq, new_peak, reuse=live_reuse)
                        self.shadow_compare_close(pid, mint, sym, qty, size, pnl, 100 * pnl / size, reason, exit_px)
                    except Exception as e:
                        self.log.event("Shadow", "ERROR", mint=mint, symbol=sym, error=repr(e), tb=traceback.format_exc()[-600:])
                else:
                    try:
                        self.shadow_open_tick(pid, mint, sym, ep, qty, size, px, liq, new_peak)
                    except Exception as e:
                        self.log.event("Shadow", "ERROR", mint=mint, symbol=sym, error=repr(e), tb=traceback.format_exc()[-600:])
                    with self.log.lock:
                        self.log.db.execute("UPDATE positions SET last_price=?,last_ts=?,peak_price=?,stop_price=?,trail_active=? WHERE id=?",
                                            (px, now(), new_peak, new_stop, int(new_trail), pid))

    # ---------------------------------------------------------------- Orchestrator
class Desk:
    def __init__(self):
        self.log = Logger(); self.scout = Scout(self.log); self.filter = Filter(self.log)
        self.analyst = Analyst(self.log); self.risk = Risk(self.log); self.exec = Executor(self.log, self.risk)
        self.stop = threading.Event()
    def risk_loop(self):
        while not self.stop.is_set():
            try: self.risk.poll()
            except Exception as e: self.log.event("Risk", "ERROR", error=repr(e), tb=traceback.format_exc()[-800:])
            self.stop.wait(CFG["risk_poll_s"])
    def shadow_entry_gate(self, ap, order):
        """Shadow rule (a), LOG ONLY: would 'buy only if >= 2 smart wallets entered' have blocked this pass/entry?"""
        try:
            c = ap.fp.cand; si = (ap.components.get("_inputs") or {}).get("smart_wallets") or {}
            n = si.get("count") if isinstance(si, dict) else None
            pid = None
            if order:
                with self.log.lock:
                    r = self.log.db.execute("SELECT id FROM positions WHERE mint=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (c.mint,)).fetchone()
                pid = r[0] if r else None
            SWL.shadow_event(self.log.db, self.log.lock, self.log.jsonl, "min2_smart", "ENTRY_GATE", c.mint, c.symbol, pid,
                             cycle=self.log.cycle, score=ap.score, smart_count=n, smart_factor=ap.components.get("smart_wallets"),
                             would_block=(None if n is None else n < 2), entered=bool(order),
                             note=None if n is not None else "smart data unavailable -> rule undecidable")
            sk = SWL.skeptic(lambda u: HTTP.get(u, tries=2), c)   # shadow 'Skeptic' checklist, log only
            SWL.shadow_event(self.log.db, self.log.lock, self.log.jsonl, "skeptic", "ENTRY_GATE", c.mint, c.symbol, pid,
                             cycle=self.log.cycle, score=ap.score, entered=bool(order), **sk)
        except Exception as e:
            self.log.event("Shadow", "ERROR", error=repr(e))
    def cycle(self):
        self.log.cycle += 1
        t0 = now(); http0 = HTTP.snapshot(); self.log.event("Logger", "CYCLE_START")
        t_sc = now()
        cands, st = self.scout.run()
        scout_s = now() - t_sc
        # Cheap market kills first (no RugCheck/RPC/Jupiter). Then one batched Helius getAccountInfo
        # for the survivors, so we never spend on-chain quota on tokens Scout/market already rejected.
        alive = []
        for c in cands:
            reasons, checks = self.filter.cheap_reasons(c)
            if reasons:
                checks["onchain_checks"] = "skipped (market checks already failed)"
                self.filter.log.kill("Filter", c.mint, c.symbol, c.pair_address, reasons, checks)
            else:
                alive.append(c)
        self.filter.prefetch_rpc([c.mint for c in alive])
        fpass = apass = entries = 0
        def fa(c):   # Filter -> Analyst handoff for one coin (independent per coin, I/O bound)
            fp = self.filter.run(c)
            return fp, (self.analyst.run(fp) if fp else None)
        t_fa = now()
        for fp, ap in HTTP.map(fa, alive, workers=CFG["filter_workers"]):
            if not fp: continue
            fpass += 1
            if not ap: continue
            apass += 1
            order = self.exec.run(ap)
            if order: entries += 1
            self.shadow_entry_gate(ap, order)
        fa_s = now() - t_fa
        cash, eq = self.exec.equity()
        http1 = HTTP.snapshot()
        http_delta = {k: http1.get(k, 0) - http0.get(k, 0) for k in http1 if http1.get(k, 0) != http0.get(k, 0)}
        st.update(filter_pass=fpass, analyst_pass=apass, entries=entries, cash=round(cash, 2), equity=round(eq, 2),
                  secs=round(now() - t0, 1), scout_s=round(scout_s, 1), filter_s=round(fa_s, 1), http=http_delta)
        self.log.cycle_done(st)
        print(iso(), "cycle", self.log.cycle, st, flush=True)
    def main(self, once=False):
        self.log.event("Logger", "DESK_START", mode="PAPER", cfg=CFG)
        if once:   # test mode: a single cycle, no risk thread
            self.cycle(); print("http", HTTP.stats, flush=True); return
        threading.Thread(target=self.risk_loop, daemon=True).start()
        while True:
            t0 = now()
            try: self.cycle()
            except Exception as e:
                self.log.event("Logger", "CYCLE_ERROR", error=repr(e), tb=traceback.format_exc()[-1500:])
                print(iso(), "cycle error", repr(e), flush=True)
            time.sleep(max(30, CFG["scan_interval_s"] - (now() - t0)))

if __name__ == "__main__":
    sys.path.insert(0, BASE)
    Desk().main(once="--once" in sys.argv)
