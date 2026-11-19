#!/usr/bin/env python3
"""
MemeDesk — PAPER-TRADING ONLY Solana memecoin desk.
No wallets, no keys, no signing, no real transactions. Every trade is simulated.

Pipeline (hard, typed handoffs):
  Scout -> ScoutCandidate -> Filter -> FilterPass -> Analyst -> AnalystPass
        -> Executor -> PaperOrder -> Risk (manages PaperPosition) -> Logger
Every stage reports to Logger; a coin only advances via the typed record of the previous stage.
"""
import json, math, os, sqlite3, sys, threading, time, traceback, random, http.client, ssl, urllib.parse, hashlib, fcntl, base64, struct, re
from concurrent.futures import ThreadPoolExecutor
import urllib.request, urllib.error
from dataclasses import dataclass, asdict
from typing import Optional
import smartwallets as SWL   # free on-chain smart-wallet list + shadow rules (log-only)
import shadow_v9 as SV9      # v9 SHADOW-only rules (ghost tracker + entry gates), log only

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("MEMEDESK_DATA", BASE)   # override for isolated test runs
DB_PATH = os.path.join(DATA, "desk.sqlite")
JSONL_PATH = os.path.join(DATA, "events.jsonl")
SUMMARY_PATH = os.path.join(DATA, "summary.txt")
HEALTH_PATH = os.path.join(DATA, "cycle_health.json")
PAUSE_PATH = os.path.join(DATA, "PAUSE_ENTRIES")   # v8.1: if this file exists the Executor opens no new paper positions   # v7: last successful cycle + consecutive cycle errors

CFG = dict(
    # Scout
    min_age_h=0.5, max_age_h=6.0, min_mcap=100_000,   # v10: max age 168h -> 6h (ex-shadow new_pair_6h)
    max_mcap=20_000_000,   # v8_loose (user-approved 2026-11-18): age 72h -> 168h, mcap min 300k -> 100k
    # Filter
    min_liq=25_000, min_vol_h1=15_000, max_top10_pct=35.0, max_dev_pct=5.0,   # v8_loose: liq 100k -> 25k, vol1h 50k -> 15k, top10 25 -> 35%, dev 3 -> 5%
    max_bundle_pct=20.0, max_h1_change=150.0, min_primary_lp_locked=90.0,   # v8_loose: bundle 10 -> 20%, 1h pump cap +80 -> +150%; LP lock unchanged
    min_weighted_lp_locked=80.0, past_rug_dead_mcap=10_000, past_rug_min_dead=2,
    # Analyst
    pass_score=55.0,   # v8_loose: 70 -> 55
    weights=dict(momentum=0.20, unique_buyers=0.15, buy_sell=0.20, holder_growth=0.15,
                 liq_mcap=0.15, smart_wallets=0.15),  # intended weights (sum 1.0); if smart data unavailable -> renormalised over the other 5
    # Executor
    start_equity=1_000.0, min_size_usd=10.0, max_pos_frac=0.03,   # v10: paper ledger restarts at 1000 $ (min order 50 -> 10 $)
    max_open=15, reentry_cooldown_h=1.0,   # v8_loose: size 5% -> 3%, max open 10 -> 15, cooldown 6h -> 1h
    max_liq_frac=0.0025,   # v6: size also capped at 0.25% of pool liquidity (modeled impact <= 0.5% per side)
    base_slippage=0.005, swap_fee=0.0025,  # + price impact = size / (liq/2)
    # Risk
    stop_pct=0.10, trail_activate=0.12, trail_dist=0.04, risk_poll_s=30,   # v10: trail 6% -> 4% (ex-shadow trail_4pct)
    be_arm=0.08, be_lock=0.01,   # v10: once the peak passes +8% over the fill, stop >= fill +1% (ex-shadow breakeven_8)
    min_score_old=70.0,          # v10: pre-v6 score (smart 0 counts as 0) must also be >= 70 (ex-shadow old_rules_v5 entry)
    crash_quote_max_impact_pct=5.0,   # v10.1: sells take a Jupiter quote below the 50% band when its route impact is <= this
    smart_exit_live=True,        # v10: close when a tracked smart wallet sells the coin (ex-shadow smart_exit)
      # v6: stop -10% from the MARKET price at entry (was -6% from the fill); trail +12% / 6% also from market prices
    # Loop
    scan_interval_s=180, rugcheck_cache_s=600,
    smart_cache_s=1800,   # v8.1 (Helius budget): reuse a mint's smart-wallet lookup for 30 min (was 10 min)
    helius_total_daily_credits=24000,   # v8.1: smart lookups stop (factor neutral) once smart + WS + other Helius use today passes this (free tier ~33k/day)
    helius_smart_skip_hopeless=True,    # v8.1: skip the smart lookup when even 3+ smart wallets could not lift the score to pass_score (decision-neutral)
    # Perf (no effect on rules): parallel I/O, background GeckoTerminal discovery, RPC cache for revoked mints
    http_workers=6, filter_workers=4, gt_refresh_s=420, rpc_revoked_cache_s=3600,
    old_mint_cache_s=1800,  # skip re-pricing mints already killed as pair-too-old; TTL so a new pair is still seen
    hard_filter_cache_s=1800,  # skip RugCheck for permanent hard kills (rugged / past rugs / transfer fee / permanent delegate)
    jsonl_rotate_bytes=15 * 1024 * 1024,
    # API pacing / caches (no effect on rules). Jupiter lite list TTLs by interval; 5m stays fresh each cycle.
    jup_list_ttl={"5m": 90, "1h": 210, "6h": 540, "24h": 780},  # staggered so list refreshes do not pile up
    jup_search_cache_s=45,   # only long enough to dedupe decimals+analyst in the same cycle
    jup_quote_cache_s=8,     # shadow/live reuse of the same paper quote
    gt_page_cache_s=420,     # shared with smartwallets; = gt_refresh_s (GT public API: ~10/min documented, ~5/min observed)
    # v3 (no effect on rules): background Jupiter list refresher, shadow fast-stop watcher, PumpPortal shadow discovery
    fast_stop_poll_s=10,     # SHADOW ONLY: Jupiter price/v3 poll of open positions (log only, never closes a trade)
    pumpportal_ws=True,      # SHADOW ONLY: free PumpPortal migration stream, logged and measured, NOT added to Scout
    # v4 (user-approved): Helius reserve prices are the LIVE exit trigger when healthy; same rule levels.
    helius_ws_live=True, ws_max_dev_pct=8.0, ws_silent_s=15, helius_ws_daily_credits=9000, ws_poll_post_watch_s=300,
)
# Shadow-only exit variants. Log only: never read by live Executor/Risk. Do not put these in CFG.
SHADOW_TRAIL4 = dict(activate=0.12, dist=0.04)  # trail used by the shadow combo_tp_trail4 (= live trail since v10)
# v5 shadow-only candidates from the best-practice review (log only, never read by live code).
SHADOW_ENTRY_V5 = dict(organic_50=50.0, churn_50=0.5, churn_max_age_h=24.0, m5_chase_10=10.0, top1_10=10.0)
SHADOW_EXIT_V5 = dict(time_stop_60m=dict(minutes=60, min_gain=0.03),   # v10: breakeven_8 is live now
                      old_rules_v5=dict(stop=0.06, activate=0.12, dist=0.06, base="fill"))   # v6: what the pre-v6 exit rules would have done
OLD_RULES_V5 = dict(min_liq=40_000, max_pos_frac=0.08, stop_pct=0.06, smart_zero_counts=True, pass_score=70.0)   # pre-v6 live values, shadow reference only
RULESET = "v10_sel"   # tag stored on every score row and position (compare rule sets / score buckets later)
# v8: the v6/v7 LIVE entry rules, kept as a SHADOW reference (log only). Every v8 entry records whether these would have allowed it.
STRICT_V7 = dict(pass_score=70.0, min_liq=100_000, min_vol_h1=50_000, min_mcap=300_000, max_mcap=20_000_000, max_age_h=72.0,
                 max_h1_change=80.0, max_top10_pct=25.0, max_dev_pct=3.0, max_bundle_pct=10.0)
def strict_v7_reasons(c, checks, score):
    """-> list of reasons the v7 strict live entry rules would have rejected this candidate ([] = would pass)."""
    S = STRICT_V7; r = []; v1 = (c.vol or {}).get("h1"); ch1 = (c.price_change or {}).get("h1"); checks = checks or {}
    if score is None or score < S["pass_score"]: r.append(f"score {score} < 70")
    if (c.liq_usd or 0) < S["min_liq"]: r.append("liquidity < $100k")
    if v1 is None or v1 < S["min_vol_h1"]: r.append("1h volume < $50k")
    if not (S["min_mcap"] <= (c.mcap or 0) <= S["max_mcap"]): r.append("mcap outside $300k-$20M")
    if c.age_h is None or c.age_h > S["max_age_h"]: r.append("pair older than 72h")
    if ch1 is not None and ch1 > S["max_h1_change"]: r.append("already > +80% in 1h")
    for k, lim, lab in (("top10_pct_ex_pools", S["max_top10_pct"], "top-10 > 25%"), ("dev_pct", S["max_dev_pct"], "dev > 3%"),
                        ("bundle_pct_proxy", S["max_bundle_pct"], "bundle/insider > 10%")):
        v = checks.get(k)
        if v is not None and v > lim: r.append(lab)
    return r
