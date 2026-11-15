"""Solana memecoin paper desk: Scout -> Filter -> Analyst -> Risk -> Executor, Logger sees all.

Run:  python desk.py            (loops forever, paper mode)
      python desk.py --once     (one cycle)
Data: DexScreener + GeckoTerminal (pairs/metrics, unique buyers), RugCheck (authorities, LP, holders, insiders).
"""
import json, os, random, re, secrets, sys, time, urllib.request, zlib

CFG = dict(
    age_min_h=0.5, age_max_h=72, mcap_min=300_000, mcap_max=20_000_000,
    liq_min=40_000, vol1h_min=25_000, top10_max=25, dev_max=3, bundle_max=10,
    pump1h_max=80, lp_locked_min=90, score_min=70,
    size_pct=0.08, stop_pct=0.06, trail_arm_pct=0.12, trail_pct=0.06,
    fee=0.005, impact_max=0.015, entry_drop_max=0.03,
    tm_replays=10_000, tm_horizon_min=240, tm_stop_first_max=0.5,   # Time Machine gate
    day_loss_max=0.03, max_open=5, max_buys_day=20, time_exit_h=4,   # risk limits, time exit
    start_equity=10_000, scan_every_s=120, tick_s=10, kill_cache_s=1800, gecko_gap_s=6.5,
    gecko_lists_scan=3, cg_calls_day=320,   # 320/day = the Demo key's 10k/month
)
DS, RC = "https://api.dexscreener.com", "https://api.rugcheck.xyz/v1"
DIR = os.path.dirname(os.path.abspath(__file__))


def load_keys(path=os.path.join(DIR, "keys.env")):
    """Free API keys live in keys.env on the server only (gitignored), never in the code or the chat."""
    if os.path.exists(path):
        for line in open(path):
            k, _, v = line.strip().partition("=")
            if k and v and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip())


load_keys()
HELIUS = os.environ.get("HELIUS_KEY")
CG_KEY = os.environ.get("CG_DEMO_KEY")  # CoinGecko Demo: own quota for buyers + Time Machine bars
if CG_KEY:
    CFG["gecko_lists_scan"] = 4  # keyless pool no longer carries buyers/bars: one more list per scan
RPC = os.environ.get("SOLANA_RPC") or (f"https://mainnet.helius-rpc.com/?api-key={HELIUS}" if HELIUS
                                       else "https://api.mainnet-beta.solana.com")
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
CG = "https://api.coingecko.com/api/v3/onchain/networks/solana"
LOGS, STATE = os.path.join(DIR, "logs"), os.path.join(DIR, "state.json")


_gt_last = [0.0]


def redact(url):
    return re.sub(r"(api-key=)[^&]+", r"\1***", url)


def get(url, body=None, headers=None):
    """GET, or POST JSON when body is given. None on failure (logged)."""
    if url.startswith(GT):  # one pace for every GeckoTerminal call (lists, buyers, 1m bars): bursts cause the 429s
        time.sleep(max(0.0, _gt_last[0] + CFG["gecko_gap_s"] - time.time()))
        _gt_last[0] = time.time()
    for i in range(3):
        try:
            req = urllib.request.Request(url, data=body and json.dumps(body).encode(), headers={
                "User-Agent": "paper-desk/1.0", "Accept": "application/json", "Content-Type": "application/json",
                **(headers or {})})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except Exception as e:
            err = e
            if getattr(e, "code", None) == 429:
                break  # rate limited: retrying right away only digs the hole deeper
            time.sleep(1 + 2 * i)
    Logger.log("http_error", url=redact(url), error=str(err))  # keys must never reach the logs/dashboard
    return None


def handoff(obj, keys, frm, to):
    """Hard handoff: next agent refuses incomplete packets."""
    missing = [k for k in keys if obj.get(k) is None]
    if missing:
        raise ValueError(f"{frm}->{to} handoff missing {missing}")
    return obj


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def clip(x):
    return max(0.0, min(1.0, x))


def slip(usd, liq):
    """Cost of a market order: fees + constant-product price impact (order vs quote-side reserve = liq/2)."""
    return CFG["fee"] + usd / max(liq / 2, 1)


