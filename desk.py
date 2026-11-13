"""Solana memecoin paper desk: Scout -> Filter -> Analyst -> Risk -> Executor, Logger sees all.

Run:  python desk.py            (loops forever, paper mode)
      python desk.py --once     (one cycle)
Data: DexScreener + GeckoTerminal (pairs/metrics), RugCheck (authorities, LP, holders, insiders).
"""
import json, os, sys, time, urllib.request

CFG = dict(
    age_min_h=0.5, age_max_h=72, mcap_min=300_000, mcap_max=20_000_000,
    liq_min=40_000, vol1h_min=50_000, top10_max=25, dev_max=3, bundle_max=10,
    pump1h_max=80, lp_locked_min=90, score_min=70,
    size_pct=0.08, stop_pct=0.06, trail_arm_pct=0.12, trail_pct=0.06,
    slippage=0.01, start_equity=10_000, scan_every_s=120, tick_s=20, kill_cache_s=1800,
)
DS, RC = "https://api.dexscreener.com", "https://api.rugcheck.xyz/v1"
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
DIR = os.path.dirname(os.path.abspath(__file__))
LOGS, STATE = os.path.join(DIR, "logs"), os.path.join(DIR, "state.json")


def get(url):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-desk/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except Exception as e:
            err = e
            time.sleep(1 + 2 * i)
    Logger.log("http_error", url=url, error=str(err))
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


class Logger:
    @staticmethod
    def log(kind, **data):
        os.makedirs(LOGS, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **data}
        with open(os.path.join(LOGS, f"{kind if kind in ('kill', 'trade') else 'events'}.jsonl"), "a") as fh:
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
    def run(self, now=None):
        now = now or time.time()
        mints = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1",
                     "/community-takeovers/latest/v1"):
            for t in get(DS + path) or []:
                if t.get("chainId") == "solana" and t.get("tokenAddress") not in mints:
                    mints.append(t["tokenAddress"])
        for path in ("new_pools", "trending_pools"):
            for page in (1, 2, 3):
                time.sleep(2.5)  # GeckoTerminal free tier rate-limits bursts (429)
                for m in gecko_mints(get(f"{GT}/{path}?page={page}")):
                    if m not in mints:
                        mints.append(m)
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
    def __init__(self):
        self.cache = {}  # mint -> time of last kill, avoids re-hammering APIs

    def run(self, cand, now=None):
        now = now or time.time()
        if now - self.cache.get(cand["mint"], 0) < CFG["kill_cache_s"]:
            return None
        rc = get(f"{RC}/tokens/{cand['mint']}/report")
        reasons, facts = self.check(cand, rc)
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
                  owners=[h.get("owner") or h.get("address") for h in holders])
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


def load_smart_wallets():
    path = os.path.join(DIR, "smart_wallets.txt")
    if not os.path.exists(path):
        return set()
    return {l.strip() for l in open(path) if l.strip() and not l.startswith("#")}


class Analyst:
    """0-100 score. Only >= score_min passes."""
    W = dict(momentum=20, buyers=15, buy_sell=20, holder_growth=15, liq_mcap=15, smart=15)

    def __init__(self):
        self.smart = load_smart_wallets()

    def run(self, c):
        s = self.score(c, self.smart)
        total = round(sum(self.W[k] * v for k, v in s.items()), 1)
        if total < CFG["score_min"]:
            Logger.log("kill", agent="Analyst", mint=c["mint"], symbol=c.get("symbol"),
                       reasons=[f"score {total} < {CFG['score_min']}"], facts=s)
            return None
        Logger.log("signal", mint=c["mint"], symbol=c.get("symbol"), score=total, parts=s)
        return handoff({**c, "score": total, "price": f(c["pair"].get("priceUsd"))}, ["mint", "score", "price"], "Analyst", "Risk")

    @staticmethod
    def score(c, smart):
        p = c["pair"]
        h1, m5 = p.get("txns", {}).get("h1", {}), p.get("priceChange", {})
        buys, sells = f(h1.get("buys")), f(h1.get("sells"))
        return dict(
            momentum=clip(f(m5.get("h1")) / 40) * 0.7 + clip(f(m5.get("m5")) / 10) * 0.3,
            # shortcut: DexScreener gives buy txn count, not unique buyers; swap for an indexer (Helius/Birdeye) if keyed
            buyers=clip(buys / 1000),
            buy_sell=clip((buys / max(sells, 1) - 1) / 1.0),  # 2:1 buys = full marks
            holder_growth=clip(c["holders"] / max(c["age_h"], 0.5) / 200),  # holders gained per hour since launch
            liq_mcap=clip(f(p.get("liquidity", {}).get("usd")) / max(c["mcap"], 1) / 0.15),
            smart=clip(len(smart & set(c.get("owners") or [])) / 3),
        )


class Risk:
    """Sets the stop BEFORE entry; manages stops/trailing after."""
    def plan(self, sig, equity, cash):
        size = min(equity * CFG["size_pct"], cash)
        if size < 1:
            Logger.log("kill", agent="Risk", mint=sig["mint"], reasons=["no cash"])
            return None
        entry = sig["price"] * (1 + CFG["slippage"])
        return handoff({"mint": sig["mint"], "symbol": sig.get("symbol"), "usd": size, "entry": entry,
                        "stop": entry * (1 - CFG["stop_pct"]), "score": sig["score"]},
                       ["mint", "usd", "entry", "stop"], "Risk", "Executor")

    @staticmethod
    def update(pos, price):
        """Return exit reason or None; mutates peak/stop."""
        pos["peak"] = max(pos["peak"], price)
        if pos["peak"] >= pos["entry"] * (1 + CFG["trail_arm_pct"]):
            pos["stop"] = max(pos["stop"], pos["peak"] * (1 - CFG["trail_pct"]))
            pos["trailing"] = True
        if price <= pos["stop"]:
            return "trail_stop" if pos.get("trailing") else "stop_loss"
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
        Logger.log("trade", side="buy", **o)

    def sell(self, mint, price, why):
        p = self.st["pos"].pop(mint)
        fill = price * (1 - CFG["slippage"])
        proceeds = p["qty"] * fill
        self.st["cash"] += proceeds
        self.st["closed"].append(mint)
        Logger.log("trade", side="sell", mint=mint, symbol=p.get("symbol"), reason=why, entry=p["entry"], exit=fill,
                   pnl_usd=round(proceeds - p["usd"], 2), pnl_pct=round((fill / p["entry"] - 1) * 100, 2),
                   cash=round(self.st["cash"], 2))


def equity(st):
    return st["cash"] + sum(p["qty"] * p["last"] for p in st["pos"].values())


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"cash": CFG["start_equity"], "pos": {}, "closed": []}


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
        for m, (px, _) in prices.items():
            if m in st["pos"] and px > 0:
                st["pos"][m]["last"] = px
                why = Risk.update(st["pos"][m], px)
                if why:
                    ex.sell(m, px, why)


def main(once=False):
    st, scout, filt, analyst, risk = load_state(), Scout(), Filter(), Analyst(), Risk()
    ex, last_scan = Executor(st), 0
    Logger.log("start", mode="paper", equity=round(equity(st), 2), cfg=CFG)
    while True:
        monitor(st, ex)
        if time.time() - last_scan >= CFG["scan_every_s"]:
            last_scan = time.time()
            cands = scout.run()
            Logger.log("scan", candidates=len(cands))
            for c in cands:
                if c["mint"] in st["pos"] or c["mint"] in st["closed"]:
                    continue
                c = filt.run(c)
                sig = c and analyst.run(c)
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