def score_bucket(s):
    return None if s is None else ("<55" if s < 55 else "55-60" if s < 60 else "60-65" if s < 65 else "65-70" if s < 70 else "70+")

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
    # Tuned for the Oracle E2.1.Micro (free-tier caps, shared desk+smartwallets limiter).
    # v3 (measured 2026-11-17 on the Oracle IP): GT public docs say ~10 calls/min (keyless doc: ~5/min, dynamic);
    # we still got 429s at ~9 s spacing, so the base is 12.5 s (~4.8/min).
    # Jupiter: lite-api.jup.ag and api.jup.ag (even with our key) share ONE per-IP sliding window of
    # 5 requests / 10 s (x-ratelimit headers) -> one rate group "jup" at 2.05s.
    BASE_GAP = {"api.dexscreener.com": 0.22, "api.rugcheck.xyz": 1.2, "api.geckoterminal.com": 12.5,
                "api.mainnet-beta.solana.com": 0.7, "jup": 2.05,
                "mainnet.helius-rpc.com": 0.15}   # v8.1: ~6.7 req/s, and a JSON-RPC batch now counts as N requests (Helius free: 10 RPS)
    RATE_GROUP = {"lite-api.jup.ag": "jup", "api.jup.ag": "jup"}
    PRIO_GAP = {"api.geckoterminal.com": 6.2}   # Analyst lookups during the gated Filter/Analyst phase
    MIN_429_WAIT = {"api.geckoterminal.com": 12.0, "jup": 4.0,
                    "api.mainnet-beta.solana.com": 4.0}
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
    def _throttle(self, host, priority=False, weight=1):
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
                use = self.PRIO_GAP.get(host, base) if prio else max(gap, base)
                wait = last + use - now()
                if wait > 0:
                    time.sleep(wait + random.uniform(0, min(0.08, use * 0.15)))
                last = now() + max(0, weight - 1) * use   # v8.1: a batch of N calls books N slots for every process sharing the limiter
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
        self.gap[host] = min(base * 3, self.gap.get(host, base) * 1.5)
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
    def get(self, url, body=None, tries=5, headers=None, cache_s=0, priority=False, weight=1):
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
            self._throttle(self.RATE_GROUP.get(host, host), priority=priority, weight=weight)
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
                    self._ok(self.RATE_GROUP.get(host, host)); self._hit(host)
                    if cache_s and body is None and out is not None:
                        self._cache_put(self._cache_key(url, None), out)
                    return out
                if code == 404:
                    return None
                if code in (429, 500, 502, 503, 504):
                    grp = self.RATE_GROUP.get(host, host)
                    if code == 429: self._slow(grp); self._hit(host + ":429")
                    ra = resp.getheader("Retry-After") if resp else None
                    extra = float(ra) if ra and str(ra).replace(".", "", 1).isdigit() else delay
                    rs = resp.getheader("x-ratelimit-reset") if resp else None   # Jupiter: unix ts when a slot frees
                    if code == 429 and rs and str(rs).isdigit():
                        extra = max(extra, min(15.0, float(rs) - now() + 0.3))
                    extra = max(extra, self.MIN_429_WAIT.get(grp, 0))
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
            d = self.get(url, body=body, tries=tries, priority=priority, weight=(len(chunk) if "helius" in url else 1))
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
# v3: set while the cycle's Filter/Analyst phase runs. Background GeckoTerminal users (discovery sweep,
# shadow Skeptic) wait for it to clear so the Analyst's GT trades lookup never queues behind them.
CRITICAL = threading.Event()
_HTOT = [0.0, 0.0]
def helius_total_today(db, lock):
    """v8.1: Helius credits used today (smart lookups + WS estimate + other RPC calls at 1 credit each). Cached 60 s."""
    if now() - _HTOT[1] < 60: return _HTOT[0]
    day = time.strftime("%Y-%m-%d"); tot = 0.0
    try:
        with lock:
            for q in ("SELECT credits FROM helius_smart_usage WHERE day=?", "SELECT credits_est FROM helius_ws_usage WHERE day=?",
                      "SELECT calls FROM helius_usage WHERE day=?"):
                try:
                    r = db.execute(q, (day,)).fetchone(); tot += float(r[0]) if r and r[0] else 0.0
                except Exception: pass
    except Exception: pass
    _HTOT[0], _HTOT[1] = tot, now()
    return tot
def wait_not_critical(max_s=240):
    t0 = time.time()
    while CRITICAL.is_set() and time.time() - t0 < max_s: time.sleep(0.5)

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

MIN_MODEL_LIQ = 1_000.0    # v8.2: below this the liquidity reading is treated as unknown (Helius feed before the first DexScreener poll)
MAX_MODEL_SLIP = 0.50      # v8.2: modeled exit slippage + impact is capped at 50% (the old formula could exceed 100% -> negative fills)
def model_slip(notional, liq):
    """Paper slippage model (base + fee + impact = notional / (liq/2)), capped. -> (slip, liq_known)."""
    known = bool(liq) and liq >= MIN_MODEL_LIQ
    imp = notional / (liq / 2) if known else MAX_MODEL_SLIP
    return min(MAX_MODEL_SLIP, CFG["base_slippage"] + CFG["swap_fee"] + imp), known
def sane_exit(exit_px, ref):
    """v8.2 fill guard: an exit fill is always > 0 and within [ref x (1 - MAX_MODEL_SLIP), ref]."""
    if not ref or ref <= 0: return max(0.0, exit_px or 0.0)
    lo, hi = ref * (1 - MAX_MODEL_SLIP), ref * (1 - 0.0005)
    if exit_px is None or not math.isfinite(exit_px) or exit_px <= 0: return lo
    return min(max(exit_px, lo), hi)
def paper_sell_fill(mint, qty, ref, liq, reuse=None, liq_fallback=None):
    """Same paper exit fill as a live Risk sell: Jupiter quote with the live sanity band, else the model.
    `reuse` lets a shadow exit at the same ref+qty reuse a fill already quoted for the live close (saves a 1 RPS slot).
    v8.2: unknown/tiny liquidity uses `liq_fallback` (entry liquidity), the model is capped and the result is clamped > 0."""
    if reuse and reuse.get("ref") == ref and reuse.get("qty") == qty and reuse.get("exit_px") is not None:
        return reuse["exit_px"], reuse["src"], reuse["model_px"], reuse.get("q")
    if (not liq or liq < MIN_MODEL_LIQ) and liq_fallback and liq_fallback >= MIN_MODEL_LIQ: liq = liq_fallback
    slip, _ = model_slip(ref * qty, liq)
    model_px = sane_exit(ref * (1 - slip), ref); exit_px = model_px; src = "model"
    q = JUP.sell_price(mint, qty)
    if q and ref and 0.5 <= q["price"] / ref <= 1.15:   # v8.2: band 1.05 -> 1.15 (fresh Jupiter quote vs a slightly stale ref); still capped at ref
        exit_px, src = min(q["price"], ref * (1 - 0.0005)), "jupiter"
    elif q and ref and 0 < q["price"] < ref * 0.5 and math.isfinite(q["price"]) and \
            q.get("impact_pct") is not None and q["impact_pct"] <= CFG["crash_quote_max_impact_pct"]:
        # v10.1: a low-impact Jupiter route far below the ref means the ref is stale after a crash/rug (DCTC #218:
        # DexScreener -35%, Jupiter -63% at 0.5% impact). Take the worse, real price instead of the model.
        return q["price"], "jupiter_crash", model_px, q
    return sane_exit(exit_px, ref), src, model_px, q


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
    meta: Optional[dict] = None   # v8: entry features (ruleset, score bucket, market + filter values, strict_v7 verdict)