def notify(st, text):
    """Phone push via ntfy.sh (free app, no account). Topic is random, so only whoever knows it can subscribe."""
    if not st.get("ntfy"):
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{st['ntfy']}", data=text.encode(), headers={"Title": "MrTrader"})
        urllib.request.urlopen(req, timeout=5).close()
    except Exception as e:  # a lost notification must never stop the desk
        Logger.log("http_error", url="https://ntfy.sh/", error=str(e))


class Logger:
    @staticmethod
    def log(kind, **data):
        os.makedirs(LOGS, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, **data}
        with open(os.path.join(LOGS, f"{kind if kind in ('kill', 'trade', 'outcome') else 'events'}.jsonl"), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        if kind != "kill":
            print(json.dumps(rec), flush=True)


def gecko_mints(resp):
    """GeckoTerminal pool list -> base token mints ("solana_<mint>")."""
    ids = [((p.get("relationships") or {}).get("base_token") or {}).get("data", {}).get("id", "")
           for p in (resp or {}).get("data") or []]
    return [i.split("_", 1)[1] for i in ids if i.startswith("solana_")]


class Scout:
    """Fresh Solana pairs, 30m-72h old, mcap $300k-$20M."""
    def __init__(self):
        self.turn, self.gecko_seen = 0, {}  # GeckoTerminal mint -> last time listed

    def run(self, now=None, tick=lambda: None):
        """tick() is called between slow calls so open positions keep being watched during a scan."""
        now = now or time.time()
        mints = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1",
                     "/community-takeovers/latest/v1"):
            for t in get(DS + path) or []:
                if t.get("chainId") == "solana" and t.get("tokenAddress") not in mints:
                    mints.append(t["tokenAddress"])
        # new_pools is all <30min old (Scout drops those); busiest + trending pools cover the 30m-72h window
        gecko = [f"trending_pools?duration={d}" for d in ("5m", "1h", "6h", "24h")] + \
                [f"pools?sort={s}&page={p}" for s in ("h24_volume_usd_desc", "h24_tx_count_desc") for p in (1, 2, 3)]
        # 3 of the 10 lists per scan, in rotation (GCP IP gets 429 at ~9 calls/min); a mint stays a candidate for 30 min
        for q in [gecko[(self.turn + k) % len(gecko)] for k in range(CFG["gecko_lists_scan"])]:
            tick()
            for m in gecko_mints(get(f"{GT}/{q}")):
                self.gecko_seen[m] = now
        self.turn += CFG["gecko_lists_scan"]
        self.gecko_seen = {m: t for m, t in self.gecko_seen.items() if now - t < 1800}
        mints += [m for m in self.gecko_seen if m not in mints]
        pairs = []
        for i in range(0, len(mints), 30):
            pairs += get(f"{DS}/tokens/v1/solana/{','.join(mints[i:i + 30])}") or []
        return self.select(pairs, now)

    @staticmethod
    def select(pairs, now):
        best = {}
        for p in pairs:
            if p.get("chainId") != "solana" or not p.get("pairCreatedAt"):
                continue
            mint = p["baseToken"]["address"]
            if f(p.get("liquidity", {}).get("usd")) > f(best.get(mint, {}).get("liquidity", {}).get("usd"), -1):
                best[mint] = p
        out = []
        for mint, p in best.items():
            age_h = (now - p["pairCreatedAt"] / 1000) / 3600
            mcap = f(p.get("marketCap") or p.get("fdv"))
            if CFG["age_min_h"] <= age_h <= CFG["age_max_h"] and CFG["mcap_min"] <= mcap <= CFG["mcap_max"]:
                out.append(handoff({"mint": mint, "symbol": p["baseToken"].get("symbol"), "pair": p,
                                    "age_h": age_h, "mcap": mcap}, ["mint", "pair", "age_h", "mcap"], "Scout", "Filter"))
        return out


