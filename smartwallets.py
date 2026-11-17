#!/usr/bin/env python3
"""
MemeDesk smart-wallet list + shadow rules. PAPER ONLY: reads public on-chain/market data, never signs or trades.

Two halves:
  * SmartBook  (imported by desk.py, runs inside the Analyst): counts how many tracked smart wallets bought a
    candidate in the last 6h -> Analyst `smart_wallets` factor. v5: read from Helius (each smart wallet's own token
    account history, ~13 credits and <1 s per lookup); GeckoTerminal /trades is only the fallback.
  * Builder    (`python3 smartwallets.py`, its own supervised process): builds/refreshes the smart-wallet list in
    the background and runs the shadow exit rule. Heavy RPC work never touches the desk's ~20s cycle.

How a wallet becomes "smart" (free sources only: DexScreener, Jupiter lite, GeckoTerminal, Solana public RPC):
  1. Winners = tokens <= 7 days old that reached $150k-$50M mcap (>= ~10x from a pump.fun launch), or that the desk saw and that later did >= 3x.
  2. For each winner: page getSignaturesForAddress back to the creation tx (skip if > max pages). pump.fun tokens use
     their bonding-curve PDA (pre-migration trades only), others the mint address.
     Deployer (creation signer) and every signer in the first slots (bundle/sniper cluster) are blacklisted.
  3. Early buyers = owners receiving the token in successful swaps after creation+60s (whole bonding curve, or the
     first 25% of the mint's tx history)
     whose own token balance grew while spending >= 0.05 SOL. Entry mcap = SOL paid / tokens * supply * SOL price.
  4. A "winning early entry" = the token later reached >= 5x that entry mcap.
  5. Smart = >= 2 winning early entries, not a bot (1000 txs in < 2h), not deployer/bundler.
     score = wins - 0.5 * losses (losses/wins also come from forward observations of their later buys).
Shadow rules (log only, never affect trades):
  (a) min2_smart: every Analyst pass/entry records whether "buy only if >= 2 smart wallets entered" would block it.
  (b) smart_exit: for open positions, the first sell by a tracked smart wallet is a hypothetical exit
      (same fill model as Risk); compared with the real exit once the position closes.
  Also (in desk.py Risk.poll, log only): trail_4pct (4% trail after +12%) and combo_tp_trail4
  (40% at +15% + 4% trail on the remainder). Those write the same shadow_events kinds (WATCH / PARTIAL / SHADOW_EXIT / COMPARE).
"""
import hashlib, json, os, sqlite3, sys, threading, time, traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("MEMEDESK_DATA", BASE)
DB_PATH = os.path.join(DATA, "desk.sqlite")
JSONL_PATH = os.path.join(DATA, "events.jsonl")
RPC = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"

SW = dict(
    winner_min_mcap=150_000, winner_max_mcap=50_000_000, winner_max_age_h=168, winner_desk_mult=3.0, winner_desk_min_mcap=100_000,
    max_sig_pages=60, skip_first_s=60, bundle_slots=2, early_frac=0.25, early_sample=120, min_buy_sol=0.05, entry_mult=5.0,
    smart_min_wins=2, smart_min_score=1.5, bot_window_h=2.0, min_list_for_factor=10,
    lookup_window_h=6.0, lookup_min_usd=50, obs_eval_h=6.0, obs_win=0.5, obs_loss=-0.4,
    shadow_poll_s=120, winners_refresh_s=1200, obs_eval_s=600, rpc_workers=3,
    # Helius Free (1M credits/month, 10 RPS): only for deep signature paging past the public-RPC cap and as a fallback.
    # Budget counted conservatively at 10 credits/call (Helius docs list 1-10 for archival methods).
    helius_daily_calls=2400, helius_credits_per_call=10, helius_max_pages_per_token=240,
    # v5: Analyst smart factor from Helius (getSignaturesForAddress + getTransaction = 1 credit each, Helius credits page).
    helius_smart=True, helius_smart_daily_credits=15000, smart_compare_every=4,
)
FACTOR_MAP = {0: 0.0, 1: 45.0, 2: 75.0}   # 3+ -> 100
PTP = dict(frac=0.40, tp_pct=0.15)          # shadow partial take-profit: 40% at +15%, 60% rides the real exit
# ---- base58 + PDA (pump.fun bonding curve = pre-migration trading only; pure python, no deps)
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def b58d(s):
    n = 0
    for c in s: n = n * 58 + B58.index(c)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + b
def b58e(b):
    n = int.from_bytes(b, "big"); s = ""
    while n: n, r = divmod(n, 58); s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s