# ---------------------------------------------------------------- Logger (stage 6)
class Logger:
    def __init__(self):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")   # WAL+NORMAL: durable on checkpoint, no fsync per row
        self.db.execute("PRAGMA busy_timeout=8000")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.execute("PRAGMA cache_size=-8000")     # 8 MB page cache, modest on the 1 GB Always Free VM
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
        for tbl, col in (("positions", "ruleset TEXT"), ("positions", "entry_meta TEXT"), ("scores", "ruleset TEXT")):   # v8 tags
            try: self.db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
            except sqlite3.OperationalError: pass   # already there
        if self.db.execute("SELECT v FROM ledger WHERE k='cash'").fetchone() is None:
            self.db.execute("INSERT INTO ledger VALUES('cash',?)", (CFG["start_equity"],))
        # v10: the first start of a new RULESET restarts the paper ledger at start_equity (only with 0 open positions;
        # otherwise it retries on the next start). ledger 'epoch_ts' = when the current ledger started.
        self.reset = False
        if self.db.execute("SELECT 1 FROM ledger WHERE k=?", ("epoch_" + RULESET,)).fetchone() is None and \
                self.db.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0] == 0:
            self.db.execute("UPDATE ledger SET v=? WHERE k='cash'", (CFG["start_equity"],))
            self.db.execute("INSERT OR REPLACE INTO ledger VALUES('epoch_ts',?)", (now(),))
            self.db.execute("INSERT OR REPLACE INTO ledger VALUES(?,?)", ("epoch_" + RULESET, now()))
            self.reset = True
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
            self.db.execute("INSERT INTO scores(ts,ts_iso,cycle,mint,symbol,score,passed,components,ruleset) VALUES(?,?,?,?,?,?,?,?,?)",
                            (ts, iso(ts), self.cycle, mint, symbol, score, int(passed), json.dumps(components), RULESET))
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
        ttl = CFG.get("jup_list_ttl") or {"5m": 90, "1h": 180, "6h": 480, "24h": 600}
        self.jup_jobs = [(f"https://lite-api.jup.ag/tokens/v2/{cat}/{iv}?limit=100", "jup_" + cat, int(ttl.get(iv, 120)))
                         for cat in ("toptrending", "toptraded", "toporganicscore") for iv in ("5m", "1h", "6h", "24h")]
        self.jup_lists = {}; self.jup_ready = threading.Event()
        threading.Thread(target=self._gt_loop, daemon=True, name="gt-discovery").start()
        threading.Thread(target=self._jup_loop, daemon=True, name="jup-discovery").start()
        self.pp = PumpPortalShadow(log) if CFG.get("pumpportal_ws") else None

    def _jup_loop(self):
        """v3: Jupiter discovery lists refreshed in the background at the shared 'jup' pace, so the cycle never
        waits on the 0.5 RPS Jupiter window. Same URLs and TTLs as before (HTTP.get serves fresh cache hits)."""
        while True:
            try:
                for url, s, ttl in self.jup_jobs:
                    d = HTTP.get(url, cache_s=ttl, tries=3)
                    if d is not None: self.jup_lists[url] = (now(), d, ttl)
                self.jup_ready.set()
            except Exception as e:
                self.log.event("Scout", "JUP_ERROR", error=repr(e))
            time.sleep(10)

    def _gt_sweep(self):
        found = {}
        for path in self.GT_PATHS:   # sequential on purpose: GT public API is ~5-10 req/min
            wait_not_critical()
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
        results = HTTP.map(lambda j: HTTP.get(j[1], cache_s=j[3]), jobs)
        self.jup_ready.wait(timeout=45)   # only the very first cycle after a restart can wait here
        jobs += [("jup", url, s, ttl) for url, s, ttl in self.jup_jobs]
        # a list whose refresh keeps failing is dropped after 3x its TTL (as before: a failed list is simply missing)
        results += [(lambda h: h[1] if h and now() - h[0] < 3 * h[2] else None)(self.jup_lists.get(url)) for url, s, ttl in self.jup_jobs]
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
            elif age_h < CFG["min_age_h"]: reasons.append(f"pair too young: {age_h*60:.0f}m < {CFG['min_age_h']*60:.0f}m")
            elif age_h > CFG["max_age_h"]: reasons.append(f"pair too old: {age_h:.1f}h > {CFG['max_age_h']:.0f}h")
            if not mcap: reasons.append("data unavailable: market cap")
            elif mcap < CFG["min_mcap"]: reasons.append(f"mcap ${mcap:,.0f} < ${CFG['min_mcap'] / 1000:.0f}k")
            elif mcap > CFG["max_mcap"]: reasons.append(f"mcap ${mcap:,.0f} > ${CFG['max_mcap'] / 1e6:.0f}M")
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
        if self.pp:
            try: self.pp.measure(set(mints))
            except Exception as e: self.log.event("Shadow", "ERROR", rule="pp_discovery", error=repr(e))
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
        results = HTTP.rpc_batch(url, calls, tries=3, batch_size=10)   # v8.1: <= 10 per batch (Helius free: 10 RPS, a batch counts per call)
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
        elif liq < CFG["min_liq"]: reasons.append(f"liquidity ${liq:,.0f} < ${CFG['min_liq'] / 1000:.0f}k")
        if v1 is None: reasons.append("data unavailable: 1h volume")
        elif v1 < CFG["min_vol_h1"]: reasons.append(f"1h volume ${v1:,.0f} < ${CFG['min_vol_h1'] / 1000:.0f}k")
        if ch1 is None: reasons.append("data unavailable: 1h price change")
        elif ch1 > CFG["max_h1_change"]: reasons.append(f"already +{ch1:.0f}% in 1h (> +{CFG['max_h1_change']:.0f}%)")
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
            checks["top1_pct_ex_pools"] = round(max((float(h.get("pct") or 0) for h in real), default=0.0), 2)   # v5: logged for shadow rules only
            if top10 > CFG["max_top10_pct"]: reasons.append(f"top-10 holders {top10:.1f}% > {CFG['max_top10_pct']:.0f}%")
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
        elif dev_pct > CFG["max_dev_pct"]: reasons.append(f"dev holds {dev_pct:.1f}% > {CFG['max_dev_pct']:.0f}%")
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
        if bundle > CFG["max_bundle_pct"]: reasons.append(f"bundled/insider supply {bundle:.1f}% > {CFG['max_bundle_pct']:.0f}%")
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
    def __init__(self, log: Logger):
        self.log = log; self.smart = SWL.SmartBook(log.db, log.lock); self._smart_cache = {}; self.last_score = {}
        self.smart.url = helius_rpc_url(); self.smart.wait = wait_not_critical   # v5: Helius smart-wallet lookups
        self.smart.rpc = lambda url, calls: HTTP.rpc_batch(url, calls, tries=2, priority=True, batch_size=10)
        self.smart.guard = lambda: helius_total_today(log.db, log.lock) < (CFG.get("helius_total_daily_credits") or 24000)   # v8.1
    def run(self, fp: FilterPass) -> Optional[AnalystPass]:
        assert isinstance(fp, FilterPass)
        c = fp.cand; pc = c.price_change; tx = c.txns
        comp, notes = {}, {}
        # momentum: reward sustained positive trend, penalise dumps; h1 sweet spot 5-60%
        m5, h1, h6 = (pc.get("m5") or 0), (pc.get("h1") or 0), (pc.get("h6") or 0)
        s = 50 + 1.5 * clamp(m5, -15, 15) + 0.6 * clamp(h1, -40, 50) + 0.15 * clamp(h6, -100, 150)
        if m5 < -5 and h1 < 0: s -= 15
        comp["momentum"] = clamp(s); notes["momentum"] = dict(m5=m5, h1=h1, h6=h6)
        jd = HTTP.get(f"https://lite-api.jup.ag/tokens/v2/search?query={c.mint}", cache_s=CFG.get("jup_search_cache_s") or 45, priority=True) or []
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
        W0 = CFG["weights"]; bw0 = sum(w for k, w in W0.items() if k != "smart_wallets" and comp.get(k) is not None and w > 0)
        base0 = (sum(comp[k] * w for k, w in W0.items() if k != "smart_wallets" and comp.get(k) is not None and w > 0) / bw0) if bw0 else 0.0
        hopeless = bool(CFG.get("helius_smart_skip_hopeless")) and bw0 and base0 + W0["smart_wallets"] * (100.0 - base0) < CFG["pass_score"]
        hit = self._smart_cache.get(c.mint)
        if hopeless:   # v8.1: even 3+ smart wallets could not reach pass_score -> lookup cannot change the decision; save Helius credits
            sv, sinfo = None, dict(status=f"skipped: base {base0:.1f} cannot reach {CFG['pass_score']:.0f} even with 3+ smart wallets (Helius saved)")
        elif hit and now() - hit[0] < (CFG.get("smart_cache_s") or 0):   # v8 perf: same lookup reused for a few cycles
            sv, sinfo = hit[1], dict(hit[2], cached_s=round(now() - hit[0]))
        else:
            try: sv, sinfo = self.smart.factor(lambda u, **kw: HTTP.get(u, tries=2, priority=True, **kw), c.mint, c.pair_address, c.price_usd)
            except Exception as e: sv, sinfo = None, dict(status=f"error {e!r}: factor skipped, weights renormalised")
            if sv is not None:
                self._smart_cache[c.mint] = (now(), sv, sinfo if isinstance(sinfo, dict) else {})
                if len(self._smart_cache) > 500:
                    for k in [k for k, v in self._smart_cache.items() if now() - v[0] > (CFG.get("smart_cache_s") or 0)]: self._smart_cache.pop(k, None)
        comp["smart_wallets"] = sv; notes["smart_wallets"] = sinfo
        W = CFG["weights"]; tw = sum(w for k, w in W.items() if comp.get(k) is not None and w > 0)
        score_old = round(sum(comp[k] * w for k, w in W.items() if comp.get(k) is not None and w > 0) / tw, 1)   # pre-v6 formula
        # v6 (user-approved): the smart factor can only ADD. Base = the other 5 factors renormalised; each smart reading
        # closes 15% x (smart/100) of the gap to 100: 0 wallets = base (no penalty), 1 -> +6.75% of the gap, 2 -> +11.25%,
        # 3+ -> +15% (= exactly the old score for 3+ wallets). Never above 100.
        bw = sum(w for k, w in W.items() if k != "smart_wallets" and comp.get(k) is not None and w > 0)
        base = (sum(comp[k] * w for k, w in W.items() if k != "smart_wallets" and comp.get(k) is not None and w > 0) / bw) if bw else 0.0
        sv_ = comp.get("smart_wallets")
        score = round(base + (W["smart_wallets"] * (sv_ / 100.0) * (100.0 - base) if sv_ is not None else 0.0), 1) if bw else score_old
        components = {k: (None if v is None else round(v, 1)) for k, v in comp.items()}
        components["_weights_effective"] = {k: round(w / bw, 3) for k, w in W.items() if w > 0 and k != "smart_wallets"} if bw else {}
        components["_smart_rule"] = "v6: base(5 factors) + 0.15 * smart/100 * (100 - base)"
        components["_score_old"] = score_old
        components["_inputs"] = notes
        passed = score >= CFG["pass_score"]
        self.last_score[c.mint] = score
        if len(self.last_score) > 2000: self.last_score.clear()
        self.log.score(c.mint, c.symbol, score, passed, components)
        if not passed:
            self.log.kill("Analyst", c.mint, c.symbol, c.pair_address, [f"score {score} < {CFG['pass_score']:.0f}"],
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
    def _entry_meta(self, ap, liq, ref):
        try:
            c = ap.fp.cand; ck = ap.fp.checks or {}; sr = strict_v7_reasons(c, ck, ap.score)
            return dict(ruleset=RULESET, score=ap.score, score_bucket=score_bucket(ap.score), score_old=ap.components.get("_score_old"),
                        components={k: v for k, v in ap.components.items() if not k.startswith("_")},
                        smart_count=((ap.components.get("_inputs") or {}).get("smart_wallets") or {}).get("count") if isinstance((ap.components.get("_inputs") or {}).get("smart_wallets"), dict) else None,
                        liq_usd=liq, scout_liq_usd=c.liq_usd, mcap=c.mcap, age_h=c.age_h, vol_h1=(c.vol or {}).get("h1"), vol_h24=(c.vol or {}).get("h24"),
                        chg_m5=(c.price_change or {}).get("m5"), chg_h1=(c.price_change or {}).get("h1"), chg_h6=(c.price_change or {}).get("h6"),
                        top10=ck.get("top10_pct_ex_pools"), top1=ck.get("top1_pct_ex_pools"), dev_pct=ck.get("dev_pct"), bundle=ck.get("bundle_pct_proxy"),
                        lp_primary=ck.get("lp_primary_locked_pct"), lp_weighted=ck.get("lp_weighted_locked_pct"),
                        organic=(ap.components.get("_inputs") or {}).get("jupiter_organic_score"), dex=c.dex, sources=list(c.sources),
                        strict_v7_pass=not sr, strict_v7_reasons=sr)
        except Exception as e:
            return dict(ruleset=RULESET, score=ap.score, error=repr(e)[:120])
    def run(self, ap: AnalystPass) -> Optional[PaperOrder]:
        assert isinstance(ap, AnalystPass)
        c = ap.fp.cand
        if os.path.exists(PAUSE_PATH):   # v8.1: operator pause (e.g. before a deploy). Exits keep running.
            self.log.kill("Executor", c.mint, c.symbol, c.pair_address, ["entries paused (PAUSE_ENTRIES file)"], {}); return None
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
            size = min(CFG["max_pos_frac"] * eq, cash, CFG["max_liq_frac"] * liq)   # v6: also <= 0.25% of pool liquidity
            if size < CFG["min_size_usd"]:
                self.log.kill("Executor", c.mint, c.symbol, c.pair_address, ["insufficient paper cash"], dict(cash=cash)); return None
            slip, _ = model_slip(size, liq)   # v8.2: capped model (liq here is the fresh DexScreener value, size <= 0.25% of it)
            model_fill = ref * (1 + slip); fill = model_fill; fill_src = "model"
            q = JUP.buy_price(c.mint, size)   # real Jupiter quote for the exact size (quote only, nothing signed)
            if q and 0.9 <= q["price"] / ref <= 1.15:   # sanity band vs fresh DexScreener price; else keep model
                fill, fill_src = max(q["price"], ref * (1 + 0.0005)), "jupiter"
                slip = fill / ref - 1
            self.log.event("Executor", "FILL_QUOTE", c.mint, c.symbol, side="buy", size_usd=round(size, 2), ref_price=ref,
                           modeled_slip_pct=round(100 * (model_fill / ref - 1), 3),
                           quoted_slip_pct=(round(100 * (q["price"] / ref - 1), 3) if q else None),
                           jup_impact_pct=(q or {}).get("impact_pct"), route=(q or {}).get("route"), used=fill_src)
            if not (fill > 0 and math.isfinite(fill)) or fill > ref * (1 + MAX_MODEL_SLIP):   # v8.2 entry fill guard
                self.log.kill("Executor", c.mint, c.symbol, c.pair_address, ["insane entry fill (guard)"], dict(ref=ref, fill=fill)); return None
            order = PaperOrder(mint=c.mint, symbol=c.symbol, pair_address=c.pair_address, ref_price=ref, fill_price=fill,
                               size_usd=round(size, 2), qty=size / fill, slippage_pct=round(slip * 100, 3),
                               stop_price=ref * (1 - CFG["stop_pct"]), score=ap.score, equity_at_entry=round(eq, 2), created_at=now(),
                               meta=self._entry_meta(ap, liq, ref))
            self.risk.open(order)   # stop is attached atomically with the order inside Risk.open
            return order

# ---------------------------------------------------------------- Risk (stage 5)
class Risk:
    def __init__(self, log: Logger):
        self.log = log
        self._shadow_booted = False
        self._v5 = {r: dict(w=set(), x=set(), c=set()) for r in SHADOW_EXIT_V5}; self._opened = {}; self._refs = {}
        self._cw = self._cp = self._cx = self._cc = set()
        self._plocks, self._plocks_lock = {}, threading.Lock()
        self._last_write, self._src_state, self._pollb, self._post, self._ws_seen = {}, {}, {}, {}, {}
        self.feed = None; self._eliq = {}

    def open(self, o: PaperOrder):
        assert isinstance(o, PaperOrder) and o.stop_price < o.fill_price
        db = self.log.db
        with self.log.lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""INSERT INTO positions(mint,symbol,pair,status,opened_ts,opened_iso,ref_price,entry_price,size_usd,qty,entry_slip_pct,
                    initial_stop,stop_price,peak_price,trail_active,last_price,last_ts,score,ruleset,entry_meta) VALUES(?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                    (o.mint, o.symbol, o.pair_address, o.created_at, iso(o.created_at), o.ref_price, o.fill_price, o.size_usd, o.qty,
                     o.slippage_pct, o.stop_price, o.stop_price, o.ref_price, o.ref_price, now(), o.score, RULESET,
                     json.dumps(o.meta, default=str) if o.meta else None))   # v6: peak starts at the market price
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
        self._cw, self._cp, self._cx, self._cc = (self._shadow_ids("combo_tp_trail4", "WATCH"), self._shadow_ids("combo_tp_trail4", "PARTIAL"),
                                                  self._shadow_ids("combo_tp_trail4", "SHADOW_EXIT"), self._shadow_ids("combo_tp_trail4", "COMPARE"))
        self._v5 = {r: dict(w=self._shadow_ids(r, "WATCH"), x=self._shadow_ids(r, "SHADOW_EXIT"), c=self._shadow_ids(r, "COMPARE"))
                    for r in SHADOW_EXIT_V5}
        self._shadow_booted = True

    def _shadow_fill_payload(self, ref, exit_px, src, model_px, q, qty):
        return dict(ref_price=ref, exit_price=exit_px, fill_src=src,
                    modeled_slip_pct=(round(100 * (1 - model_px / ref), 3) if ref else None),
                    quoted_slip_pct=(round(100 * (1 - q["price"] / ref), 3) if q and ref else None),
                    jup_impact_pct=(q or {}).get("impact_pct"), route=(q or {}).get("route"), qty=qty)

    def shadow_open_tick(self, pid, mint, sym, ep, qty, size, px, liq, peak, reuse=None):
        """Track shadow stops for one open position on every Risk poll. Writes WATCH / PARTIAL / SHADOW_EXIT only."""
        if not ep or not qty or not px: return
        if not liq or liq < MIN_MODEL_LIQ: liq = self._entry_liq(pid) or liq   # v8.2: same liquidity fallback as the live close
        try: self.shadow_v5_tick(pid, mint, sym, ep, qty, size, px, liq, peak, reuse)
        except Exception as e: self.log.event("Shadow", "ERROR", mint=mint, symbol=sym, rule="v5_tick", error=repr(e))
        act_lvl = (self._ref(pid) or ep) * (1 + SHADOW_TRAIL4["activate"])   # v6: same market reference as the live trail
        trail_on = peak >= act_lvl
        shadow_stop = peak * (1 - SHADOW_TRAIL4["dist"]) if trail_on else None   # v10: trail_4pct is live; combo below still uses it

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

    def _opened_ts(self, pid):
        if pid not in self._opened:
            with self.log.lock:
                r = self.log.db.execute("SELECT opened_ts FROM positions WHERE id=?", (pid,)).fetchone()
            self._opened[pid] = r[0] if r else now()
        return self._opened[pid]
    def shadow_v5_tick(self, pid, mint, sym, ep, qty, size, px, liq, peak, reuse=None):
        """v5 SHADOW exits (log only): time stop after 60 min without +3%, breakeven (+1%) once the peak passed +8%."""
        for rule, prm in SHADOW_EXIT_V5.items():
            st = self._v5[rule]
            if pid not in st["w"]:
                self._sev(rule, "WATCH", mint, sym, pid, entry_price=ep, peak=peak, **prm); st["w"].add(pid)
            if pid in st["x"]: continue
            if rule == "old_rules_v5":   # pre-v6 exits: -6% from the fill, trail from fill*1.12, 6% under the peak
                old_stop = ep * (1 - prm["stop"])
                if peak >= ep * (1 + prm["activate"]): old_stop = max(old_stop, peak * (1 - prm["dist"]))
                hit = px <= old_stop; why = dict(old_stop=old_stop)
            elif rule == "time_stop_60m":
                age_min = (now() - self._opened_ts(pid)) / 60
                hit = age_min >= prm["minutes"] and px < ep * (1 + prm["min_gain"]); why = dict(age_min=round(age_min, 1))
            else:
                hit = peak >= ep * (1 + prm["arm"]) and px <= ep * (1 + prm["lock_gain"]); why = dict(peak_gain_pct=round(100 * (peak / ep - 1), 2))
            if hit:
                exit_px, src, model_px, q = paper_sell_fill(mint, qty, px, liq, reuse)
                pnl = exit_px * qty - size
                self._sev(rule, "SHADOW_EXIT", mint, sym, pid, entry_price=ep, peak=peak, size_usd=size, hyp_pnl_usd=round(pnl, 2),
                          hyp_pnl_pct=round(100 * pnl / size, 2) if size else None, **why,
                          **self._shadow_fill_payload(px, exit_px, src, model_px, q, qty))
                st["x"].add(pid)
    def shadow_v5_close(self, pid, mint, sym, actual_pnl, actual_pct, reason, exit_px):
        for rule in SHADOW_EXIT_V5:
            st = self._v5[rule]
            if pid not in st["w"] or pid in st["c"]: continue
            hyp = self._shadow_row(rule, "SHADOW_EXIT", pid)
            self._sev(rule, "COMPARE", mint, sym, pid, triggered=bool(hyp),
                      actual_pnl_usd=round(actual_pnl or 0, 2), actual_pnl_pct=round(actual_pct or 0, 2), actual_reason=reason,
                      actual_exit_price=exit_px,
                      shadow_pnl_usd=(hyp or {}).get("hyp_pnl_usd", round(actual_pnl or 0, 2)),
                      shadow_pnl_pct=(hyp or {}).get("hyp_pnl_pct", round(actual_pct or 0, 2)),
                      shadow_exit_iso=(hyp or {}).get("_ts_iso"), shadow_exit_price=(hyp or {}).get("exit_price"))
            st["c"].add(pid)
    def shadow_compare_close(self, pid, mint, sym, qty, size, actual_pnl, actual_pct, reason, exit_px):
        """Once the live paper position closes, record actual vs shadow PnL for the v5 exits and the combo variant."""
        try: self.shadow_v5_close(pid, mint, sym, actual_pnl, actual_pct, reason, exit_px)
        except Exception as e: self.log.event("Shadow", "ERROR", mint=mint, symbol=sym, rule="v5_close", error=repr(e))
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
            if (pid in self._cw and pid not in self._cc) or \
               any(pid in st["w"] and pid not in st["c"] for st in self._v5.values()):
                self.shadow_compare_close(pid, mint, sym, qty, size, pnl, pct, reason, xp)

    # ---- v4: live exit evaluation shared by the 30 s DexScreener poll and the Helius real-time feed.
    # v6 levels: stop -10% from the market price at entry, trail activates at +12% over it, 6% under the peak. Only the price
    # source / timing differs: Helius pool reserves when the feed is healthy, else the DexScreener poll.
    def _entry_liq(self, pid):
        """v8.2: pool liquidity recorded at entry (fallback when the live feed has no liquidity reading yet)."""
        if pid not in self._eliq:
            try:
                with self.log.lock:
                    r = self.log.db.execute("SELECT entry_meta FROM positions WHERE id=?", (pid,)).fetchone()
                m = json.loads(r[0]) if r and r[0] else {}
                self._eliq[pid] = float(m.get("liq_usd") or m.get("scout_liq_usd") or 0) or None
            except Exception: self._eliq[pid] = None
        return self._eliq[pid]
    def _plock(self, pid):
        with self._plocks_lock: return self._plocks.setdefault(pid, threading.Lock())
    def _open_row(self, pid):
        with self.log.lock:
            return self.log.db.execute("SELECT id,mint,symbol,pair,entry_price,qty,size_usd,stop_price,peak_price,trail_active,initial_stop "
                                       "FROM positions WHERE id=? AND status='OPEN'", (pid,)).fetchone()
    def _ref(self, pid):
        """Market (DexScreener) price at entry: v6 reference for the stop and the trailing activation."""
        if pid not in self._refs:
            with self.log.lock:
                r = self.log.db.execute("SELECT ref_price,entry_price FROM positions WHERE id=?", (pid,)).fetchone()
            self._refs[pid] = (r[0] or r[1]) if r else None
        return self._refs[pid]
    def apply_price(self, pid, px, liq, src, trig=None, force=None):
        """Evaluate the live rules for one OPEN position at price `px` from `src`. Returns True if it closed it.
        `force` = exit reason that closes at `px` regardless of the stop (v10: smart_exit)."""
        with self._plock(pid):
            r = self._open_row(pid)
            if not r or not px: return False
            pid, mint, sym, pair, ep, qty, size, stop, peak, trail, istop = r
            new_peak = max(peak, px)
            new_trail = bool(trail) or new_peak >= (self._ref(pid) or ep) * (1 + CFG["trail_activate"])
            new_stop = stop
            if new_trail: new_stop = max(stop, new_peak * (1 - CFG["trail_dist"]))
            be = new_peak >= ep * (1 + CFG["be_arm"])   # v10 breakeven: from the fill, like the old breakeven_8 shadow
            if be: new_stop = max(new_stop, ep * (1 + CFG["be_lock"]))
            if new_trail and not trail:
                self.log.event("Risk", "TRAIL_ACTIVATED", mint, sym, peak=new_peak, entry=ep, stop=new_stop, price_src=src)
            if px <= new_stop or force:
                # live fill first (unchanged model), then shadow can reuse it when the size matches
                exit_px, fill_src, model_px, q = paper_sell_fill(mint, qty, px, liq, liq_fallback=self._entry_liq(pid))
                live_reuse = dict(ref=px, qty=qty, exit_px=exit_px, src=fill_src, model_px=model_px, q=q)
                self.log.event("Risk", "FILL_QUOTE", mint, sym, side="sell", qty=qty, ref_price=px,
                               modeled_slip_pct=round(100 * (1 - model_px / px), 3),
                               quoted_slip_pct=(round(100 * (1 - q["price"] / px), 3) if q else None),
                               jup_impact_pct=(q or {}).get("impact_pct"), route=(q or {}).get("route"), used=fill_src)
                proceeds = max(0.0, exit_px * qty); pnl = max(proceeds - size, -size)   # v8.2 hard guard: never below -100% of size
                reason = force or ("trailing stop" if new_trail and new_stop > ep * (1 + CFG["be_lock"]) else
                                   f"breakeven +{100 * CFG['be_lock']:.0f}%" if be else f"stop-loss -{100 * CFG['stop_pct']:.0f}%")
                with self.log.lock:
                    db = self.log.db; db.execute("BEGIN IMMEDIATE")
                    try:   # same all-or-nothing pattern as Risk.open: a failed write must never leave the shared connection mid-transaction
                        db.execute("""UPDATE positions SET status='CLOSED',last_price=?,last_ts=?,peak_price=?,stop_price=?,trail_active=?,
                            closed_ts=?,closed_iso=?,exit_ref=?,exit_price=?,exit_reason=?,pnl_usd=?,pnl_pct=? WHERE id=? AND status='OPEN'""",
                            (px, now(), new_peak, new_stop, int(new_trail), now(), iso(), px, exit_px, reason, pnl, 100 * pnl / size, pid))
                        db.execute("UPDATE ledger SET v=v+? WHERE k='cash'", (proceeds,)); db.execute("COMMIT")
                    except Exception:
                        db.execute("ROLLBACK"); raise
                self.log.event("Risk", "EXIT", mint, sym, reason=reason, ref_price=px, exit_price=exit_px, entry=ep,
                               pnl_usd=round(pnl, 2), pnl_pct=round(100 * pnl / size, 2), peak=new_peak, stop=new_stop,
                               trigger_src=src, trigger_px=px, fill_vs_trigger_pct=round(100 * (exit_px / px - 1), 3),
                               trigger_vs_stop_pct=round(100 * (px / new_stop - 1), 3), **(trig or {}))
                self._post[pid] = dict(mint=mint, sym=sym, pair=pair, stop=new_stop, exit_ts=now(), exit_ref=px, exit_px=exit_px,
                                       src=src, until=now() + (CFG.get("ws_poll_post_watch_s") or 300),
                                       poll_breach=self._pollb.pop(pid, None))
                try:
                    self.shadow_open_tick(pid, mint, sym, ep, qty, size, px, liq, new_peak, reuse=live_reuse)
                    self.shadow_compare_close(pid, mint, sym, qty, size, pnl, 100 * pnl / size, reason, exit_px)
                except Exception as e:
                    self.log.event("Shadow", "ERROR", mint=mint, symbol=sym, error=repr(e), tb=traceback.format_exc()[-600:])
                return True
            if src == "dexscreener_poll":   # shadow exit rules keep their 30 s DexScreener cadence, as before
                try:
                    self.shadow_open_tick(pid, mint, sym, ep, qty, size, px, liq, new_peak)
                except Exception as e:
                    self.log.event("Shadow", "ERROR", mint=mint, symbol=sym, error=repr(e), tb=traceback.format_exc()[-600:])
            changed = new_peak != peak or new_stop != stop or bool(new_trail) != bool(trail)
            if changed or src == "dexscreener_poll" or now() - self._last_write.get(pid, 0) >= 5:
                self._last_write[pid] = now()
                with self.log.lock:
                    self.log.db.execute("UPDATE positions SET last_price=?,last_ts=?,peak_price=?,stop_price=?,trail_active=? WHERE id=? AND status='OPEN'",
                                        (px, now(), new_peak, new_stop, int(new_trail), pid))
            return False

    def _source(self, pid, mint, sym, ok, why):
        """Log every switch between the Helius live trigger and the DexScreener fallback."""
        prev = self._src_state.get(pid)
        cur = "helius_ws" if ok else "dexscreener_poll"
        if prev != cur:
            self._src_state[pid] = cur
            self.log.event("Risk", "PRICE_SOURCE", mint, sym, position_id=pid, source=cur, previous=prev, reason=why)

    def _poll_shadow(self, pid, mint, sym, stop, px):
        """'poll_stop' shadow: first time the old 30 s DexScreener poll would have triggered, while Helius is live."""
        if pid not in self._pollb and stop and px <= stop:
            self._pollb[pid] = dict(ts=now(), px=px, stop=stop)
            self._sev("poll_stop", "BREACH", mint, sym, pid, cycle=self.log.cycle, ds_px=px, stop=stop,
                      note="shadow: the old DexScreener poll would have exited here; live exit trigger is Helius")

    def _post_watch(self, prices):
        """After a Helius-triggered exit, keep watching the DexScreener price for a few minutes to measure when
        the old poll path would have exited (poll_stop COMPARE)."""
        for pid, w in list(self._post.items()):
            b = w.get("poll_breach")
            px = (prices.get(w["pair"]) or (None, None))[0]
            if not b and px and px <= w["stop"]: b = dict(ts=now(), px=px, stop=w["stop"])
            if b or now() > w["until"] or w["src"] != "helius_ws":
                self._post.pop(pid, None)
                if w["src"] != "helius_ws": continue
                self._sev("poll_stop", "COMPARE", w["mint"], w["sym"], pid, cycle=self.log.cycle, live_src=w["src"],
                          live_exit_ts=iso(w["exit_ts"]), live_trigger_px=w["exit_ref"], live_fill_px=w["exit_px"], stop=w["stop"],
                          poll_breach_ts=(iso(b["ts"]) if b else None), poll_px=(b["px"] if b else None),
                          poll_seconds_later=(round(b["ts"] - w["exit_ts"], 1) if b else None),
                          poll_px_vs_live_trigger_pct=(round(100 * (b["px"] / w["exit_ref"] - 1), 3) if b else None),
                          last_ds_px=px, note=None if b else f"poll had not crossed the stop within {CFG.get('ws_poll_post_watch_s') or 300}s")

    def poll(self):
        with HTTP.prefer_live():
            try:
                self._shadow_boot(); self.shadow_reconcile()
            except Exception as e:
                self.log.event("Shadow", "ERROR", error=repr(e), tb=traceback.format_exc()[-600:])
            with self.log.lock:
              rows = self.log.db.execute("SELECT id,mint,symbol,pair,entry_price,qty,size_usd,stop_price,peak_price,trail_active,initial_stop FROM positions WHERE status='OPEN'").fetchall()
            if not rows and not self._post: return
            prices = {}
            pairs = list({r[3] for r in rows} | {w["pair"] for w in self._post.values()})
            for i in range(0, len(pairs), 30):
                d = HTTP.get("https://api.dexscreener.com/latest/dex/pairs/solana/" + ",".join(pairs[i:i+30])) or {}
                for p in d.get("pairs") or []:
                    prices[p["pairAddress"]] = (float(p.get("priceUsd") or 0), (p.get("liquidity") or {}).get("usd") or 0)
            try: self._post_watch(prices)
            except Exception as e: self.log.event("Shadow", "ERROR", rule="poll_stop", error=repr(e))
            feed = getattr(self, "feed", None)
            smart = set()
            if CFG["smart_exit_live"]:   # v10: smartwallets.py logs the first sell by a tracked smart wallet; Risk closes on it
                with self.log.lock:
                    smart = {r[0] for r in self.log.db.execute(   # shadow_pos index: (position_id, kind)
                        f"SELECT position_id FROM shadow_events WHERE position_id IN ({','.join('?' * len(rows))}) AND kind='SHADOW_EXIT' AND rule='smart_exit'",
                        [r[0] for r in rows])} if rows else set()
            for pid, mint, sym, pair, ep, qty, size, stop, peak, trail, istop in rows:
                if pair not in prices or not prices[pair][0]:
                    self.log.event("Risk", "PRICE_MISSING", mint, sym, pair=pair); continue
                px, liq = prices[pair]
                if pid in smart and self.apply_price(pid, px, liq, "dexscreener_poll", force="smart_exit"): continue
                if feed: feed.note_ds(pid, px, liq)
                ok, why = feed.live_ok(pid) if feed else (False, "helius feed disabled")
                self._source(pid, mint, sym, ok, why)
                if ok:   # Helius is the live trigger for this position: the poll only feeds shadows and sanity checks
                    self._poll_shadow(pid, mint, sym, stop, px)
                    try: self.shadow_open_tick(pid, mint, sym, ep, qty, size, px, liq, peak)
                    except Exception as e:
                        self.log.event("Shadow", "ERROR", mint=mint, symbol=sym, error=repr(e), tb=traceback.format_exc()[-600:])
                    continue
                self.apply_price(pid, px, liq, "dexscreener_poll")

    def ws_tick(self):
        """Called every ~0.25 s by the Desk: apply fresh Helius reserve prices to the live rules."""
        feed = getattr(self, "feed", None)
        if not feed: return
        for pid, (px, ts, meta) in feed.prices().items():
            seen = self._ws_seen.get(pid, (0, 0))
            if ts <= seen[0] and now() - seen[1] < 2: continue   # new reserve price, or a 2 s re-check of the current one
            self._ws_seen[pid] = (ts, now())
            ok, why = feed.live_ok(pid)
            st = feed.pos.get(pid) or {}
            self._source(pid, st.get("mint"), st.get("sym"), ok, why)
            if ok:
                self.apply_price(pid, px, feed.liq(pid), "helius_ws", trig=meta)

# ---------------------------------------------------------------- v3 shadow-only observers (never trade)
class FastStopShadow:
    """SHADOW ONLY. Polls Jupiter price/v3 (one batched call, <=50 mints) for OPEN positions every
    fast_stop_poll_s and logs when the price first crosses the position's *current live* stop. On close it
    logs how much earlier / at what price a faster feed would have seen the breach versus the live
    30 s DexScreener poll. Reads positions; never writes them."""
    def __init__(self, log):
        self.log = log; self.seen = {}   # pid -> dict(first breach)
        self.last = {}   # v4: pid -> (jupiter px, ts), used by HeliusFeed as a sanity reference
    def _ev(self, kind, mint, sym, pid, **d):
        SWL.shadow_event(self.log.db, self.log.lock, self.log.jsonl, "fast_stop", kind, mint, sym, pid, cycle=self.log.cycle, **d)
    def tick(self):
        with self.log.lock:
            rows = self.log.db.execute("SELECT id,mint,symbol,stop_price,last_price,entry_price FROM positions WHERE status='OPEN'").fetchall()
        open_ids = {r[0] for r in rows}
        for pid in [p for p in self.seen if p not in open_ids]:   # closed since last tick -> compare once
            b = self.seen.pop(pid)
            with self.log.lock:
                r = self.log.db.execute("SELECT closed_ts,exit_ref,exit_reason,pnl_pct FROM positions WHERE id=?", (pid,)).fetchone()
            if r and b.get("breach_ts"):
                self._ev("COMPARE", b["mint"], b["sym"], pid, breach_ts=iso(b["breach_ts"]), breach_px=b["breach_px"],
                         stop=b["stop"], live_exit_ts=iso(r[0]) if r[0] else None, live_exit_ref=r[1], live_reason=r[2],
                         live_pnl_pct=r[3], seconds_earlier=(round(r[0] - b["breach_ts"], 1) if r[0] else None),
                         px_vs_live_ref_pct=(round(100 * (b["breach_px"] / r[1] - 1), 3) if r[1] else None))
        if not rows: return
        d = HTTP.get("https://lite-api.jup.ag/price/v3?ids=" + ",".join(sorted({r[1] for r in rows})), tries=1) or {}
        for pid, mint, sym, stop, last, ep in rows:
            px = ((d.get(mint) or {}).get("usdPrice")) if isinstance(d, dict) else None
            st = self.seen.setdefault(pid, dict(mint=mint, sym=sym, breach_ts=None, n=0, dev=[]))
            if not px: continue
            px = float(px); st["n"] += 1; self.last[pid] = (px, now())
            if last: st["dev"] = (st["dev"] + [round(100 * (px / last - 1), 2)])[-20:]   # Jupiter vs DexScreener mark
            if st["breach_ts"] is None and stop and px <= stop:
                st.update(breach_ts=now(), breach_px=px, stop=stop)
                self._ev("BREACH", mint, sym, pid, jup_px=px, stop=stop, ds_last=last, entry=ep,
                         jup_vs_ds_pct_recent=st["dev"][-5:], note="log only: Jupiter price/v3 every 10s (live trigger: Helius reserves when healthy, else 30s DexScreener poll)")
    def loop(self):
        while True:
            try: self.tick()
            except Exception as e: self.log.event("Shadow", "ERROR", rule="fast_stop", error=repr(e))
            time.sleep(CFG.get("fast_stop_poll_s") or 10)

class PumpPortalShadow:
    """SHADOW ONLY. Free PumpPortal WebSocket (wss://pumpportal.fun/api/data, subscribeMigration, no key):
    pump.fun -> PumpSwap migrations are stored in pp_migrations. Each Scout cycle logs how many recent
    migrated mints the current discovery set does NOT already see, and (one DexScreener batch) how many of
    those would sit inside the Scout window. Nothing is added to the live candidate list."""
    URL = "wss://pumpportal.fun/api/data"
    def __init__(self, log):
        self.log = log; self.connected = False; self.msgs = 0; self.last_measure = 0.0
        with log.lock:
            log.db.execute("CREATE TABLE IF NOT EXISTS pp_migrations(mint TEXT PRIMARY KEY, ts REAL, pool TEXT, sig TEXT)")
    def loop(self):
        import wsmini
        backoff = 5
        while True:
            ws = None
            try:
                ws = wsmini.WS(self.URL, timeout=120)
                ws.send({"method": "subscribeMigration"})
                self.connected = True; backoff = 5
                while True:
                    m = json.loads(ws.recv()); self.msgs += 1
                    if m.get("txType") == "migrate" and m.get("mint"):
                        with self.log.lock:
                            self.log.db.execute("INSERT OR IGNORE INTO pp_migrations(mint,ts,pool,sig) VALUES(?,?,?,?)",
                                                (m["mint"], now(), m.get("pool"), m.get("signature")))
            except Exception as e:
                self.connected = False
                self.log.event("Shadow", "PP_WS_RECONNECT", rule="pp_discovery", error=repr(e)[:200], backoff_s=backoff)
            finally:
                if ws: ws.close()
            time.sleep(backoff); backoff = min(backoff * 2, 600)   # one connection only; never hammer (ban risk)
    def measure(self, discovered):
        if now() - self.last_measure < 600: return   # every 10 min is plenty for a shadow statistic
        self.last_measure = now()
        lo, hi = now() - CFG["max_age_h"] * 3600, now() - CFG["min_age_h"] * 3600
        with self.log.lock:
            rows = [r[0] for r in self.log.db.execute("SELECT mint FROM pp_migrations WHERE ts>? ORDER BY ts DESC", (now() - 86400,))]
        unseen = [m for m in rows if m not in discovered]
        in_window = []
        if unseen:
            d = HTTP.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(unseen[:30])) or []
            best = {}
            for p in d if isinstance(d, list) else []:
                m = (p.get("baseToken") or {}).get("address")
                if m and ((p.get("liquidity") or {}).get("usd") or 0) >= ((best.get(m, {}).get("liquidity") or {}).get("usd") or 0): best[m] = p
            for m, p in best.items():
                c, mc = (p.get("pairCreatedAt") or 0) / 1000, p.get("marketCap") or p.get("fdv") or 0
                if c and lo <= c <= hi and CFG["min_mcap"] <= mc <= CFG["max_mcap"]:
                    in_window.append((p["baseToken"].get("symbol"), round(mc)))
        SWL.shadow_event(self.log.db, self.log.lock, self.log.jsonl, "pp_discovery", "MEASURE", None, None, None,
                         cycle=self.log.cycle, ws_connected=self.connected, ws_msgs=self.msgs, migrations_24h=len(rows),
                         unseen_by_scout=len(unseen), unseen_checked=min(30, len(unseen)), unseen_in_scout_window=len(in_window),
                         sample=in_window[:8], note="log only: not added to live discovery")