class Filter:
    """Kills on any red flag. Missing safety data = kill (fail closed)."""
    def __init__(self, seen=None):
        self.cache = {}  # mint -> time of last kill, avoids re-hammering APIs
        self.seen = {} if seen is None else seen  # mint -> first price + holders, for wallet learning

    def run(self, cand, now=None):
        now = now or time.time()
        if now - self.cache.get(cand["mint"], 0) < CFG["kill_cache_s"]:
            return None
        # cheap DexScreener kills first: dead/thin pairs never cost a RugCheck call
        pre = [x for x in self.check(cand, None)[0] if x != "rugcheck unavailable"]
        rc = None if pre else get(f"{RC}/tokens/{cand['mint']}/report")
        reasons, facts = self.check(cand, rc)
        reasons = pre or reasons
        price = f(cand["pair"].get("priceUsd"))
        if rc and price > 0 and cand["mint"] not in self.seen:
            self.seen[cand["mint"]] = {"p": price, "t": now, "w": facts.get("owners") or []}
        if reasons:
            if not any("unavailable" in x for x in reasons):  # missing data: kill now, retry next scan
                self.cache[cand["mint"]] = now
            Logger.log("kill", agent="Filter", mint=cand["mint"], symbol=cand.get("symbol"), reasons=reasons, facts={k: v for k, v in facts.items() if k != "owners"})
            return None
        return handoff({**cand, "rc": rc, **facts}, ["mint", "pair", "rc", "holders"], "Filter", "Analyst")

    @staticmethod
    def check(cand, rc):
        p, r, fx = cand["pair"], [], {}
        liq, vol = f(p.get("liquidity", {}).get("usd")), f(p.get("volume", {}).get("h1"))
        ch1 = f(p.get("priceChange", {}).get("h1"))
        fx.update(liq=liq, vol1h=vol, change1h=ch1)
        if liq < CFG["liq_min"]: r.append(f"liq ${liq:,.0f} < ${CFG['liq_min']:,}")
        if vol < CFG["vol1h_min"]: r.append(f"1h vol ${vol:,.0f} < ${CFG['vol1h_min']:,}")
        if ch1 > CFG["pump1h_max"]: r.append(f"already +{ch1:.0f}% in 1h")
        if not rc:
            return r + ["rugcheck unavailable"], fx
        tok = rc.get("token") or {}
        if rc.get("mintAuthority") or tok.get("mintAuthority"): r.append("mint authority not revoked")
        if rc.get("freezeAuthority") or tok.get("freezeAuthority"): r.append("freeze authority not revoked")
        lp = max([f((m.get("lp") or {}).get("lpLockedPct")) for m in rc.get("markets") or []] or [0])
        fx["lp_locked_pct"] = lp
        if lp < CFG["lp_locked_min"]: r.append(f"LP burned/locked {lp:.0f}% < {CFG['lp_locked_min']}%")
        known = set(rc.get("knownAccounts") or {})  # AMM pools, lockers: not real holders
        holders = [h for h in rc.get("topHolders") or [] if h.get("address") not in known and h.get("owner") not in known]
        top10 = sum(f(h.get("pct")) for h in holders[:10])
        creator = rc.get("creator")
        dev = sum(f(h.get("pct")) for h in holders if creator and creator in (h.get("owner"), h.get("address")))
        fx.update(top10_pct=top10, dev_pct=dev, holders=rc.get("totalHolders") or 0,
                  owners=[h.get("owner") or h.get("address") for h in holders  # insiders/dev are not "smart"
                          if not h.get("insider") and creator not in (h.get("owner"), h.get("address"))])
        if not holders: r.append("no holder data")
        if top10 > CFG["top10_max"]: r.append(f"top10 {top10:.1f}% > {CFG['top10_max']}%")
        if dev > CFG["dev_max"]: r.append(f"dev holds {dev:.1f}% > {CFG['dev_max']}%")
        if rc.get("rugged") or any("rug" in (x.get("name") or "").lower() for x in rc.get("risks") or []):
            r.append("dev/token rug history")
        # shortcut: bundle % = supply held by RugCheck-flagged insiders (trench.bot never answered); swap for a real bundle API if one works
        if holders and not any("insider" in h for h in holders): r.append("insider data unavailable")
        b = fx["insider_pct"] = sum(f(h.get("pct")) for h in holders if h.get("insider"))
        if b > CFG["bundle_max"]: r.append(f"insiders/bundle {b:.1f}% > {CFG['bundle_max']}%")
        return r, fx


def is_wallet(s):
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", s))  # Solana base58 address


def load_smart_wallets(auto=True):
    """Manual list (dashboard) + wallets the desk learned itself (smart_wallets_auto.txt)."""
    out = set()
    for name in ("smart_wallets.txt", "smart_wallets_auto.txt") if auto else ("smart_wallets.txt",):
        path = os.path.join(DIR, name)
        if os.path.exists(path):
            out |= {l.strip() for l in open(path) if is_wallet(l.strip())}
    return out