P = 2**255 - 19; D = (-121665 * pow(121666, P - 2, P)) % P
def on_curve(b):
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= P: return False
    y2 = y * y % P; x2 = (y2 - 1) * pow(D * y2 + 1, P - 2, P) % P
    return x2 == 0 or pow(x2, (P - 1) // 2, P) == 1
def pda(seeds, program):
    pid = b58d(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not on_curve(h): return b58e(h)
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
def pump_curve(mint):
    try: return pda([b"bonding-curve", b58d(mint)], PUMP_PROGRAM)
    except Exception: return None

def factor_value(n): return FACTOR_MAP.get(n, 100.0)

# ---- v5: smart-wallet buys via Helius on the wallets' own token accounts (replaces the GT /trades bottleneck)
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
STABLES = ("EPjFWdu5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")
_ata_cache = {}
def ata(owner, mint, prog):
    k = (owner, mint, prog)
    if k not in _ata_cache: _ata_cache[k] = pda([b58d(owner), b58d(prog), b58d(mint)], ATA_PROGRAM)
    return _ata_cache[k]
def wallet_buy_usd(tx, wallet, mint, sol_usd, token_px=None):
    """-> (USD the wallet spent buying `mint` in one tx, tokens received); (None, 0) if its balance did not grow."""
    meta = tx.get("meta") or {}
    if meta.get("err"): return None, 0.0
    def bal(arr, m):
        return sum(float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0) for b in arr or []
                   if b.get("mint") == m and b.get("owner") == wallet)
    got = bal(meta.get("postTokenBalances"), mint) - bal(meta.get("preTokenBalances"), mint)
    if got <= 0: return None, 0.0
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    sol = 0.0
    if wallet in keys:
        i = keys.index(wallet); sol = max(0.0, (meta["preBalances"][i] - meta["postBalances"][i]) / 1e9)
    sol += max(0.0, bal(meta.get("preTokenBalances"), SOL_MINT) - bal(meta.get("postTokenBalances"), SOL_MINT))
    usd = sum(max(0.0, bal(meta.get("preTokenBalances"), m) - bal(meta.get("postTokenBalances"), m)) for m in STABLES)
    paid = sol * (sol_usd or 0) + usd
    if token_px: paid = max(paid, got * token_px)   # paid with another token / by a relayer: value the tokens received
    return paid, got
def helius_smart_buys(rpc_batch, url, wallets, mint, token_prog, window_s, min_usd, sol_usd, token_px=None,
                      sig_limit=100, chunk=6, max_tx=18):
    """-> (buys {wallet: dict(ts, usd, sig)} or None on RPC failure, credits used). Same question as the GT lookup
    ("which tracked wallets bought this token >= min_usd in the window"), answered from each wallet's own associated
    token account history, so it covers the whole window and every pool instead of GT's last 300 pool trades."""
    wallets = sorted(wallets); atas = [ata(w, mint, token_prog) for w in wallets]
    sigs = rpc_batch(url, [("getSignaturesForAddress", [a, {"limit": sig_limit, "commitment": "confirmed"}]) for a in atas])
    credits = len(atas)
    if not sigs or all(r is None for r in sigs): return None, credits
    cutoff = now() - window_s
    pend = {w: [(x["signature"], x.get("blockTime")) for x in (r or [])
                if x.get("err") is None and (x.get("blockTime") or 0) >= cutoff][:max_tx] for w, r in zip(wallets, sigs)}
    buys = {}
    while True:   # newest first, a few txs per wallet per round, stop a wallet at its first qualifying buy
        todo = []
        for w, lst in pend.items():
            if w in buys: continue
            todo += [(w, s_, ts) for s_, ts in lst[:chunk]]; pend[w] = lst[chunk:]
        if not todo: break
        txs = rpc_batch(url, [("getTransaction", [s_, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                                                       "commitment": "confirmed"}]) for _, s_, _ in todo])
        credits += len(todo)
        for (w, s_, ts), tx in zip(todo, txs):
            if not tx or w in buys: continue
            try: usd, got = wallet_buy_usd(tx, w, mint, sol_usd, token_px)
            except Exception: usd, got = None, 0.0
            if usd is not None and usd >= min_usd: buys[w] = dict(ts=ts, usd=round(usd, 2), sig=s_, price=(usd / got if got else 0.0))
    return buys, credits

SCHEMA = """
CREATE TABLE IF NOT EXISTS smart_wallets(address TEXT PRIMARY KEY, score REAL, wins INT, losses INT, seen INT,
    is_smart INT DEFAULT 0, bot INT, first_seen REAL, last_seen REAL, tokens TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS sw_entries(mint TEXT, wallet TEXT, ts REAL, sol REAL, entry_mcap REAL, peak_mcap REAL,
    outcome TEXT, symbol TEXT, PRIMARY KEY(mint, wallet));
CREATE TABLE IF NOT EXISTS sw_tokens(mint TEXT PRIMARY KEY, symbol TEXT, status TEXT, processed_ts REAL, creation_ts REAL,
    deployer TEXT, n_sigs INT, n_early INT, n_buyers INT, n_wins INT, peak_mcap REAL, note TEXT);
CREATE TABLE IF NOT EXISTS sw_blacklist(address TEXT PRIMARY KEY, reason TEXT, mint TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS sw_obs(mint TEXT, wallet TEXT, ts REAL, price REAL, src TEXT, evaluated INT DEFAULT 0,
    eval_ts REAL, eval_price REAL, outcome TEXT, PRIMARY KEY(mint, wallet));
CREATE TABLE IF NOT EXISTS shadow_events(id INTEGER PRIMARY KEY, ts REAL, ts_iso TEXT, rule TEXT, kind TEXT,
    mint TEXT, symbol TEXT, position_id INT, data TEXT);
CREATE INDEX IF NOT EXISTS shadow_pos ON shadow_events(position_id, kind);
CREATE TABLE IF NOT EXISTS helius_usage(day TEXT PRIMARY KEY, calls INT, purpose TEXT);
CREATE TABLE IF NOT EXISTS helius_smart_usage(day TEXT PRIMARY KEY, credits INT, lookups INT, fallbacks INT);
CREATE INDEX IF NOT EXISTS sw_entries_wallet ON sw_entries(wallet);
CREATE INDEX IF NOT EXISTS smart_wallets_smart ON smart_wallets(is_smart);
"""
def ensure_schema(db): db.executescript(SCHEMA)

def now(): return time.time()
def iso(ts=None): return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or now()))
def gt_ts(s):
    try: return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception: return None

def gt_trades(get, pair, min_usd=None):
    """GeckoTerminal last <=300 trades of a pool (free, carries tx_from_address). None on failure."""
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair}/trades"
    if min_usd: url += f"?trade_volume_in_usd_greater_than={min_usd}"
    try:
        d = get(url, cache_s=90)  # shared HTTP cache; Analyst + builder reuse the same pool snapshot
    except TypeError:
        d = get(url)
    if not d or not isinstance(d.get("data"), list): return None
    out = []
    for x in d["data"]:
        a = x.get("attributes") or {}
        kind = a.get("kind"); w = a.get("tx_from_address")
        if not w or kind not in ("buy", "sell"): continue
        px = a.get("price_to_in_usd") if kind == "buy" else a.get("price_from_in_usd")
        out.append(dict(kind=kind, wallet=w, ts=gt_ts(a.get("block_timestamp") or ""), price=float(px or 0),
                        usd=float(a.get("volume_in_usd") or 0), tx=a.get("tx_hash")))
    return out