class HeliusFeed:
    """v4 LIVE price source for OPEN positions (paper). One Helius standard WebSocket (free plan, max 5
    connections; we use 1). Subscribes (accountSubscribe, base64 + dataSlice of the 8-byte amount) to the two
    token vaults of each position's pool and prices the token from the reserves: (quote/10^dq)/(base/10^db) x quote USD.
    Supported pool programs: PumpSwap, Raydium CPMM, Raydium AMM v4 (constant product). Anything else, a
    stale/silent socket, a missing fresh reference price, a >ws_max_dev_pct deviation from DexScreener/Jupiter,
    an RPC-resync mismatch or the daily credit budget => live_ok() is False and Risk falls back to the poll."""
    LAYOUTS = {   # program id -> (name, vault0 off, vault1 off, mint0 off, mint1 off); validated on live pools 2026-11-17
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": ("pumpswap", 139, 171, 43, 75),
        "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": ("raydium_cpmm", 72, 104, 168, 200),
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": ("raydium_v4", 336, 368, 400, 432),
    }
    QUOTES = {SOL_MINT: "sol", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "usd", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "usd"}
    def __init__(self, log, fast):
        self.log, self.fast = log, fast
        self.pos, self.subs, self.req = {}, {}, {}
        self.lock = threading.RLock()
        self.ws = None; self.connected = False; self.next_id = 100
        self.sol = (None, 0.0); self.connects = 0; self.rpc_calls = 0; self.bytes_seen = 0; self._last_rx_bytes = 0
        self.day = time.strftime("%Y-%m-%d"); self.over_budget = False
        with log.lock:
            log.db.execute("CREATE TABLE IF NOT EXISTS helius_ws_usage(day TEXT PRIMARY KEY, bytes INT, rpc_calls INT, connects INT, credits_est REAL)")
            r = log.db.execute("SELECT bytes,rpc_calls,connects FROM helius_ws_usage WHERE day=?", (self.day,)).fetchone()
        self.base_bytes, self.base_rpc, self.base_conn = (r or (0, 0, 0))
    # ---- accounting (2 credits / 0.1 MB streamed, 1 credit per connection, 1 credit per RPC call)
    def credits_today(self):
        b = self.base_bytes + self.bytes_seen
        return round(b / 100000 * 2 + self.base_conn + self.connects + self.base_rpc + self.rpc_calls, 1)
    def _account(self, flush=False):
        if self.ws:
            d = self.ws.rx_bytes - self._last_rx_bytes
            if d > 0: self.bytes_seen += d; self._last_rx_bytes = self.ws.rx_bytes
        day = time.strftime("%Y-%m-%d")
        if day != self.day:
            self._flush(); self.day = day; self.base_bytes = self.base_rpc = self.base_conn = 0
            self.bytes_seen = self.rpc_calls = self.connects = 0; self.over_budget = False
        if self.credits_today() > (CFG.get("helius_ws_daily_credits") or 6000) and not self.over_budget:
            self.over_budget = True
            self.log.event("Risk", "WS_BUDGET", credits_est=self.credits_today(), note="daily Helius WS budget reached: poll fallback until tomorrow")
        if flush: self._flush()
    def _flush(self):
        with self.log.lock:
            self.log.db.execute("INSERT INTO helius_ws_usage(day,bytes,rpc_calls,connects,credits_est) VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
                                "bytes=excluded.bytes,rpc_calls=excluded.rpc_calls,connects=excluded.connects,credits_est=excluded.credits_est",
                                (self.day, self.base_bytes + self.bytes_seen, self.base_rpc + self.rpc_calls, self.base_conn + self.connects, self.credits_today()))
    # ---- helpers
    def _rpc(self, method, params):
        url = helius_rpc_url()
        if not url: return None
        self.rpc_calls += 1
        d = HTTP.get(url, body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, tries=2, priority=True)
        return d.get("result") if isinstance(d, dict) else None
    def _sol_usd(self):
        px, ts = self.sol
        if not px or now() - ts > 30:
            d = HTTP.get("https://api.dexscreener.com/tokens/v1/solana/" + SOL_MINT, cache_s=20, tries=2) or []
            best = None
            for p in d if isinstance(d, list) else []:
                if (p.get("baseToken") or {}).get("address") != SOL_MINT: continue
                if (p.get("quoteToken") or {}).get("symbol") not in ("USDC", "USDT"): continue
                if not best or ((p.get("liquidity") or {}).get("usd") or 0) > ((best.get("liquidity") or {}).get("usd") or 0): best = p
            if best and best.get("priceUsd"): self.sol = (float(best["priceUsd"]), now())
        return self.sol[0]
    def _unsupported(self, pid, mint, sym, pair, why):
        self.pos[pid] = dict(pid=pid, mint=mint, sym=sym, pair=pair, unsupported=why, px=None)
        self.log.event("Risk", "WS_UNSUPPORTED", mint, sym, position_id=pid, reason=why)
    def _resolve(self, pid, mint, sym, pair):
        a = self._rpc("getAccountInfo", [pair, {"encoding": "base64"}])
        v = (a or {}).get("value") if isinstance(a, dict) else None
        if not v: return self._unsupported(pid, mint, sym, pair, "pool account not found")
        L = self.LAYOUTS.get(v.get("owner"))
        if not L: return self._unsupported(pid, mint, sym, pair, f"pool program {str(v.get('owner'))[:8]}... not supported (CLMM/DLMM/other)")
        data = base64.b64decode(v["data"][0]); kind, v0, v1, m0, m1 = L
        if len(data) < max(v0, v1, m0, m1) + 32: return self._unsupported(pid, mint, sym, pair, "pool data too short")
        pk = lambda o: SWL.b58e(data[o:o + 32])
        vaults, mints = (pk(v0), pk(v1)), (pk(m0), pk(m1))
        if mint not in mints: return self._unsupported(pid, mint, sym, pair, "position mint not in pool")
        bi = mints.index(mint); qi = 1 - bi
        if mints[qi] not in self.QUOTES: return self._unsupported(pid, mint, sym, pair, f"quote {mints[qi][:6]}... not SOL/USDC/USDT")
        r = self._rpc("getMultipleAccounts", [[vaults[bi], vaults[qi]], {"encoding": "jsonParsed"}]) or {}
        vals = r.get("value") or []
        try:
            ib, iq = [x["data"]["parsed"]["info"] for x in vals]
            assert ib["mint"] == mint and iq["mint"] == mints[qi]
        except Exception:
            return self._unsupported(pid, mint, sym, pair, "vault check failed")
        slot = (r.get("context") or {}).get("slot") or 0
        st = dict(pid=pid, mint=mint, sym=sym, pair=pair, kind=kind, unsupported=None, vb=vaults[bi], vq=vaults[qi],
                  db=int(ib["tokenAmount"]["decimals"]), dq=int(iq["tokenAmount"]["decimals"]), qkind=self.QUOTES[mints[qi]],
                  amt={vaults[bi]: int(ib["tokenAmount"]["amount"]), vaults[qi]: int(iq["tokenAmount"]["amount"])},
                  slot={vaults[bi]: slot, vaults[qi]: slot}, note_ts=now(), px=None, px_ts=0.0, px_slot=0, subs=[],
                  ds=None, liq=0, resync_dev=None, last_resync=now(), devs=[], n=0)
        self.pos[pid] = st
        self._calc(st, force=True)
        self.log.event("Risk", "WS_TRACK", mint, sym, position_id=pid, pool=kind, quote=st["qkind"], px=st["px"])
        if self.connected: self._subscribe(st)
    def _calc(self, st, force=False):
        sb, sq = st["slot"][st["vb"]], st["slot"][st["vq"]]
        # Both vaults move in the same slot on a swap: wait (<=1.5 s) for the pair so a half-updated pair is never priced.
        if sb != sq and not force and now() - st["note_ts"] < 1.5: return
        ab, aq = st["amt"][st["vb"]], st["amt"][st["vq"]]
        if ab <= 0 or aq <= 0: return
        usd = 1.0 if st["qkind"] == "usd" else self._sol_usd()
        if not usd: return
        px = (aq / 10 ** st["dq"]) / (ab / 10 ** st["db"]) * usd
        if px != st["px"] or force:
            st["px"], st["px_ts"], st["px_slot"] = px, now(), max(sb, sq); st["n"] += 1
        st["pending"] = False
    def _subscribe(self, st):
        st["subs"] = []
        for v in (st["vb"], st["vq"]):
            self.next_id += 1; self.req[self.next_id] = (st["pid"], v)
            self.ws.send({"jsonrpc": "2.0", "id": self.next_id, "method": "accountSubscribe",
                          "params": [v, {"encoding": "base64", "commitment": "confirmed", "dataSlice": {"offset": 64, "length": 8}}]})
    def _drop(self, pid):
        st = self.pos.pop(pid, None)
        for sid in (st or {}).get("subs") or []:
            self.subs.pop(sid, None)
            if self.connected:
                try: self.next_id += 1; self.ws.send({"jsonrpc": "2.0", "id": self.next_id, "method": "accountUnsubscribe", "params": [sid]})
                except Exception: pass
    def _sync(self):
        with self.log.lock:
            rows = self.log.db.execute("SELECT id,mint,symbol,pair FROM positions WHERE status='OPEN'").fetchall()
        open_ids = {r[0] for r in rows}
        with self.lock:
            for pid in [p for p in self.pos if p not in open_ids]: self._drop(pid)
            for pid, mint, sym, pair in rows:
                if pid not in self.pos: self._resolve(pid, mint, sym, pair)
    def _resync(self):
        """Every 60 s: independent RPC snapshot of both vaults (1 call). A >3% mismatch vs the stream disables the
        live trigger for that position until the next clean resync; the snapshot is adopted if it is newer."""
        for st in list(self.pos.values()):
            if st.get("unsupported") or now() - st["last_resync"] < 60: continue
            st["last_resync"] = now()
            r = self._rpc("getMultipleAccounts", [[st["vb"], st["vq"]], {"encoding": "jsonParsed"}]) or {}
            try:
                ib, iq = [x["data"]["parsed"]["info"] for x in r.get("value") or []]
                slot = (r.get("context") or {}).get("slot") or 0
                ab, aq = int(ib["tokenAmount"]["amount"]), int(iq["tokenAmount"]["amount"])
            except Exception:
                continue
            usd = 1.0 if st["qkind"] == "usd" else self._sol_usd()
            rpx = (aq / 10 ** st["dq"]) / (ab / 10 ** st["db"]) * (usd or 0)
            with self.lock:
                st["resync_dev"] = round(100 * (st["px"] / rpx - 1), 3) if st["px"] and rpx else None
                if slot >= st.get("px_slot", 0):
                    st["amt"][st["vb"]], st["amt"][st["vq"]] = ab, aq
                    st["slot"][st["vb"]] = st["slot"][st["vq"]] = slot
                    self._calc(st, force=True)
    def _handle(self, m):
        if "id" in m and m.get("id") in self.req:
            pid, v = self.req.pop(m["id"])
            if "result" in m and pid in self.pos:
                self.subs[m["result"]] = (pid, v); self.pos[pid].setdefault("subs", []).append(m["result"])
            elif "error" in m:
                self.log.event("Risk", "WS_SUB_ERROR", position_id=pid, error=str(m.get("error"))[:200])
            return
        if m.get("method") != "accountNotification": return
        p = m.get("params") or {}
        hit = self.subs.get(p.get("subscription"))
        if not hit: return
        pid, v = hit; st = self.pos.get(pid)
        if not st: return
        res = p.get("result") or {}
        raw = base64.b64decode(((res.get("value") or {}).get("data") or [""])[0] or "")
        if len(raw) < 8: return
        with self.lock:
            st["amt"][v] = struct.unpack("<Q", raw[:8])[0]
            st["slot"][v] = (res.get("context") or {}).get("slot") or st["slot"][v]
            st["note_ts"] = now(); st["pending"] = True
            self._calc(st)
    def _status(self):
        self._account(flush=True)
        self.log.event("Risk", "WS_STATUS", connected=self.connected, positions=len(self.pos),
                       tracked=sum(1 for s in self.pos.values() if not s.get("unsupported")), credits_today_est=self.credits_today(),
                       bytes_today=self.base_bytes + self.bytes_seen, rpc_calls_today=self.base_rpc + self.rpc_calls,
                       per_pos={s.get("sym"): dict(n=s.get("n"), px=s.get("px"), resync_dev=s.get("resync_dev"), devs=s.get("devs", [])[-5:],
                                                    unsupported=s.get("unsupported")) for s in self.pos.values()})
    # ---- used by Risk (other threads)
    def note_ds(self, pid, px, liq):
        st = self.pos.get(pid)
        if st is not None: st["ds"] = (px, now()); st["liq"] = liq
    def liq(self, pid): return (self.pos.get(pid) or {}).get("liq") or 0
    def prices(self):
        out = {}
        with self.lock:
            for pid, st in self.pos.items():
                if st.get("px") and not st.get("unsupported") and not st.get("pending"):
                    out[pid] = (st["px"], st["px_ts"], dict(ws_slot=st.get("px_slot"), ws_pool=st.get("kind")))
        return out
    def live_ok(self, pid):
        if not CFG.get("helius_ws_live"): return False, "helius_ws_live off"
        if self.over_budget: return False, "daily Helius WS credit budget reached"
        if not self.connected or not self.ws: return False, "ws disconnected"
        silent = now() - self.ws.last_rx
        if silent > (CFG.get("ws_silent_s") or 15): return False, f"ws silent {silent:.0f}s"
        st = self.pos.get(pid)
        if not st: return False, "not tracked yet"
        if st.get("unsupported"): return False, st["unsupported"]
        if len(st.get("subs") or []) < 2: return False, "subscribing"
        if not st.get("px"): return False, "no reserve price yet"
        if st.get("resync_dev") is not None and abs(st["resync_dev"]) > 3: return False, f"stream vs RPC snapshot {st['resync_dev']}%"
        refs = {}
        if st.get("ds") and now() - st["ds"][1] <= 90: refs["dexscreener"] = st["ds"][0]
        j = (getattr(self.fast, "last", {}) or {}).get(pid)
        if j and now() - j[1] <= 30: refs["jupiter"] = j[0]
        if not refs: return False, "no fresh DexScreener/Jupiter reference"
        devs = {k: 100 * (st["px"] / v - 1) for k, v in refs.items() if v}
        best = min(devs.items(), key=lambda kv: abs(kv[1]))
        st["devs"] = (st.get("devs", []) + [round(best[1], 2)])[-50:]
        if abs(best[1]) > (CFG.get("ws_max_dev_pct") or 8.0):
            return False, f"reserve price {best[1]:+.1f}% vs {best[0]} (>{CFG.get('ws_max_dev_pct') or 8.0}%)"
        return True, f"ok ({best[0]} {best[1]:+.2f}%)"
    # ---- thread
    def loop(self):
        import wsmini, socket as _sock
        backoff = 5
        while True:
            url = helius_rpc_url()
            if not url or not CFG.get("helius_ws_live"):
                time.sleep(300); continue
            try:
                self._sync()
                if self.over_budget: raise RuntimeError("daily credit budget reached")
                self.ws = wsmini.WS(url.replace("https://", "wss://", 1), timeout=15)
                self.ws.sock.settimeout(0.5); self._last_rx_bytes = 0
                self.connects += 1; self.subs.clear(); self.req.clear()
                self.connected = True; backoff = 5
                self.log.event("Risk", "WS_CONNECTED", positions=len(self.pos))
                with self.lock:
                    for st in self.pos.values():
                        if not st.get("unsupported"): self._subscribe(st)
                t_ping = t_sync = t_res = t_stat = now()
                while True:
                    t = now()
                    if t - t_sync >= 2: self._sync(); t_sync = t
                    if t - t_ping >= 5: self.ws.ping(); t_ping = t
                    if t - t_res >= 10: self._resync(); t_res = t
                    if t - t_stat >= 600: self._status(); t_stat = t
                    self._account()
                    if self.over_budget: raise RuntimeError("daily credit budget reached")
                    if t - self.ws.last_rx > 30: raise ConnectionError("no data or pong for 30s")
                    with self.lock:
                        for st in self.pos.values():
                            if st.get("pending"): self._calc(st)
                    try: msg = self.ws.recv()
                    except _sock.timeout: continue
                    self._handle(json.loads(msg))
            except Exception as e:
                err = re.sub(r"api-key=[^&\s'\"]+", "api-key=***", repr(e))[:200]
                was = self.connected; self.connected = False
                self.log.event("Risk", "WS_RECONNECT", error=err, backoff_s=backoff, was_connected=was)
                try: self._account(flush=True)
                except Exception: pass
            finally:
                if self.ws:
                    try: self.ws.close()
                    except Exception: pass
            time.sleep(backoff if not self.over_budget else 600); backoff = min(backoff * 2, 120)

    # ---------------------------------------------------------------- Orchestrator