def learn_wallets(st, now=None):
    """Coin we saw went 2x+ within 72h -> its top holders (non-insider) at first sight get a win.
    2+ wins = smart. shortcut: top holders only (RugCheck), not every early buyer; use a tx indexer for that."""
    now = now or time.time()
    seen, wins = st.setdefault("seen", {}), st.setdefault("wins", {})
    for m in [m for m, v in seen.items() if now - v["t"] > 72 * 3600]:
        del seen[m]
    mints = list(seen)
    for i in range(0, len(mints), 30):
        best = {}
        for p in get(f"{DS}/tokens/v1/solana/{','.join(mints[i:i + 30])}") or []:
            m = p["baseToken"]["address"]
            if f(p.get("liquidity", {}).get("usd")) >= best.get(m, (0, -1))[1]:
                best[m] = (f(p.get("priceUsd")), f(p.get("liquidity", {}).get("usd")))
        for m, (px, _) in best.items():
            v = seen.get(m)
            if not v or px <= 0:
                continue
            v["hi"], v["lo"] = max(v.get("hi", v["p"]), px), min(v.get("lo", v["p"]), px)
            won = px >= 2 * v["p"]
            if "s" in v and not v.get("o") and (won or now - v["t"] >= 3600):
                v["o"] = 1  # one outcome row per scored coin: did the score predict the next hour?
                Logger.log("outcome", mint=m, symbol=v.get("sym"), score=v["s"], parts=v["parts"],
                           ret=round(px / v["p"] - 1, 3), hi=round(v["hi"] / v["p"] - 1, 3), lo=round(v["lo"] / v["p"] - 1, 3))
            if won:
                del seen[m]
                for w in v["w"]:
                    wins[w] = wins.get(w, 0) + 1
                Logger.log("learn", mint=m, x=round(px / v["p"], 1), holders=len(v["w"]))
    smart = sorted(w for w, n in wins.items() if n >= 2 and is_wallet(w))
    tmp = os.path.join(DIR, "smart_wallets_auto.txt.tmp")
    open(tmp, "w").write("\n".join(smart) + "\n")
    os.replace(tmp, os.path.join(DIR, "smart_wallets_auto.txt"))
    return smart


def smart_holders(mint, wallets):
    """On-chain: which smart wallets currently hold this token (Solana RPC)."""
    hits = set()
    for w in wallets:
        r = get(RPC, {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                      "params": [w, {"mint": mint}, {"encoding": "jsonParsed"}]})
        accts = ((r or {}).get("result") or {}).get("value") or []
        if any(f(a["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount")) > 0 for a in accts):
            hits.add(w)
        time.sleep(0.12 if HELIUS else 0.15)  # Helius free: 10 req/s; public RPC: ~100 req/10s
    return hits


_cg_used = {}  # UTC day -> CoinGecko Demo calls made