def shadow_event(db, lock, jsonl, rule, kind, mint=None, symbol=None, position_id=None, **data):
    ts = now()
    with lock:
        db.execute("INSERT INTO shadow_events(ts,ts_iso,rule,kind,mint,symbol,position_id,data) VALUES(?,?,?,?,?,?,?,?)",
                   (ts, iso(ts), rule, kind, mint, symbol, position_id, json.dumps(data, default=str)))
        if jsonl:
            jsonl.write(json.dumps(dict(ts=iso(ts), stage="Shadow", kind=kind, rule=rule, mint=mint, symbol=symbol,
                                        position_id=position_id, **data), default=str) + "\n")

# ---------------------------------------------------------------- Skeptic (shadow pre-entry checklist, deterministic)
SKEPTIC = dict(min_rr=1.5, stop_pct=0.10, min_liq_mcap=0.05, max_ext_from_6h_low=1.40, max_h1=40.0, ohlcv_cache_s=600)
_ohlcv = {}
def skeptic(get, c):
    """Would a sceptical trader take this entry? Risk = the desk's -10% stop (v6); reward = distance to the 24h swing high.
    Returns dict(would_block, fails, checks). Log only."""
    pair, price = c.pair_address, float(c.price_usd or 0)
    hit = _ohlcv.get(pair)
    if not hit or now() - hit[0] > SKEPTIC["ohlcv_cache_s"]:
        d = get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair}/ohlcv/minute?aggregate=15&limit=96") or {}
        rows = (((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        hit = (now(), rows); _ohlcv[pair] = hit
    rows = hit[1]
    fails, checks = [], {}
    if rows and price:
        t_last = max(r[0] for r in rows)
        swing_high = max(r[2] for r in rows)
        low6 = min((r[3] for r in rows if r[0] >= t_last - 6 * 3600), default=None)
        reward = swing_high / price - 1
        rr = reward / SKEPTIC["stop_pct"]
        checks.update(swing_high_24h=swing_high, reward_to_high_pct=round(100 * reward, 2), rr=round(rr, 2),
                      ext_vs_6h_low=(round(price / low6, 2) if low6 else None))
        if rr < SKEPTIC["min_rr"]: fails.append(f"R:R {rr:.2f} < {SKEPTIC['min_rr']} (target = 24h high, {100*reward:.1f}% away, stop -{100*SKEPTIC['stop_pct']:.0f}%)")
        if low6 and price / low6 > SKEPTIC["max_ext_from_6h_low"]: fails.append(f"extended: {price/low6:.2f}x the 6h low")
    else:
        checks["ohlcv"] = "unavailable"
    lm = (c.liq_usd / c.mcap) if c.mcap else 0
    h1 = (c.price_change or {}).get("h1") or 0
    checks.update(liq_mcap=round(lm, 4), h1=h1)
    if lm < SKEPTIC["min_liq_mcap"]: fails.append(f"weak liq/mcap {lm:.3f} < {SKEPTIC['min_liq_mcap']}")
    if h1 > SKEPTIC["max_h1"]: fails.append(f"extended: +{h1:.0f}% in 1h")
    return dict(would_block=bool(fails), fails=fails, checks=checks)

# ---------------------------------------------------------------- used inside the desk (Analyst)
class SmartBook:
    def __init__(self, db, lock):
        self.db, self.lock = db, lock
        with lock: ensure_schema(db)
        self._set, self._ts = set(), 0.0
        self.rpc = self.url = self.wait = None   # set by the desk: (HTTP.rpc_batch, Helius URL, wait_not_critical)
        self._prog, self._n = {}, 0
    def smart_set(self):
        if now() - self._ts > 60:
            with self.lock:
                self._set = {r[0] for r in self.db.execute("SELECT address FROM smart_wallets WHERE is_smart=1")}
            self._ts = now()
        return self._set
    def _credits_today(self):
        with self.lock:
            r = self.db.execute("SELECT credits FROM helius_smart_usage WHERE day=?", (time.strftime("%Y-%m-%d"),)).fetchone()
        return r[0] if r else 0
    def _note(self, credits, fallback=False):
        try:
            with self.lock:
                self.db.execute("INSERT INTO helius_smart_usage(day,credits,lookups,fallbacks) VALUES(?,?,1,?) ON CONFLICT(day) DO "
                                "UPDATE SET credits=credits+?, lookups=lookups+1, fallbacks=fallbacks+?",
                                (time.strftime("%Y-%m-%d"), credits, int(fallback), credits, int(fallback)))
        except Exception: pass
    def _token_prog(self, mint):
        if mint not in self._prog:
            r = self.rpc(self.url, [("getAccountInfo", [mint, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])])
            owner = (((r or [None])[0] or {}).get("value") or {}).get("owner")
            if owner not in TOKEN_PROGRAMS: return None, 1
            self._prog[mint] = owner
            return owner, 1
        return self._prog[mint], 0
    def _helius(self, get, S, mint, price):
        """-> (buys, credits) or (None, credits) when unavailable (no key, budget spent, RPC failure)."""
        if not (SW["helius_smart"] and self.rpc and self.url): return None, 0
        if self._credits_today() >= SW["helius_smart_daily_credits"]: return None, 0
        prog, cr = self._token_prog(mint)
        if not prog: return None, cr
        try:
            d = get(f"https://lite-api.jup.ag/price/v3?ids={SOL_MINT}", cache_s=600) or {}
            sol = float((d.get(SOL_MINT) or {}).get("usdPrice") or 0) or None
        except Exception: sol = None
        buys, c2 = helius_smart_buys(self.rpc, self.url, S, mint, prog, SW["lookup_window_h"] * 3600, SW["lookup_min_usd"],
                                     sol, float(price or 0) or None)
        return buys, cr + c2
    def _gt(self, get, S, pair):
        tr = gt_trades(get, pair, SW["lookup_min_usd"])
        if tr is None: return None, None
        cutoff = now() - SW["lookup_window_h"] * 3600
        buys = {}
        for t in tr:
            if t["kind"] == "buy" and (t["ts"] or 0) >= cutoff and t["wallet"] in S:
                buys.setdefault(t["wallet"], t)
        tss = [t["ts"] for t in tr if t["ts"]]
        return buys, dict(trades_scanned=len(tr), trades_span_min=(round((max(tss) - min(tss)) / 60, 1) if tss else None))
    def _compare(self, get, S, mint, pair, hb):
        """SHADOW check of the Helius source against GT on the same smart list (off the critical path, log only)."""
        try:
            if self.wait: self.wait()
            gb, gi = self._gt(get, S, pair)
            if gb is None: return
            h, g = set(hb), set(gb)
            shadow_event(self.db, self.lock, None, "smart_src", "COMPARE", mint, None, None, helius=sorted(h), gt=sorted(g),
                         agree=(h == g), only_helius=len(h - g), only_gt=len(g - h), gt_span_min=(gi or {}).get("trades_span_min"))
        except Exception: pass
    def factor(self, get, mint, pair, price=None):
        """-> (value 0..100 or None if unavailable, info). None never penalises: the Analyst renormalises.
        v5: Helius (wallets' own token-account history, whole 6h window) first; GeckoTerminal /trades as fallback."""
        S = self.smart_set()
        if len(S) < SW["min_list_for_factor"]:
            return None, dict(status=f"smart list too small ({len(S)} < {SW['min_list_for_factor']}): factor skipped, weights renormalised", list_size=len(S))
        t0 = now(); extra = {}
        try: buys, credits = self._helius(get, S, mint, price)
        except Exception: buys, credits = None, 0
        if buys is not None:
            src = "helius token-account history >$50, 6h"
            self._note(credits); self._n += 1
            if SW["smart_compare_every"] and self._n % SW["smart_compare_every"] == 0:
                threading.Thread(target=self._compare, args=(get, S, mint, pair, dict(buys)), daemon=True).start()
        else:
            if credits: self._note(credits, fallback=True)
            try: buys, extra = self._gt(get, S, pair)
            except Exception: buys = None
            src = "geckoterminal trades >$50 (fallback)"
            if buys is None:
                return None, dict(status="trades lookup failed: factor skipped, weights renormalised", list_size=len(S))
        n = len(buys)
        with self.lock:
            for w, t in buys.items():
                self.db.execute("INSERT OR IGNORE INTO sw_obs(mint,wallet,ts,price,src) VALUES(?,?,?,?,?)", (mint, w, t["ts"], t.get("price") or 0, "analyst"))
        return factor_value(n), dict(status="ok", count=n, wallets=sorted(buys)[:10], list_size=len(S), src=src,
                                     secs=round(now() - t0, 2), credits=credits, **(extra or {}))

# ---------------------------------------------------------------- background builder process
class Builder:
    def __init__(self):
        sys.path.insert(0, BASE)
        from desk import Http, helius_rpc_url   # same keep-alive + *shared* inter-process limiter as the desk
        self.http = Http()
        k = os.environ.get("HELIUS_API_KEY")
        self._helius = helius_rpc_url()   # never logged
        self.lock = threading.RLock()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA busy_timeout=10000")
        ensure_schema(self.db)
        self.jsonl = open(JSONL_PATH, "a", buffering=1)
        self.queue, self.sol_usd, self.sol_ts = [], None, 0.0
        self.stop = threading.Event()

    def log(self, kind, **d):
        rec = dict(ts=iso(), stage="SmartWallets", kind=kind, **d)
        with self.lock: self.jsonl.write(json.dumps(rec, default=str) + "\n")
        print(iso(), kind, json.dumps(d, default=str)[:300], flush=True)

    def q(self, sql, args=()):
        with self.lock: return self.db.execute(sql, args).fetchall()
    def x(self, sql, args=()):
        with self.lock: self.db.execute(sql, args)

    def helius_left(self):
        if not self._helius: return 0
        r = self.q("SELECT calls FROM helius_usage WHERE day=?", (time.strftime("%Y-%m-%d"),))
        return SW["helius_daily_calls"] - (r[0][0] if r else 0)
    def _call(self, url, method, params, tries=5):
        try: d = self.http.get(url, body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, tries=tries)
        except Exception: return None   # (exception text could contain the URL -> swallowed, never logged)
        if not d: return None
        if "error" in d:
            if d["error"].get("code") in (429, -32429, -32005): time.sleep(3)
            return None
        return d.get("result")
    def _count_helius(self, n=1):
        self.x("INSERT INTO helius_usage(day,calls,purpose) VALUES(?,?,'') ON CONFLICT(day) DO UPDATE SET calls=calls+?",
               (time.strftime("%Y-%m-%d"), n, n))
    def rpc(self, method, params, helius=False):
        """Cheap methods (getAccountInfo / getTokenSupply) prefer Helius when budget remains.
        Heavy getTransaction / signature paging stays on public RPC first; Helius is used when
        asked (deep paging past the public cap) or as a fallback, still within the daily budget."""
        cheap = method in ("getAccountInfo", "getTokenSupply", "getBalance")
        if (helius or cheap) and self._helius and self.helius_left() > 0:
            self._count_helius(1)
            r = self._call(self._helius, method, params, tries=3)
            if r is not None: return r
        if not helius:
            r = self._call(RPC, method, params)
            if r is not None or self.helius_left() <= 0: return r
        if not self._helius or self.helius_left() <= 0: return None
        self._count_helius(1)
        return self._call(self._helius, method, params, tries=3)
    def get_txs(self, sigs, helius=False):
        """JSON-RPC batch of getTransaction (public RPC by default, to stay inside the Helius free budget)."""
        if not sigs: return []
        params_base = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}
        calls = [("getTransaction", [s, params_base]) for s in sigs]
        url, bs = RPC, 10
        if helius and self._helius and self.helius_left() >= len(sigs):
            url, bs = self._helius, 20
            self._count_helius(len(sigs))
        try:
            return self.http.rpc_batch(url, calls, tries=3, batch_size=bs)
        except Exception:
            return [None] * len(sigs)

    def sol_price(self):
        if not self.sol_usd or now() - self.sol_ts > 600:
            d = self.http.get(f"https://lite-api.jup.ag/price/v3?ids={SOL_MINT}", cache_s=600) or {}
            p = (d.get(SOL_MINT) or {}).get("usdPrice")
            if p: self.sol_usd, self.sol_ts = float(p), now()
        return self.sol_usd

    # ---------------- 1. winners
    def refresh_winners(self):
        t0 = now()
        cand = {}   # mint -> dict(first_ts, first_mcap, max_mcap, symbol)
        for mint, sym, met, ts in self.q("SELECT mint,symbol,metrics,ts FROM kills WHERE stage='Scout' AND ts>? ORDER BY id", (now() - 7 * 86400,)):
            try: d = json.loads(met)
            except Exception: continue
            a, mc = d.get("age_h"), d.get("mcap") or 0
            if a is None or a > SW["winner_max_age_h"]: continue
            c = cand.setdefault(mint, dict(first_ts=ts, first_mcap=mc, max_mcap=mc, symbol=sym))
            c["max_mcap"] = max(c["max_mcap"], mc)
        for (mint, sym) in self.q("SELECT DISTINCT mint,symbol FROM scores UNION SELECT DISTINCT mint,symbol FROM positions"):
            cand.setdefault(mint, dict(first_ts=None, first_mcap=None, max_mcap=0, symbol=sym))
        from desk import CFG as _DESK_CFG
        jup_ttl = (_DESK_CFG.get("jup_list_ttl") or {}).get("24h", 600)
        gt_ttl = _DESK_CFG.get("gt_page_cache_s") or 150
        for cat in ("toptrending", "toptraded", "toporganicscore"):
            for t in self.http.get(f"https://lite-api.jup.ag/tokens/v2/{cat}/24h?limit=100", cache_s=jup_ttl) or []:
                if isinstance(t, dict) and t.get("id"): cand.setdefault(t["id"], dict(first_ts=None, first_mcap=None, max_mcap=0, symbol=t.get("symbol")))
        for path in ([f"trending_pools?duration={d_}&page={p_}" for d_ in ("6h", "24h") for p_ in (1, 2, 3)]
                     + [f"new_pools?page={p_}" for p_ in (1, 2)]
                     # graduated pump.fun tokens (PumpSwap) + other launchpads: cheapest winners to analyse (bonding curve)
                     + [f"dexes/pumpswap/pools?sort=h24_volume_usd_desc&page={p_}" for p_ in range(1, 11)]
                     + [f"dexes/{dx}/pools?sort=h24_volume_usd_desc&page={p_}" for dx in ("raydium-launchlab", "meteora-dbc") for p_ in (1, 2)]):
            d = self.http.get(f"https://api.geckoterminal.com/api/v2/networks/solana/{path}", cache_s=gt_ttl) or {}
            for x in d.get("data") or []:
                bt = (((x.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                if bt.startswith("solana_"): cand.setdefault(bt[7:], dict(first_ts=None, first_mcap=None, max_mcap=0, symbol=None))
        done = {r[0] for r in self.q("SELECT mint FROM sw_tokens WHERE status!='too_busy' OR ?", (self.helius_left() <= 0,))}
        mints = [m for m in cand if m not in done and m != SOL_MINT]
        best = {}
        for i in range(0, len(mints), 30):
            for p in self.http.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i+30])) or []:
                if not isinstance(p, dict) or p.get("chainId") != "solana": continue
                m = p["baseToken"]["address"]
                if m in best and ((best[m].get("liquidity") or {}).get("usd") or 0) >= ((p.get("liquidity") or {}).get("usd") or 0): continue
                best[m] = p
        winners = []
        for m, p in best.items():
            c = cand[m]; created = p.get("pairCreatedAt")
            if not created: continue
            age_h = (now() * 1000 - created) / 3.6e6
            if age_h > SW["winner_max_age_h"]: continue
            mc = p.get("marketCap") or p.get("fdv") or 0
            peak = max(mc, c["max_mcap"] or 0)
            why = None
            if peak > SW["winner_max_mcap"]: continue   # not a fresh memecoin (stocks/majors/old tokens on new pairs)
            if mc >= SW["winner_min_mcap"]: why = f"mcap ${mc:,.0f} at {age_h:.0f}h old"
            elif c["first_mcap"] and mc >= SW["winner_desk_min_mcap"] and mc >= SW["winner_desk_mult"] * c["first_mcap"]:
                why = f"{mc / c['first_mcap']:.1f}x since desk first saw it"
            if why: winners.append(dict(mint=m, symbol=p["baseToken"].get("symbol"), peak=peak, age_h=age_h, why=why))
        # pump.fun first (bonding-curve path is cheap), then younger first: fewer signatures to page through
        winners.sort(key=lambda w: (not w["mint"].endswith("pump"), w["age_h"]))
        self.queue = winners
        self.log("SW_WINNERS", candidates=len(cand), priced=len(best), winners=len(winners), secs=round(now() - t0, 1),
                 http={k: v for k, v in self.http.snapshot().items() if "geckoterminal" in k or "jup" in k},   # v3: cumulative, 429 visibility
                 sample=[(w["symbol"], w["why"]) for w in winners[:8]])

    # ---------------- 2. early buyers of one winner
    def get_tx(self, sig):
        r = self.get_txs([sig])
        return r[0] if r else None
    @staticmethod
    def signer_and_delta(tx, mint):
        """-> (buyer, tokens bought, SOL paid, err). Buyer = the single owner whose balance of `mint` grew (works for
        fee-relayed txs where the fee payer is not the buyer); SOL paid = WSOL gained by the side that lost tokens
        (the pool), else the buyer's lamport decrease. Multi-receiver txs (bundles/airdrops) are ignored."""
        USDC = "EPjFWdu5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        try:
            meta = tx["meta"]
            if meta.get("err"): return None, 0, 0, meta["err"]
            keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
            def tot(arr, m):
                d = {}
                for b in arr or []:
                    if b.get("mint") == m and b.get("owner"):
                        d[b["owner"]] = d.get(b["owner"], 0.0) + float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                return d
            def delta(m):
                pre, post = tot(meta.get("preTokenBalances"), m), tot(meta.get("postTokenBalances"), m)
                return {o: post.get(o, 0.0) - pre.get(o, 0.0) for o in set(pre) | set(post)}
            dm = delta(mint)
            gain = {o: v for o, v in dm.items() if v > 0}
            losers = {o for o, v in dm.items() if v < 0}
            if len(gain) != 1 or not losers: return None, 0, 0, "not a single swap"
            buyer, d_tok = next(iter(gain.items()))
            dw, du = delta(SOL_MINT), delta(USDC)
            sol = sum(v for o, v in dw.items() if o in losers and v > 0)
            usdc = sum(v for o, v in du.items() if o in losers and v > 0)
            if not sol and not usdc and buyer in keys:
                i = keys.index(buyer); sol = (meta["preBalances"][i] - meta["postBalances"][i]) / 1e9
            return buyer, d_tok, (sol, usdc), None
        except Exception:
            return None, 0, 0, "parse"

    def process_token(self, w):
        mint, sym, peak = w["mint"], w["symbol"], w["peak"]
        t0 = now()
        hel = dict(pages=0)
        def page_all(addr):
            sigs, before, pages = [], None, 0
            while True:
                deep = pages >= SW["max_sig_pages"]   # past the public-RPC cap -> Helius (budgeted)
                if deep and (hel["pages"] >= SW["helius_max_pages_per_token"] or self.helius_left() <= 0): return sigs, False
                p = {"limit": 1000}
                if before: p["before"] = before
                r = self.rpc("getSignaturesForAddress", [addr, p], helius=deep)
                if r is None: return (sigs or None), False
                pages += 1; sigs += r
                if deep: hel["pages"] += 1
                if len(r) < 1000: return sigs, True
                before = r[-1]["signature"]
        # pump.fun tokens: page the bonding-curve PDA (only pre-migration trades = the early phase, far fewer txs)
        src, sigs, complete = "mint", None, False
        curve = pump_curve(mint) if mint.endswith("pump") else None
        if curve:
            sigs, complete = page_all(curve)
            bt = [x["blockTime"] for x in sigs or [] if x.get("blockTime") and x.get("err") is None]
            if complete and len(sigs) >= 50 and bt and max(bt) - min(bt) >= 900: src = "pump.fun bonding curve"
            else: sigs = None   # curve sniped/graduated within minutes (bots/bundle): real early buying was on the AMM -> mint
        if not sigs:
            src = "mint"; sigs, complete = page_all(mint)
        if sigs is None:
            self.x("INSERT OR REPLACE INTO sw_tokens(mint,symbol,status,processed_ts,note) VALUES(?,?,?,?,?)", (mint, sym, "error", now(), "signatures unavailable"))
            return
        if not complete:
            self.x("INSERT OR REPLACE INTO sw_tokens(mint,symbol,status,processed_ts,n_sigs,peak_mcap,note) VALUES(?,?,?,?,?,?,?)",
                   (mint, sym, "too_busy_deep" if hel["pages"] else "too_busy", now(), len(sigs), peak,
                    f">{len(sigs)} txs on {src}, creation not reachable (public RPC + Helius budget cap)"))
            self.log("SW_TOKEN_SKIP", mint=mint, symbol=sym, sigs=len(sigs), src=src, reason="too busy", helius_pages=hel["pages"],
                     helius_calls_left_today=self.helius_left())
            return
        sigs.reverse()   # chronological
        sigs = [s for s in sigs if s.get("blockTime")]
        if not sigs: return
        c_slot, c_time = sigs[0]["slot"], sigs[0]["blockTime"]
        # deployer + bundle/sniper cluster (first slots)
        cluster = [s for s in sigs if s["slot"] <= c_slot + SW["bundle_slots"] and s.get("err") is None][:14]
        deployer = None
        ctx = self.get_txs([s["signature"] for s in cluster])
        for i, tx in enumerate(ctx):
            if not tx: continue
            try:
                k0 = tx["transaction"]["message"]["accountKeys"][0]; signer = k0["pubkey"] if isinstance(k0, dict) else k0
            except Exception: continue
            buyer = self.signer_and_delta(tx, mint)[0]
            if buyer and buyer != signer:
                self.x("INSERT OR IGNORE INTO sw_blacklist VALUES(?,?,?,?)", (buyer, "bundle/sniper (launch slots)", mint, now()))
            if i == 0: deployer = signer
            self.x("INSERT OR IGNORE INTO sw_blacklist VALUES(?,?,?,?)", (signer, "deployer" if i == 0 else "bundle/sniper (launch slots)", mint, now()))
        # early window sample
        # early window: successful txs from creation+60s until the later of (25% of the successful tx history, +30 min);
        # bot frenzies can burn thousands of (mostly failed) txs in the first minute, so the window is also time-based
        succ = [s for s in sigs if s.get("err") is None]
        if src == "mint" and succ:
            t_end = max(succ[min(len(succ) - 1, int(len(succ) * SW["early_frac"]))]["blockTime"], c_time + 1800)
        else:
            t_end = float("inf")   # whole bonding curve = pre-migration phase
        early = [s for s in succ if c_time + SW["skip_first_s"] <= s["blockTime"] <= t_end]
        if len(early) > SW["early_sample"]:
            step = len(early) / SW["early_sample"]
            early = [early[int(i * step)] for i in range(SW["early_sample"])]
        sup = self.rpc("getTokenSupply", [mint]) or {}
        supply = float(((sup.get("value") or {}).get("uiAmount")) or 0)
        solusd = self.sol_price()
        txs = self.get_txs([s["signature"] for s in early])
        bl = {r[0] for r in self.q("SELECT address FROM sw_blacklist")}
        buyers, wins = {}, 0
        stats = dict(tx_ok=sum(1 for t in txs if t), not_swap=0, small=0, blacklisted=0)
        for s, tx in zip(early, txs):
            if not tx: continue
            signer, d_tok, paid, err = self.signer_and_delta(tx, mint)
            if err or not signer or d_tok <= 0: stats["not_swap"] += 1; continue
            if signer in bl: stats["blacklisted"] += 1; continue
            if signer in buyers: continue
            sol_eq = paid[0] + (paid[1] / solusd if solusd else 0)
            if sol_eq < SW["min_buy_sol"]: stats["small"] += 1; continue
            d_sol = sol_eq
            entry_mcap = (sol_eq / d_tok) * supply * solusd if (supply and solusd) else None
            won = bool(entry_mcap) and peak >= SW["entry_mult"] * entry_mcap
            wins += won
            buyers[signer] = (s["blockTime"], d_sol, entry_mcap, "win" if won else "early_nowin")
        with self.lock:
            self.db.execute("BEGIN")
            try:
                for wlt, (ts, sol, em, oc) in buyers.items():
                    self.db.execute("INSERT OR REPLACE INTO sw_entries VALUES(?,?,?,?,?,?,?,?)", (mint, wlt, ts, sol, em, peak, oc, sym))
                self.db.execute("INSERT OR REPLACE INTO sw_tokens VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (mint, sym, "done", now(), c_time, deployer, len(sigs), len(early), len(buyers), wins, peak, f"{w.get('why')} · {src}"))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK"); raise
        self.log("SW_TOKEN_DONE", mint=mint, symbol=sym, src=src, sigs=len(sigs), early_sampled=len(early), buyers=len(buyers),
                 winning_entries=wins, cluster_blacklisted=len(cluster), secs=round(now() - t0, 1), helius_pages=hel["pages"],
                 helius_calls_left_today=self.helius_left(), **stats)

    # ---------------- 3. scoring + bot check
    def rescore(self):
        bl = {r[0] for r in self.q("SELECT address FROM sw_blacklist")}
        agg = {}
        for wlt, oc, sym, ts in self.q("SELECT wallet,outcome,symbol,ts FROM sw_entries"):
            a = agg.setdefault(wlt, dict(wins=0, losses=0, seen=0, toks=[], first=ts, last=ts))
            a["seen"] += 1; a["first"] = min(a["first"], ts); a["last"] = max(a["last"], ts)
            if oc == "win": a["wins"] += 1; a["toks"].append(sym)
        for wlt, oc, ts in self.q("SELECT wallet,outcome,ts FROM sw_obs WHERE evaluated=1"):
            a = agg.setdefault(wlt, dict(wins=0, losses=0, seen=0, toks=[], first=ts, last=ts))
            a["seen"] += 1; a["last"] = max(a["last"] or 0, ts or 0)
            if oc == "win": a["wins"] += 1
            elif oc == "loss": a["losses"] += 1
        bots = {r[0]: r[1] for r in self.q("SELECT address,bot FROM smart_wallets WHERE bot IS NOT NULL")}
        # bot check only for wallets that could qualify (1 cheap RPC call each, cached forever)
        for wlt, a in agg.items():
            if a["wins"] >= SW["smart_min_wins"] and wlt not in bots and wlt not in bl:
                r = self.rpc("getSignaturesForAddress", [wlt, {"limit": 1000}])
                if r is None: continue
                span_h = ((r[0].get("blockTime") or 0) - (r[-1].get("blockTime") or 0)) / 3600 if r else 99
                bots[wlt] = int(len(r) >= 1000 and span_h < SW["bot_window_h"])
        rows = []
        for wlt, a in agg.items():
            score = a["wins"] - 0.5 * a["losses"]
            bot = bots.get(wlt)
            smart = int(a["wins"] >= SW["smart_min_wins"] and score >= SW["smart_min_score"] and not bot and wlt not in bl)
            rows.append((wlt, round(score, 2), a["wins"], a["losses"], a["seen"], smart, bot, a["first"], a["last"],
                         json.dumps(sorted(set(a["toks"]))[:20]), now()))
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany("INSERT OR REPLACE INTO smart_wallets VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK"); raise
        n = sum(r[5] for r in rows)
        return n, len(rows)

    # ---------------- 4. forward observations (later buys by smart wallets) -> wins/losses
    def evaluate_obs(self):
        rows = self.q("SELECT mint,wallet,ts,price FROM sw_obs WHERE evaluated=0 AND ts<?", (now() - SW["obs_eval_h"] * 3600,))
        if not rows: return 0
        mints = sorted({r[0] for r in rows}); px = {}
        for i in range(0, len(mints), 30):
            for p in self.http.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i+30])) or []:
                if isinstance(p, dict) and p.get("priceUsd"): px.setdefault(p["baseToken"]["address"], float(p["priceUsd"]))
        for mint, wlt, ts, p0 in rows:
            p1 = px.get(mint, 0.0)   # missing pair after 6h = token gone -> loss
            ch = (p1 / p0 - 1) if p0 else 0
            oc = "win" if ch >= SW["obs_win"] else ("loss" if ch <= SW["obs_loss"] else "flat")
            self.x("UPDATE sw_obs SET evaluated=1,eval_ts=?,eval_price=?,outcome=? WHERE mint=? AND wallet=?", (now(), p1, oc, mint, wlt))
        return len(rows)

    # ---------------- 5. shadow rule (b): exit when smart money exits
    def shadow_exit_poll(self):
        S = {r[0] for r in self.q("SELECT address FROM smart_wallets WHERE is_smart=1")}
        opn = self.q("SELECT id,mint,symbol,pair,opened_ts,entry_price,qty,size_usd FROM positions WHERE status='OPEN'")
        watched = {r[0] for r in self.q("SELECT DISTINCT position_id FROM shadow_events WHERE rule='smart_exit' AND kind IN ('WATCH','SHADOW_EXIT')")}
        fired = {r[0] for r in self.q("SELECT DISTINCT position_id FROM shadow_events WHERE rule='smart_exit' AND kind='SHADOW_EXIT'")}
        for pid, mint, sym, pair, ots, ep, qty, size in opn:
            if pid not in watched:
                shadow_event(self.db, self.lock, self.jsonl, "smart_exit", "WATCH", mint, sym, pid, list_size=len(S), opened_ts=ots)
                watched.add(pid)
            if pid in fired or not S: continue
            tr = gt_trades(self.http.get, pair, SW["lookup_min_usd"])
            if not tr: continue
            sells = sorted([t for t in tr if t["kind"] == "sell" and t["wallet"] in S and (t["ts"] or 0) >= ots], key=lambda t: t["ts"])
            if not sells: continue
            t = sells[0]
            d = self.http.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair}") or {}
            liq = (((d.get("pairs") or [None])[0] or {}).get("liquidity") or {}).get("usd") or 0
            from desk import CFG
            slip = CFG["base_slippage"] + CFG["swap_fee"] + (t["price"] * qty) / max(liq / 2, 1)
            xp = t["price"] * (1 - slip); pnl = xp * qty - size
            shadow_event(self.db, self.lock, self.jsonl, "smart_exit", "SHADOW_EXIT", mint, sym, pid, wallet=t["wallet"],
                         trade_ts=t["ts"], trade_iso=iso(t["ts"]), ref_price=t["price"], exit_price=xp, entry_price=ep,
                         hyp_pnl_usd=round(pnl, 2), hyp_pnl_pct=round(100 * pnl / size, 2), smart_sellers=len({s['wallet'] for s in sells}))
            fired.add(pid)

    def shadow_reconcile(self):
        """Close the loop for every watched position that has closed: real vs shadow PnL for both rules."""
        done = {r[0] for r in self.q("SELECT DISTINCT position_id FROM shadow_events WHERE rule='smart_exit' AND kind='COMPARE'")}
        watched = {r[0] for r in self.q("SELECT DISTINCT position_id FROM shadow_events WHERE rule='smart_exit' AND kind='WATCH'")}
        for pid, mint, sym, pnl, pct, reason in self.q("SELECT id,mint,symbol,pnl_usd,pnl_pct,exit_reason FROM positions WHERE status='CLOSED'"):
            if pid not in watched or pid in done: continue
            ex = self.q("SELECT data FROM shadow_events WHERE rule='smart_exit' AND kind='SHADOW_EXIT' AND position_id=?", (pid,))
            hyp = json.loads(ex[0][0]) if ex else None
            shadow_event(self.db, self.lock, self.jsonl, "smart_exit", "COMPARE", mint, sym, pid, actual_pnl_usd=round(pnl or 0, 2),
                         actual_pnl_pct=round(pct or 0, 2), actual_reason=reason, triggered=bool(hyp),
                         shadow_pnl_usd=(hyp["hyp_pnl_usd"] if hyp else round(pnl or 0, 2)),
                         shadow_pnl_pct=(hyp["hyp_pnl_pct"] if hyp else round(pct or 0, 2)))

    def shadow_partial_tp(self):
        """Shadow exit variant: sell 40% at +15% (if the position's peak reached it), the other 60% exits exactly like
        the real trade. Compared with the actual PnL for every watched position once it closes."""
        from desk import CFG
        done = {r[0] for r in self.q("SELECT DISTINCT position_id FROM shadow_events WHERE rule='partial_tp' AND kind='COMPARE'")}
        watched = {r[0] for r in self.q("SELECT DISTINCT position_id FROM shadow_events WHERE rule='smart_exit' AND kind='WATCH'")}
        for pid, mint, sym, ep, qty, size, peak, xp, pnl in self.q(
                "SELECT id,mint,symbol,entry_price,qty,size_usd,peak_price,exit_price,pnl_usd FROM positions WHERE status='CLOSED'"):
            if pid not in watched or pid in done or not ep or not qty: continue
            tp = ep * (1 + PTP["tp_pct"]); hit = (peak or 0) >= tp
            if hit:
                part = PTP["frac"] * qty * tp * (1 - CFG["base_slippage"] - CFG["swap_fee"])
                spnl = part + (1 - PTP["frac"]) * qty * (xp or 0) - size
            else: spnl = pnl or 0
            shadow_event(self.db, self.lock, self.jsonl, "partial_tp", "COMPARE", mint, sym, pid, tp_hit=hit, tp_price=tp,
                         peak=peak, actual_pnl_usd=round(pnl or 0, 2), shadow_pnl_usd=round(spnl, 2),
                         shadow_pnl_pct=round(100 * spnl / size, 2) if size else None)

    def shadow_loop(self):
        while not self.stop.is_set():
            try: self.shadow_exit_poll(); self.shadow_reconcile(); self.shadow_partial_tp()
            except Exception as e: self.log("SW_SHADOW_ERROR", error=repr(e), tb=traceback.format_exc()[-600:])
            self.stop.wait(SW["shadow_poll_s"])

    def main(self, seed_only=False, max_tokens=None):
        self.log("SW_START", cfg=SW)
        if not seed_only: threading.Thread(target=self.shadow_loop, daemon=True, name="shadow").start()
        last_w = last_o = 0.0; processed = 0
        while True:
            try:
                if now() - last_w > SW["winners_refresh_s"] or not self.queue:
                    if now() - last_w > 300: self.refresh_winners(); last_w = now()
                if now() - last_o > SW["obs_eval_s"]:
                    self.evaluate_obs(); last_o = now()
                if self.queue:
                    w = self.queue.pop(0)
                    if not self.q("SELECT 1 FROM sw_tokens WHERE mint=? AND (status!='too_busy' OR ?)", (w["mint"], self.helius_left() <= 0)):
                        self.process_token(w); processed += 1
                        n, tot = self.rescore()
                        self.log("SW_LIST", smart=n, tracked=tot, tokens_processed=processed, queue=len(self.queue))
                    if max_tokens and processed >= max_tokens: break
                else:
                    n, tot = self.rescore()
                    if seed_only: break
                    time.sleep(60)
            except Exception as e:
                self.log("SW_ERROR", error=repr(e), tb=traceback.format_exc()[-800:]); time.sleep(30)

if __name__ == "__main__":
    b = Builder()
    if "--seed" in sys.argv:
        b.main(seed_only=True)
    else:
        b.main()