class Desk:
    def __init__(self):
        self.log = Logger(); self.scout = Scout(self.log); self.filter = Filter(self.log)
        self.analyst = Analyst(self.log); self.risk = Risk(self.log); self.exec = Executor(self.log, self.risk)
        self.stop = threading.Event()
        self.shadow_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow")
        self.fast = FastStopShadow(self.log)
        self.feed = HeliusFeed(self.log, self.fast) if helius_rpc_url() else None
        self.risk.feed = self.feed
        self.liq_hist = {}   # v9 shadow: mint -> [(ts, liq)] over the last 2h (Scout candidates)
        try:
            self.ghosts = SV9.GhostBook(self.log.db, self.log.lock, self.log.jsonl, lambda u: HTTP.get(u, tries=2), paper_sell_fill, CFG,
                                        self.log.event, SWL.shadow_event)
        except Exception as e:
            self.ghosts = None; self.log.event("Shadow", "ERROR", rule="ghost_v9_init", error=repr(e)[:200])
    def _mig_ts(self, mint):
        try:
            with self.log.lock:
                r = self.log.db.execute("SELECT ts FROM pp_migrations WHERE mint=?", (mint,)).fetchone()
            return r[0] if r else None
        except Exception: return None
    def ws_risk_loop(self):
        while not self.stop.is_set():
            try: self.risk.ws_tick()
            except Exception as e: self.log.event("Risk", "WS_ERROR", error=repr(e)[:300], tb=traceback.format_exc()[-800:])
            self.stop.wait(0.25)
    def risk_loop(self):
        while not self.stop.is_set():
            try: self.risk.poll()
            except Exception as e: self.log.event("Risk", "ERROR", error=repr(e), tb=traceback.format_exc()[-800:])
            self.stop.wait(CFG["risk_poll_s"])
    def live_gates(self, ap):
        """v10 (user-approved 2026-11-19): ex-shadow entry gates with the lowest loss per trade on 203 paper trades.
        new_pair_6h is CFG max_age_h=6 in Scout. -> list of kill reasons ([] = may enter)."""
        r = []; so = ap.components.get("_score_old")
        if so is None or so < CFG["min_score_old"]: r.append(f"score_old {so} < {CFG['min_score_old']:.0f}")
        c = ap.fp.cand
        bp = SV9.entry_gates(c, ap, self.liq_hist, self._mig_ts(c.mint))["buy_pressure"]
        if bp[1]: r.append(f"buy pressure weak {bp[0]}")
        return r
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
            inp = ap.components.get("_inputs") or {}; E = SHADOW_ENTRY_V5
            org = inp.get("jupiter_organic_score"); v24 = (c.vol or {}).get("h24"); m5 = (c.price_change or {}).get("m5")
            churn = (float(v24) / c.mcap) if (v24 is not None and c.mcap) else None
            top1 = (ap.fp.checks or {}).get("top1_pct_ex_pools")
            gates = dict(
                organic_50=(org, None if org is None else org < E["organic_50"], f"Jupiter organic score < {E['organic_50']:.0f} (wash/bot volume)"),
                churn_50=(None if churn is None else round(churn, 3),
                          None if churn is None else (c.age_h <= E["churn_max_age_h"] and churn < E["churn_50"]),
                          f"young coin (<= {E['churn_max_age_h']:.0f}h) with 24h volume < {E['churn_50']:.0%} of mcap (supply not changing hands)"),
                m5_chase_10=(m5, None if m5 is None else m5 > E["m5_chase_10"], f"buying after > +{E['m5_chase_10']:.0f}% in 5 min (chasing)"),
                top1_10=(top1, None if top1 is None else top1 > E["top1_10"], f"largest non-pool holder > {E['top1_10']:.0f}%"))
            so = ap.components.get("_score_old")
            gates["old_rules_v5"] = (so, None if so is None else so < OLD_RULES_V5["pass_score"],
                                     f"pre-v6 score (smart 0 counted as 0) < {OLD_RULES_V5['pass_score']:.0f}; old size 8% of equity, no liquidity cap")
            sr = strict_v7_reasons(c, ap.fp.checks, ap.score)   # v8: would the v6/v7 strict live entry rules have taken it?
            gates["strict_v7"] = (score_bucket(ap.score), bool(sr), "v7 strict entry: " + ("; ".join(sr) if sr else "would also pass"))
            try: gates.update(SV9.entry_gates(c, ap, self.liq_hist, self._mig_ts(c.mint)))   # v9 shadow entry gates (log only)
            except Exception as e: self.log.event("Shadow", "ERROR", rule="v9_gates", error=repr(e)[:200])
            for k in ("old_rules_v5", "new_pair_6h", "buy_pressure"): gates.pop(k, None)   # v10: live now, no longer shadow
            if order and self.ghosts and pid:
                try: self.ghosts.on_live_entry(pid, order, ap)   # v9 ghost exit variants for this live entry
                except Exception as e: self.log.event("Shadow", "ERROR", rule="ghost_v9_entry", error=repr(e)[:200])
            for rule, (val, blk, why) in gates.items():
                SWL.shadow_event(self.log.db, self.log.lock, self.log.jsonl, rule, "ENTRY_GATE", c.mint, c.symbol, pid,
                                 cycle=self.log.cycle, score=ap.score, value=val, would_block=blk, entered=bool(order), rule_text=why)
            cyc = self.log.cycle
            def _sk():   # v3: shadow 'Skeptic' checklist (GT OHLCV) runs off the cycle's critical path; log only
                try:
                    wait_not_critical()
                    sk = SWL.skeptic(lambda u: HTTP.get(u, tries=2), c)
                    SWL.shadow_event(self.log.db, self.log.lock, self.log.jsonl, "skeptic", "ENTRY_GATE", c.mint, c.symbol, pid,
                                     cycle=cyc, score=ap.score, entered=bool(order), **sk)
                except Exception as e:
                    self.log.event("Shadow", "ERROR", rule="skeptic", error=repr(e))
            self.shadow_pool.submit(_sk)
        except Exception as e:
            self.log.event("Shadow", "ERROR", error=repr(e))
    def _v9_entry(self, fp, ap, order):
        """v9 SHADOW entries for Filter passes the live desk did not enter (log only)."""
        if not self.ghosts or order: return
        try:
            score = ap.score if ap else self.analyst.last_score.get(fp.cand.mint)
            with self.log.lock:
                live = {r[0] for r in self.log.db.execute("SELECT mint FROM positions WHERE status='OPEN'")}
            self.ghosts.consider_entries(fp, score, self._mig_ts(fp.cand.mint), live, self.exec.equity()[1])
        except Exception as e:
            self.log.event("Shadow", "ERROR", rule="ghost_v9_shadow_entry", error=repr(e)[:200])
    def cycle(self):
        self.log.cycle += 1
        t0 = now(); http0 = HTTP.snapshot(); self.log.event("Logger", "CYCLE_START")
        t_sc = now()
        cands, st = self.scout.run()
        scout_s = now() - t_sc
        try:   # v9 shadow: liquidity history for the liq_growth gate (in memory, 2h)
            tcut = now() - 7200
            for c in cands:
                h = [x for x in self.liq_hist.get(c.mint, []) if x[0] >= tcut]; h.append((now(), c.liq_usd)); self.liq_hist[c.mint] = h[-60:]
            for m in [m for m, h in self.liq_hist.items() if not h or h[-1][0] < tcut]: self.liq_hist.pop(m, None)
        except Exception: pass
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
        t_fa = now(); CRITICAL.set()
        for fp, ap in HTTP.map(fa, alive, workers=CFG["filter_workers"]):
            if not fp: continue
            fpass += 1
            if not ap:
                self._v9_entry(fp, None, None); continue
            apass += 1
            blk = self.live_gates(ap)
            if blk: self.log.kill("Executor", ap.fp.cand.mint, ap.fp.cand.symbol, ap.fp.cand.pair_address, blk, {})
            order = None if blk else self.exec.run(ap)
            if order: entries += 1
            else: self._v9_entry(fp, ap, order)
            self.shadow_entry_gate(ap, order)
        CRITICAL.clear(); fa_s = now() - t_fa
        cash, eq = self.exec.equity()
        http1 = HTTP.snapshot()
        http_delta = {k: http1.get(k, 0) - http0.get(k, 0) for k in http1 if http1.get(k, 0) != http0.get(k, 0)}
        st.update(filter_pass=fpass, analyst_pass=apass, entries=entries, cash=round(cash, 2), equity=round(eq, 2),
                  secs=round(now() - t0, 1), scout_s=round(scout_s, 1), filter_s=round(fa_s, 1), http=http_delta)
        self.log.cycle_done(st)
        print(iso(), "cycle", self.log.cycle, st, flush=True)
    def main(self, once=False):
        self.log.event("Logger", "DESK_START", mode="PAPER", ruleset=RULESET, cfg=CFG, strict_v7_shadow=STRICT_V7)
        if self.log.reset: self.log.event("Logger", "LEDGER_RESET", ruleset=RULESET, cash=CFG["start_equity"])
        if once:   # test mode: a single cycle, no risk thread
            self.cycle(); print("http", HTTP.stats, flush=True); return
        threading.Thread(target=self.risk_loop, daemon=True).start()
        threading.Thread(target=self.fast.loop, daemon=True, name="fast-stop-shadow").start()
        if self.feed:
            threading.Thread(target=self.feed.loop, daemon=True, name="helius-ws").start()
            threading.Thread(target=self.ws_risk_loop, daemon=True, name="ws-risk").start()
        if self.scout.pp: threading.Thread(target=self.scout.pp.loop, daemon=True, name="pumpportal-shadow").start()
        if self.ghosts: threading.Thread(target=self.ghosts.loop, daemon=True, name="ghost-v9-shadow").start()
        self._health_write(reset=True)
        while True:
            t0 = now()
            try:
                self.cycle(); self._health_write(ok=True)
            except Exception as e:
                CRITICAL.clear()
                self.log.event("Logger", "CYCLE_ERROR", error=repr(e), tb=traceback.format_exc()[-1500:])
                print(iso(), "cycle error", repr(e), flush=True)
                self._health_write(ok=False, err=e)
            time.sleep(max(30, CFG["scan_interval_s"] - (now() - t0)))

    def _health_write(self, ok=None, err=None, reset=False):
        """v7: cycle health for the VM watchdog, the syncer (Supabase desk_status) and the external monitor.
        The heartbeat alone keeps updating when cycles fail, so record the last SUCCESSFUL cycle separately."""
        try:
            try: h = json.load(open(HEALTH_PATH))
            except Exception: h = {}
            if reset:
                h.update(consecutive_errors=0, started_ts=now())
            elif ok:
                h.update(last_success_ts=now(), last_success_cycle=self.log.cycle, consecutive_errors=0)
            else:
                msg = re.sub(r"(?i)(api[-_]?key|token|secret|apikey|key)=[^&\s'\"]+", r"\1=***", repr(err))
                msg = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", msg)
                h.update(consecutive_errors=int(h.get("consecutive_errors") or 0) + 1, last_error=msg[:200], last_error_ts=now(),
                         last_error_cycle=self.log.cycle)
            h["updated_ts"] = now()
            tmp = HEALTH_PATH + ".tmp"; json.dump(h, open(tmp, "w")); os.replace(tmp, HEALTH_PATH)
        except Exception as e:
            print(iso(), "health write failed", repr(e)[:120], flush=True)

if __name__ == "__main__":
    sys.path.insert(0, BASE)
    Desk().main(once="--once" in sys.argv)