def gecko(path):
    """GeckoTerminal data, via the CoinGecko Demo key while today's share of its 10k/month lasts, else keyless.
    shortcut: the day counter lives in memory, so a restart re-grants the day's budget; persist it if restarts get frequent."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if CG_KEY and _cg_used.get(day, 0) < CFG["cg_calls_day"]:
        _cg_used[day] = _cg_used.get(day, 0) + 1
        return get(CG + path, headers={"x-cg-demo-api-key": CG_KEY})
    return get(GT + path)


def unique_buyers(pair_addr):
    """GeckoTerminal: unique buyer wallets in the last hour (DexScreener only has txn counts). None if unknown."""
    a = ((gecko(f"/pools/{pair_addr}") or {}).get("data") or {}).get("attributes") or {}
    return ((a.get("transactions") or {}).get("h1") or {}).get("buyers")


class Analyst:
    """0-100 score. Only >= score_min passes."""
    W = dict(momentum=20, buyers=15, buy_sell=20, holder_growth=15, liq_mcap=15, smart=15)

    def __init__(self, seen=None):
        self.seen = {} if seen is None else seen  # shared with Filter: first score is kept for outcome tracking

    def run(self, c):
        smart = load_smart_wallets()  # re-read: list is edited from the dashboard
        hits = (smart & set(c.get("owners") or [])) | (smart_holders(c["mint"], smart) if smart else set())
        s = self.score(c, hits, unique_buyers(c["pair"].get("pairAddress")))
        total = round(sum(self.W[k] * v for k, v in s.items()), 1)
        v = self.seen.get(c["mint"])
        if v is not None and "s" not in v:
            v.update(s=total, parts={k: round(x, 2) for k, x in s.items()}, sym=c.get("symbol"))
        if total < CFG["score_min"]:
            Logger.log("kill", agent="Analyst", mint=c["mint"], symbol=c.get("symbol"),
                       reasons=[f"score {total} < {CFG['score_min']}"], facts=s)
            return None
        Logger.log("signal", mint=c["mint"], symbol=c.get("symbol"), score=total, parts=s, smart_wallets=sorted(hits))
        return handoff({**c, "score": total, "price": f(c["pair"].get("priceUsd"))}, ["mint", "score", "price"], "Analyst", "Risk")

    @staticmethod
    def score(c, hits, buyers=None):
        p = c["pair"]
        h1, m5 = p.get("txns", {}).get("h1", {}), p.get("priceChange", {})
        buys, sells = f(h1.get("buys")), f(h1.get("sells"))
        return dict(
            momentum=clip(f(m5.get("h1")) / 40) * 0.7 + clip(f(m5.get("m5")) / 10) * 0.3,
            # 300+ unique buyer wallets/h = full marks; buy-txn count only as fallback when GeckoTerminal is down
            buyers=clip(f(buyers) / 300) if buyers is not None else clip(buys / 1000),
            buy_sell=clip((buys / max(sells, 1) - 1) / 1.0),  # 2:1 buys = full marks
            holder_growth=clip(c["holders"] / max(c["age_h"], 0.5) / 200),  # holders gained per hour since launch
            liq_mcap=clip(f(p.get("liquidity", {}).get("usd")) / max(c["mcap"], 1) / 0.15),
            smart=clip(len(hits) / 3),  # 3+ smart wallets holding = full marks
        )


def bars_1m(pair_addr, limit=240):
    """1-minute OHLCV, oldest first: [(o, h, l, c), ...]. None if unavailable.
    With a CoinGecko Demo key the call skips the shared keyless GeckoTerminal pool (the 429s)."""
    rows = (((gecko(f"/pools/{pair_addr}/ohlcv/minute?aggregate=1&limit={limit}") or {})
             .get("data") or {}).get("attributes") or {}).get("ohlcv_list")
    return [tuple(f(x) for x in r[1:5]) for r in sorted(rows, key=lambda r: r[0])] if rows else None


def time_machine(bars, stop_pct, arm_pct, horizon, replays, seed):
    """trader-timemachine's method on memecoin 1-minute bars: rebuild the next `horizon` minutes `replays` times
    from the coin's own bars (each bar as open/high/low/close over the previous close), count which comes first:
    the stop or the +arm_pct that arms the trail. Both inside one bar = stop (conservative)."""
    pool = [(o / pc, h / pc, l / pc, c / pc) for (_, _, _, pc), (o, h, l, c) in zip(bars, bars[1:]) if pc > 0]
    rng, stop, arm, cnt = random.Random(seed), 1 - stop_pct, 1 + arm_pct, {"stop": 0, "arm": 0, "none": 0}
    for _ in range(replays):
        p, out = 1.0, "none"
        for _ in range(horizon):
            _, h, l, c = rng.choice(pool)
            if p * l <= stop:
                out = "stop"; break
            if p * h >= arm:
                out = "arm"; break
            p *= c
        cnt[out] += 1
    return {k: round(v / replays, 3) for k, v in cnt.items()}


class Risk:
    """Sets the stop BEFORE entry; manages stops/trailing after."""
    def recheck(self, sig, now=None):
        """The signal is minutes old by now (RugCheck, RPC...): refetch the pair, kill if the picture changed."""
        fresh = Scout.select(get(f"{DS}/tokens/v1/solana/{sig['mint']}") or [], now or time.time())
        reasons = ["no fresh price"] if not fresh else \
            [x for x in Filter.check(fresh[0], None)[0] if x != "rugcheck unavailable"]
        if fresh:
            px = f(fresh[0]["pair"].get("priceUsd"))
            drop = 1 - px / sig["price"]
            if px <= 0 or drop > CFG["entry_drop_max"]:
                reasons.append(f"price fell {drop * 100:.1f}% since signal")
        tm = None
        if not reasons:
            bars = bars_1m(fresh[0]["pair"].get("pairAddress"))
            if not bars or len(bars) < 30:
                reasons.append("time machine unavailable (too few 1m bars)")
            else:
                # costs are paid at entry, so price must fall less than stop_pct to hit the stop
                liq = f(fresh[0]["pair"].get("liquidity", {}).get("usd"))
                cost = slip(min(CFG["size_pct"] * CFG["start_equity"], liq * CFG["impact_max"] / 2), liq)  # same size plan() buys
                tm = time_machine(bars, CFG["stop_pct"] - cost, CFG["trail_arm_pct"] + cost, CFG["tm_horizon_min"],
                                  CFG["tm_replays"], zlib.crc32(sig["mint"].encode()))
                if tm["stop"] > CFG["tm_stop_first_max"]:
                    reasons.append(f"time machine: stop first {tm['stop'] * 100:.0f}% of {CFG['tm_replays']:,} replays")
        if reasons:
            Logger.log("kill", agent="Risk", mint=sig["mint"], symbol=sig.get("symbol"), reasons=reasons, facts={"tm": tm})
            return None
        Logger.log("timemachine", mint=sig["mint"], symbol=sig.get("symbol"), **tm)
        return {**sig, "price": px, "liq": f(fresh[0]["pair"].get("liquidity", {}).get("usd")), "tm": tm}

    @staticmethod
    def limits(st, eq, now=None):
        """trader-risk's day limits. Returns a reason to block new buys, or None. Selling is never blocked."""
        day = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
        d = st.get("day") or {}
        if d.get("date") != day:
            d = st["day"] = {"date": day, "start_eq": eq, "buys": 0}
        if eq < d["start_eq"] * (1 - CFG["day_loss_max"]):
            return f"daily loss limit: {(eq / d['start_eq'] - 1) * 100:.1f}% today"
        if len(st["pos"]) >= CFG["max_open"]:
            return f"max {CFG['max_open']} open positions"
        if d["buys"] >= CFG["max_buys_day"]:
            return f"max {CFG['max_buys_day']} buys today"
        return None

    def plan(self, sig, equity, cash):
        # size down in thin pools so our own order moves the price by at most impact_max
        size = min(equity * CFG["size_pct"], cash, sig["liq"] * CFG["impact_max"] / 2)
        if size < 1:
            Logger.log("kill", agent="Risk", mint=sig["mint"], reasons=["no cash"])
            return None
        entry = sig["price"] * (1 + slip(size, sig["liq"]))
        return handoff({"mint": sig["mint"], "symbol": sig.get("symbol"), "usd": size, "entry": entry,
                        "stop": entry * (1 - CFG["stop_pct"]), "score": sig["score"], "liq": sig["liq"], "tm": sig.get("tm")},
                       ["mint", "usd", "entry", "stop", "liq"], "Risk", "Executor")

    @staticmethod
    def update(pos, price, now=None):
        """Return exit reason or None; mutates peak/stop."""
        pos["peak"] = max(pos["peak"], price)
        if pos["peak"] >= pos["entry"] * (1 + CFG["trail_arm_pct"]):
            pos["stop"] = max(pos["stop"], pos["peak"] * (1 - CFG["trail_pct"]))
            pos["trailing"] = True
        if price <= pos["stop"]:
            return "trail_stop" if pos.get("trailing") else "stop_loss"
        if not pos.get("trailing") and (now or time.time()) - pos.get("opened", now or time.time()) >= CFG["time_exit_h"] * 3600:
            return "time_exit"  # never reached +12% in time_exit_h: free the cash
        return None


class Executor:
    """Paper fills only. Refuses orders without a stop or above 8% of equity."""
    def __init__(self, st):
        self.st = st

    def buy(self, o):
        assert o["stop"] < o["entry"], "stop must be below entry"
        assert o["usd"] <= equity(self.st) * CFG["size_pct"] + 1e-6, "size over 8% cap"
        self.st["cash"] -= o["usd"]
        self.st["pos"][o["mint"]] = {**o, "qty": o["usd"] / o["entry"], "peak": o["entry"], "last": o["entry"],
                                     "opened": time.time()}
        self.st.setdefault("day", {}).setdefault("buys", 0)
        self.st["day"]["buys"] += 1
        Logger.log("trade", side="buy", **{k: v for k, v in o.items() if k != "tm"})
        notify(self.st, f"COMPRA {o.get('symbol')} ${o['usd']:,.0f} voto {o['score']}"
                        + (f" | time machine: stop prima {o['tm']['stop'] * 100:.0f}%" if o.get("tm") else ""))

    def sell(self, mint, price, why):
        p = self.st["pos"].pop(mint)
        fill = price * (1 - slip(p["qty"] * price, p.get("liq") or 1e12))
        proceeds = p["qty"] * fill
        self.st["cash"] += proceeds
        self.st["closed"].append(mint)
        Logger.log("trade", side="sell", mint=mint, symbol=p.get("symbol"), reason=why, entry=p["entry"], exit=fill,
                   pnl_usd=round(proceeds - p["usd"], 2), pnl_pct=round((fill / p["entry"] - 1) * 100, 2),
                   cash=round(self.st["cash"], 2))
        notify(self.st, f"VENDE {p.get('symbol')} {(fill / p['entry'] - 1) * 100:+.1f}% (${proceeds - p['usd']:+,.0f}) {why}")


def equity(st):
    return st["cash"] + sum(p["qty"] * p["last"] for p in st["pos"].values())


def load_state():
    st = json.load(open(STATE)) if os.path.exists(STATE) else {"cash": CFG["start_equity"], "pos": {}, "closed": []}
    st.setdefault("ntfy", "mrtrader-" + secrets.token_hex(8))  # private push topic, shown in the dashboard
    return st


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)  # atomic: no corrupted state if killed mid-write


def monitor(st, ex):
    mints = list(st["pos"])
    for i in range(0, len(mints), 30):
        prices = {}
        for p in get(f"{DS}/tokens/v1/solana/{','.join(mints[i:i + 30])}") or []:
            m = p["baseToken"]["address"]
            if f(p.get("liquidity", {}).get("usd")) >= prices.get(m, (0, -1))[1]:
                prices[m] = (f(p.get("priceUsd")), f(p.get("liquidity", {}).get("usd")))
        for m, (px, liq) in prices.items():
            if m in st["pos"] and px > 0:
                st["pos"][m].update(last=px, liq=liq)
                why = Risk.update(st["pos"][m], px)
                if why:
                    ex.sell(m, px, why)


def main(once=False):
    st = load_state()
    seen = st.setdefault("seen", {})
    scout, filt, analyst, risk = Scout(), Filter(seen), Analyst(seen), Risk()
    ex, last_scan, last_learn, last_watch = Executor(st), 0, 0, [0]

    def watch():  # stops must be checked every tick_s even in the middle of a slow scan
        if time.time() - last_watch[0] >= CFG["tick_s"]:
            last_watch[0] = time.time()
            monitor(st, ex)

    Logger.log("start", mode="paper", helius=bool(HELIUS), coingecko=bool(CG_KEY), equity=round(equity(st), 2), cfg=CFG)
    while True:
        watch()
        if time.time() - last_learn >= 300:
            last_learn = time.time()
            Logger.log("smart_wallets", learned=len(learn_wallets(st)), watching=len(st["seen"]))
        if time.time() - last_scan >= CFG["scan_every_s"]:
            last_scan = time.time()
            cands = scout.run(tick=watch)
            Logger.log("scan", candidates=len(cands))
            for c in cands:
                watch()
                if c["mint"] in st["pos"] or c["mint"] in st["closed"]:
                    continue
                c = filt.run(c)
                sig = c and analyst.run(c)
                if c and not sig:
                    filt.cache[c["mint"]] = time.time()  # low score: rescore after kill_cache_s, not every scan
                block = sig and Risk.limits(st, equity(st))
                if block:
                    Logger.log("kill", agent="Risk", mint=sig["mint"], symbol=sig.get("symbol"), reasons=[block])
                    sig = None
                sig = sig and risk.recheck(sig)
                order = sig and risk.plan(sig, equity(st), st["cash"])
                if order:
                    ex.buy(order)
            Logger.log("equity", equity=round(equity(st), 2), cash=round(st["cash"], 2), open=len(st["pos"]))
        save_state(st)
        if once:
            return st
        time.sleep(CFG["tick_s"])


if __name__ == "__main__":
    main(once="--once" in sys.argv)
