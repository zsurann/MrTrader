"""
MemeDesk v9 SHADOW rules (LOG ONLY). Nothing here ever opens, closes or resizes a live paper position.

Ghost tracker: every live entry spawns one hypothetical copy ("ghost") per exit variant, with the same entry
(v14: while the live position is open, exit-variant ghosts also get every live price tick -- see GhostBook.on_tick)
fill, size and qty. Ghosts are priced from batched DexScreener pair quotes every `poll_s` seconds and live on
after the real position closes (up to `horizon_h`), so ladders / moonbags / wide trails are measured to their own
end instead of being cut by the live exit. Exit fills use the same paper fill as the live desk (Jupiter quote
with the live sanity band, else the slippage model). `ghost_live_baseline` replays the LIVE exit rules at the
same sampling cadence, so every variant should be compared with that baseline (same sampling bias).

Shadow entries: candidates the live desk did NOT enter but a v9 entry idea would have (they passed every
Filter safety check). They get a ghost with the live exit rules, so their hypothetical PnL is comparable.

Entry gates (would_block flags on live entries) are written by desk.shadow_entry_gate via `entry_gates()`.
"""
import json, math, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor

V9 = dict(poll_s=30, horizon_h=24.0, max_entry_ghosts=60, entry_cooldown_h=1.0, sample_keep=20)

# Exit variants (parameters are fractions of the market reference price at entry, like the live rules).
EXIT_V9 = {
    "ghost_live_baseline": dict(stop=0.10, activate=0.12, dist=0.04, be_arm=0.08, be_lock=0.01),   # = LIVE_EXIT; v14: fed the live price ticks (Helius)
    # v14: the same live rules priced ONLY by the 30 s DexScreener ghost poll (the pre-v14 ghost cadence). Its drift vs live
    # measures the sampling bias that shadow ENTRY ghosts still have (they have no live position, so no Helius ticks).
    "ghost_live_poll30":   dict(stop=0.10, activate=0.12, dist=0.04, be_arm=0.08, be_lock=0.01),
    "tp2x_half_trail25":   dict(stop=0.10, tp_x=2.0, tp_frac=0.50, trail=0.25),            # take initials at 2x, rest trails 25%
    "ladder_2x4x_trail18": dict(stop=0.30, steps=((2.0, 0.50), (4.0, 0.25)), trail=0.18),  # -30% stop, 50% @2x, 25% @4x, rest 18% under high (never below entry)
    "profit_lock":         dict(stop=0.10, locks=((0.5, 0.0), (1.0, 0.5)), trail_after=3.0, trail=0.40),
    "time_stop_15m":       dict(stop=0.10, activate=0.12, dist=0.06, minutes=15, min_gain=0.05),
    "be20_trail15":        dict(stop=0.10, be_arm=0.20, be_lock=0.02, trail_arm=0.40, trail=0.15),
    "chandelier_3atr":     dict(stop=0.10, n=14, mult=3.0),
    "momentum_wide_trail": dict(stop=0.10, activate=0.12, dist=0.06, wide_dist=0.12, h1_min=30.0, m5_min=5.0),
    # v10 shadow ideas from public memecoin exit guides (2026-11-19 web search; vendor heuristics, never backtested):
    "tranche_25_50_100":   dict(stop=0.10, steps=((1.25, 0.25), (1.5, 0.25), (2.0, 0.25)), trail=0.10),  # scale out in quarters, rest trails 10%
    "stale_3h":            dict(stop=0.10, activate=0.12, dist=0.04, be_arm=0.08, be_lock=0.01, minutes=180, min_gain=0.05),  # live rules + out after 3h if < +5%
    "wide_stop50_trail20": dict(stop=0.50, activate=0.30, dist=0.20),   # memecoins dip before pumping: -50% stop, 20% trail from +30%
    # "fomo" TikTok bot (2026-11-19 video, screenshots only): Risk card shows stop -6%; take-profits seen +8%..+171%.
    # shortcut: the trail (10% from +15%) is our guess of their take-profit logic, refine if their rules ever become public.
    "fomo_stop6_trail10":  dict(stop=0.06, activate=0.15, dist=0.10),
    # "Grok Bot" TikTok video (same app): cards read "stop -6%", "trail +12%", "exit if it drops 12% from top", size 8% of stack.
    "grok_stop6_trail12":  dict(stop=0.06, activate=0.12, dist=0.12),
    # v12: limit buy 2% under the live entry's market price, valid 10 min; if filled, live exit rules from the limit price.
    # Not filled = no trade (PnL 0), so it is compared with the live trade it would have replaced.
    "limit_entry_2pct":    dict(lim=0.02, wait_min=10, stop=0.10, activate=0.12, dist=0.04, be_arm=0.08, be_lock=0.01),
    # v14: live exit rules, but the exit sells 50% at the trigger and 50% one ghost poll (~30 s) later (smaller impact per swap)
    "split_exit_2":        dict(stop=0.10, activate=0.12, dist=0.04, be_arm=0.08, be_lock=0.01, gap_s=30),
    # v15: v11 trades peaked at only +5% on average, so the live trail (+12%) and breakeven (+8%) rarely arm
    "trail_early_6_3":     dict(stop=0.10, activate=0.06, dist=0.03),
    "be_early_4":          dict(stop=0.10, activate=0.12, dist=0.04, be_arm=0.04, be_lock=0.005),
}
# Shadow entry ideas (ghost uses the live exit rules: LIVE_EXIT).
ENTRY_V9 = {
    "dip_after_pump": dict(h6_min=50.0, h1_max=-10.0, h1_min=-35.0, m5_min=0.0),
    "score_45_55":    dict(lo=45.0, hi=55.0),
    "migration_dip":  dict(max_since_mig_h=6.0, h1_max=-20.0, m5_min=0.0),
    # v14 "pre-pump" ideas (the live desk buys after +18% 1h median and peaks at +3%): coins still flat with early activity.
    # All need h1 in [-5%, +5%) and m5 in [-3%, +3%] (no 5m spike); they come from Filter passes, so every safety check passed.
    "prepump_vol":    dict(h1_lo=-5.0, h1_hi=5.0, m5_lo=-3.0, m5_hi=3.0, vol_rate_x=2.0, min_b5=5),          # 5m volume rate >= 2x the 1h rate, buys >= sells (5m)
    "prepump_buys":   dict(h1_lo=-5.0, h1_hi=5.0, m5_lo=-3.0, m5_hi=3.0, buy_share_5m=0.60, r_h1=1.2, min_b5=5),  # >= 60% buys (5m) and buys/sells >= 1.2 (1h)
    "prepump_both":   dict(h1_lo=-5.0, h1_hi=5.0, m5_lo=-3.0, m5_hi=3.0),                                     # both signals at once
}
# v14: retired rules (consistently worst, or never blocking anything). No new ghosts / events; history stays in the DB and
# open ghosts of these rules finish with their own parameters (kept above).
RETIRED_V14 = {"ladder_2x4x_trail18", "profit_lock", "tp2x_half_trail25", "wide_stop50_trail20", "limit_entry_2pct", "trail_early_6_3",
               "copy_smart2", "score_45_55", "migration_dip", "dip_after_pump", "churn_50", "top1_10", "small_trades_200", "fomo_score_70"}
ACTIVE_EXIT = [r for r in EXIT_V9 if r not in RETIRED_V14]
POLL_ONLY = {"ghost_live_poll30"}   # never fed the live ticks
LIVE_EXIT = EXIT_V9["ghost_live_baseline"]   # v10: trail 4%, breakeven +1% after +8% (smart_exit not modeled)

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_ghosts(id INTEGER PRIMARY KEY, rule TEXT, kind TEXT, position_id INT, mint TEXT, symbol TEXT,
    pair TEXT, opened_ts REAL, opened_iso TEXT, ref REAL, entry_px REAL, qty REAL, size_usd REAL, status TEXT, state TEXT,
    closed_ts REAL, closed_iso TEXT, exit_reason TEXT, proceeds REAL, hyp_pnl_usd REAL, hyp_pnl_pct REAL, meta TEXT);
CREATE INDEX IF NOT EXISTS shadow_ghosts_status ON shadow_ghosts(status);
CREATE INDEX IF NOT EXISTS shadow_ghosts_rule ON shadow_ghosts(rule, kind);
"""

def now(): return time.time()
def iso(ts=None): return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or now()))

def entry_gates(c, ap, liq_hist, mig_ts):
    """-> {rule: (value, would_block or None, text)} for one live Analyst pass. Pure (no I/O)."""
    tx = c.txns or {}; pc = c.price_change or {}; vol = c.vol or {}
    b5 = (tx.get("m5") or {}).get("buys") or 0; s5 = (tx.get("m5") or {}).get("sells") or 0
    b1 = (tx.get("h1") or {}).get("buys") or 0; s1 = (tx.get("h1") or {}).get("sells") or 0
    inp = (ap.components.get("_inputs") or {})
    nb = ((inp.get("unique_buyers") or {}) if isinstance(inp.get("unique_buyers"), dict) else {}).get("net_buyers_1h")
    g = {}
    v5, v6 = vol.get("m5"), vol.get("h6")
    ratio = (v5 * 72 / v6) if (v5 is not None and v6) else None
    g["vol_spike_2x"] = (None if ratio is None else round(ratio, 2), None if ratio is None else ratio < 2.0,
                         "5-min volume rate >= 2x the 6h average rate (breakout on rising volume)")
    r5 = b5 / max(s5, 1); r1 = b1 / max(s1, 1)
    bp_block = not (r5 >= 1.2 and r1 >= 1.1 and (nb is None or nb > 0))
    g["buy_pressure"] = (dict(r_m5=round(r5, 2), r_h1=round(r1, 2), net_buyers_1h=nb), bp_block,
                         "buys/sells >= 1.2 (5m) and >= 1.1 (1h), Jupiter net buyers 1h > 0")
    m5 = pc.get("m5")
    sp = (s5 >= 1.5 * max(b5, 1)) or (m5 is not None and m5 <= -8)
    g["sell_spike_avoid"] = (dict(b5=b5, s5=s5, m5=m5), sp, "skip if 5m sells >= 1.5x buys or 5m price <= -8%")
    hist = liq_hist.get(c.mint) or []
    old = [l for t, l in hist if now() - t >= 20 * 60]
    if old and old[0]:
        ch = c.liq_usd / old[0] - 1
        g["liq_growth"] = (round(100 * ch, 1), ch < -0.05, "liquidity not down > 5% vs the oldest sample >= 20 min ago (<= 2h)")
    else:
        g["liq_growth"] = (None, None, "no liquidity history >= 20 min yet (undecidable)")
    hg = (inp.get("holder_growth") or {}) if isinstance(inp.get("holder_growth"), dict) else {}
    hv = hg.get("pct_per_h", hg.get("pct_1h"))
    g["holder_growth_pos"] = (hv, None if hv is None else hv <= 0, "holder count growing (tracked %/h or Jupiter 1h change > 0)")
    g["new_pair_6h"] = (c.age_h, c.age_h is None or c.age_h > 6.0, "only pairs <= 6h old (early window)")
    since = (now() - mig_ts) / 3600 if mig_ts else None
    g["migrated_24h"] = (None if since is None else round(since, 2), since is None or since > 24,
                         "pump.fun graduate (PumpPortal migration) within the last 24h")
    # v10 shadow gates (log only) from 2026 Solana memecoin papers / guides (see VM.md):
    ck = ap.fp.checks or {}
    t10 = ck.get("top10_pct_ex_pools")
    g["top10_15"] = (t10, None if t10 is None else t10 > 15.0, "top-10 holders <= 15% (MemeTrans: concentrated early holders = high risk)")
    v1 = vol.get("h1"); n1 = b1 + s1
    avg = (v1 / n1) if (v1 and n1) else None
    g["small_trades_200"] = (None if avg is None else round(avg, 1), None if avg is None else avg > 200.0,
                             "1h volume per trade <= $200 (MemeTrans: large per-buyer volume = risky launches)")
    to = (v1 / c.liq_usd) if (v1 is not None and c.liq_usd) else None
    g["turnover_03_3"] = (None if to is None else round(to, 2), None if to is None else not (0.3 <= to <= 3.0),
                          "1h volume / liquidity between 0.3 and 3 (active but not a frenzy)")
    h1c = pc.get("h1")
    g["dip_in_uptrend"] = (dict(h1=h1c, m5=m5), None if h1c is None or m5 is None else not (h1c >= 5.0 and m5 <= 0.0),
                           "1h up >= +5% and 5m <= 0 (buy the pullback, not the spike)")
    return g


def prepump_signals(c):
    """v14 SHADOW pre-pump signals for one Scout candidate. Pure. -> dict(vol=bool, buys=bool, values=dict)."""
    pc = c.price_change or {}; tx = c.txns or {}; vol = c.vol or {}
    h1, m5 = pc.get("h1"), pc.get("m5")
    b5 = (tx.get("m5") or {}).get("buys") or 0; s5 = (tx.get("m5") or {}).get("sells") or 0
    b1 = (tx.get("h1") or {}).get("buys") or 0; s1 = (tx.get("h1") or {}).get("sells") or 0
    v5, v1 = vol.get("m5"), vol.get("h1")
    E = ENTRY_V9["prepump_vol"]
    flat = h1 is not None and m5 is not None and E["h1_lo"] <= h1 < E["h1_hi"] and E["m5_lo"] <= m5 <= E["m5_hi"]
    rate = (v5 * 12 / v1) if (v5 and v1) else None
    sig_v = bool(flat and rate is not None and rate >= E["vol_rate_x"] and b5 >= E["min_b5"] and b5 >= s5)
    B = ENTRY_V9["prepump_buys"]
    share = b5 / (b5 + s5) if (b5 + s5) else None
    sig_b = bool(flat and share is not None and share >= B["buy_share_5m"] and b5 >= B["min_b5"] and b1 >= B["r_h1"] * max(s1, 1))
    return dict(vol=sig_v, buys=sig_b, values=dict(h1=h1, m5=m5, vol_rate_5m_vs_1h=None if rate is None else round(rate, 2),
                                                    buy_share_5m=None if share is None else round(share, 3), b5=b5, s5=s5, b1=b1, s1=s1))


class GhostBook:
    def __init__(self, db, lock, jsonl, http_get, sell_fill, cfg, log_event, shadow_event):
        self.db, self.lock, self.jsonl = db, lock, jsonl
        self.get, self.sell_fill, self.cfg, self.ev, self.sev = http_get, sell_fill, cfg, log_event, shadow_event
        with self.lock: self.db.executescript(SCHEMA)
        self.open = {}   # id -> row dict
        with self.lock:
            cols = [d[1] for d in self.db.execute("PRAGMA table_info(shadow_ghosts)")]
            for r in self.db.execute("SELECT * FROM shadow_ghosts WHERE status='OPEN'"):
                d = dict(zip(cols, r)); d["state"] = json.loads(d["state"] or "{}"); d["meta"] = json.loads(d["meta"] or "{}")
                d["state"].pop("busy", None)   # v14: a fill in flight at shutdown is simply re-decided
                self.open[d["id"]] = d
        self.mlock = threading.Lock()
        self.glock = threading.RLock()   # v14: ghost decisions come from two threads (30 s poll + live price ticks)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ghost-fill")   # tick-path fills never block the live feed

    # ---------- creation
    def _insert(self, rule, kind, pid, mint, sym, pair, ref, entry_px, qty, size, meta):
        st = dict(peak=ref, rem=1.0, proceeds=0.0, steps=[], samples=[], cstop=None, checked=False, last_px=ref, last_ts=now())
        with self.lock:
            cur = self.db.execute("""INSERT INTO shadow_ghosts(rule,kind,position_id,mint,symbol,pair,opened_ts,opened_iso,ref,entry_px,qty,size_usd,
                status,state,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?)""",
                (rule, kind, pid, mint, sym, pair, now(), iso(), ref, entry_px, qty, size, json.dumps(st), json.dumps(meta, default=str)))
            gid = cur.lastrowid
        with self.mlock:
            self.open[gid] = dict(id=gid, rule=rule, kind=kind, position_id=pid, mint=mint, symbol=sym, pair=pair, opened_ts=now(),
                                  ref=ref, entry_px=entry_px, qty=qty, size_usd=size, state=st, meta=meta)
        self.sev(self.db, self.lock, self.jsonl, rule, "WATCH", mint, sym, pid, ghost_id=gid, ghost_kind=kind, ref=ref, entry_px=entry_px,
                 size_usd=round(size, 2), params=(EXIT_V9.get(rule) or ENTRY_V9.get(rule)), **{k: meta.get(k) for k in ("score", "score_bucket") if k in meta})
        return gid

    def on_live_entry(self, pid, order, ap):
        c = ap.fp.cand; pc = c.price_change or {}
        meta = dict(score=ap.score, chg_h1=pc.get("h1"), chg_m5=pc.get("m5"), liq=c.liq_usd, mcap=c.mcap, age_h=c.age_h)
        for rule in ACTIVE_EXIT:
            try: self._insert(rule, "exit_variant", pid, order.mint, order.symbol, order.pair_address, order.ref_price, order.fill_price,
                              order.qty, order.size_usd, meta)
            except Exception as e: self.ev("Shadow", "ERROR", rule=rule, error=repr(e)[:200])

    def consider_entries(self, fp, score, mig_ts, live_open_mints, equity):
        """Shadow entry ideas for a Filter-passed candidate the live desk did not enter."""
        c = fp.cand
        if c.mint in live_open_mints or not c.price_usd or not c.liq_usd: return
        pc = c.price_change or {}; h6, h1, m5 = pc.get("h6"), pc.get("h1"), pc.get("m5")
        hits, pp = [], prepump_signals(c)   # v14: dip_after_pump / score_45_55 / migration_dip retired (no new ghosts)
        if pp["vol"]: hits.append("prepump_vol")
        if pp["buys"]: hits.append("prepump_buys")
        if pp["vol"] and pp["buys"]: hits.append("prepump_both")
        if not hits: return
        with self.mlock:
            n_entry = sum(1 for g in self.open.values() if g["kind"] == "entry")
            open_keys = {(g["rule"], g["mint"]) for g in self.open.values() if g["kind"] == "entry"}
        for rule in hits:
            if n_entry >= V9["max_entry_ghosts"] or (rule, c.mint) in open_keys: continue
            if self.add_entry(rule, c.mint, c.symbol, c.pair_address, c.price_usd, c.liq_usd, equity,
                              dict(score=score, chg_h6=h6, chg_h1=h1, chg_m5=m5, liq=c.liq_usd, mcap=c.mcap, age_h=c.age_h, fill_src="model",
                                   prepump=pp.get("values"), pricing="dexscreener_poll_30s")):
                n_entry += 1

    def add_entry(self, rule, mint, sym, pair, ref, liq, equity, meta):
        """One shadow entry ghost (live exit rules) at the model fill. -> True if opened."""
        with self.mlock:
            if sum(1 for g in self.open.values() if g["kind"] == "entry") >= V9["max_entry_ghosts"]: return False
        with self.lock:
            last = self.db.execute("SELECT MAX(closed_ts) FROM shadow_ghosts WHERE rule=? AND mint=?", (rule, mint)).fetchone()[0]
        if last and now() - last < V9["entry_cooldown_h"] * 3600: return False
        size = min(self.cfg["max_pos_frac"] * equity, self.cfg["max_liq_frac"] * liq)
        if not ref or not liq or size < self.cfg.get("min_size_usd", 50): return False
        fill = ref * (1 + min(0.5, self.cfg["base_slippage"] + self.cfg["swap_fee"] + size / (liq / 2)))   # model fill (no Jupiter quote), capped (v8.2)
        fill *= 1 + self.cfg.get("net_fee_usd", 0) / size   # v11: network fee, like the live buy
        self._insert(rule, "entry", None, mint, sym, pair, ref, fill, size / fill, size, meta)
        return True

    # ---------- exit logic (pure: returns list of (fraction_of_original, reason), and whether the ghost is done)
    @staticmethod
    def decide(rule, kind, g, st, px, t):
        ref = g["ref"]; peak = st["peak"]; rem = st["rem"]; out = []
        P = EXIT_V9.get(rule, LIVE_EXIT) if kind == "exit_variant" else LIVE_EXIT
        def live_like(dist):
            stop = ref * (1 - P["stop"])
            if peak >= ref * (1 + P.get("activate", 0.12)): stop = max(stop, peak * (1 - dist))
            if P.get("be_arm") and peak >= g["entry_px"] * (1 + P["be_arm"]): stop = max(stop, g["entry_px"] * (1 + P["be_lock"]))
            return px <= stop
        if kind == "entry" or rule in ("ghost_live_baseline", "ghost_live_poll30", "wide_stop50_trail20", "fomo_stop6_trail10", "grok_stop6_trail12", "limit_entry_2pct",
                                            "trail_early_6_3", "be_early_4"):
            if live_like(P["dist"]): out.append((rem, "live_rules_stop"))
        elif rule == "tp2x_half_trail25":
            if "tp" not in st["steps"] and px >= ref * P["tp_x"]:
                out.append((P["tp_frac"], "tp_2x")); st["steps"].append("tp"); rem -= P["tp_frac"]
            if "tp" in st["steps"]:
                if px <= peak * (1 - P["trail"]) and rem > 1e-9: out.append((rem, "trail_25"))
            elif px <= ref * (1 - P["stop"]): out.append((rem, "stop_10"))
        elif rule == "split_exit_2":
            if st.get("split_ts"):
                if t - st["split_ts"] >= P["gap_s"]: out.append((rem, "split_2"))
            elif live_like(P["dist"]):
                out.append((0.5, "split_1")); st["split_ts"] = t
        elif rule in ("ladder_2x4x_trail18", "tranche_25_50_100"):
            for i, (x, f) in enumerate(P["steps"]):
                k = f"s{i}"
                if k not in st["steps"] and px >= ref * x:
                    out.append((f, f"tp_{x:g}x")); st["steps"].append(k); rem -= f
            if st["steps"]:
                if px <= max(ref, peak * (1 - P["trail"])) and rem > 1e-9: out.append((rem, f"trail_{100 * P['trail']:.0f}"))
            elif px <= ref * (1 - P["stop"]): out.append((rem, f"stop_{100 * P['stop']:.0f}"))
        elif rule == "profit_lock":
            pk = peak / ref - 1; lvl = ref * (1 - P["stop"])
            if pk >= P["trail_after"]: lvl = max(lvl, peak * (1 - P["trail"]))
            elif pk >= P["locks"][1][0]: lvl = max(lvl, ref * (1 + P["locks"][1][1]))
            elif pk >= P["locks"][0][0]: lvl = max(lvl, ref * (1 + P["locks"][0][1]))
            if px <= lvl: out.append((rem, "profit_lock"))
        elif rule in ("time_stop_15m", "stale_3h"):
            if not st["checked"] and t - g["opened_ts"] >= P["minutes"] * 60:
                st["checked"] = True
                if px < ref * (1 + P["min_gain"]): out.append((rem, f"time_{P['minutes']}m"))
            if not out and live_like(P["dist"]): out.append((rem, "live_rules_stop"))
        elif rule == "be20_trail15":
            stop = ref * (1 - P["stop"])
            if peak >= ref * (1 + P["be_arm"]): stop = max(stop, ref * (1 + P["be_lock"]))
            if peak >= ref * (1 + P["trail_arm"]): stop = max(stop, peak * (1 - P["trail"]))
            if px <= stop: out.append((rem, "be20_trail15"))
        elif rule == "chandelier_3atr":
            s = st["samples"]
            if len(s) > P["n"]:
                diffs = [abs(s[i] - s[i - 1]) for i in range(len(s) - P["n"], len(s))]
                atr = sum(diffs) / len(diffs)
                ch = peak - P["mult"] * atr
                st["cstop"] = max(st["cstop"] or 0, ch)   # ratchet: only tightens
            stop = max(ref * (1 - P["stop"]), st["cstop"] or 0)
            if px <= stop: out.append((rem, "chandelier"))
        elif rule == "momentum_wide_trail":
            m = g.get("meta") or {}
            hot = (m.get("chg_h1") or 0) >= P["h1_min"] or (m.get("chg_m5") or 0) >= P["m5_min"]
            if live_like(P["wide_dist"] if hot else P["dist"]): out.append((rem, "trail_wide" if hot else "live_rules_stop"))
        return out

    # ---------- polling
    def _prices(self, pairs):
        prices = {}
        for i in range(0, len(pairs), 30):
            d = self.get("https://api.dexscreener.com/latest/dex/pairs/solana/" + ",".join(pairs[i:i + 30])) or {}
            for p in d.get("pairs") or []:
                prices[p["pairAddress"]] = (float(p.get("priceUsd") or 0), (p.get("liquidity") or {}).get("usd") or 0)
        return prices

    def tick(self):
        """30 s DexScreener poll of every open ghost (the only price source once the live position is closed)."""
        with self.mlock: ghosts = list(self.open.values())
        if not ghosts: return
        prices = self._prices(sorted({g["pair"] for g in ghosts}))
        t = now()
        for g in ghosts:
            px, liq = prices.get(g["pair"], (0, 0))
            self._process(g, px, liq, t, from_tick=False)

    def on_tick(self, pair, px, liq, reuse=None):
        """v14: a price the LIVE desk just used for its own exit rules (Helius reserves, or its DexScreener fallback poll).
        Exit-variant ghosts on that pair see the same ticks as the live position, so ghost_live_baseline closes on the
        same tick at the same fill (reuse) instead of missing the wick between two 30 s polls. Entry ghosts and
        POLL_ONLY rules are not fed. Decisions run here; fills run on the ghost-fill worker (never blocks the feed)."""
        if not px or px <= 0: return
        with self.mlock:
            ghosts = [g for g in self.open.values() if g["pair"] == pair and g["kind"] == "exit_variant" and g["rule"] not in POLL_ONLY]
        t = now()
        for g in ghosts: self._process(g, px, liq, t, from_tick=True, reuse=reuse)

    def _process(self, g, px, liq, t, from_tick, reuse=None):
        with self.glock:
            if g["id"] not in self.open: return
            st = g["state"]
            if st.get("busy"): return
            horizon = t - g["opened_ts"] >= V9["horizon_h"] * 3600
            if not px:
                if from_tick: return
                if horizon or t - (st.get("last_ts") or g["opened_ts"]) > 3600:   # no quote for 1h (pair gone) -> close at last price, flagged
                    acts, px, liq = [(st["rem"], "no_price")], st.get("last_px") or g["ref"], 0
                else: return
            else:
                if g["rule"] == "limit_entry_2pct" and not st.get("filled"):
                    if not from_tick: self._limit(g, st, px, t)
                    return
                st["peak"] = max(st["peak"], px); st["last_px"], st["last_ts"] = px, t
                if not from_tick or t - st.get("sample_ts", 0) >= V9["poll_s"]:   # chandelier ATR keeps its ~30 s sample spacing
                    st["samples"] = (st["samples"] + [px])[-V9["sample_keep"]:]; st["sample_ts"] = t
                acts = [(st["rem"], "horizon_24h")] if horizon else self.decide(g["rule"], g["kind"], g, st, px, t)
                if from_tick: st["fed_ticks"] = st.get("fed_ticks", 0) + 1
            if not acts:
                if not from_tick:
                    with self.lock: self.db.execute("UPDATE shadow_ghosts SET state=? WHERE id=?", (json.dumps(st), g["id"]))
                return
            st["busy"] = True
        job = lambda: self._exec(g, st, acts, px, liq, reuse, "live_tick" if from_tick else "ghost_poll")
        if from_tick: self.pool.submit(job)
        else: job()

    def _exec(self, g, st, acts, px, liq, reuse, via):
        try:
            reason = None
            for frac, why in acts:
                frac = min(frac, st["rem"])
                if frac <= 1e-9: continue
                self._sell(g, st, frac, why, px, liq, reuse); reason = why
            st["exit_via"] = via
            if st["rem"] <= 1e-9: self._close(g, st, reason)
            else:
                with self.lock: self.db.execute("UPDATE shadow_ghosts SET state=? WHERE id=?", (json.dumps(st), g["id"]))
        except Exception as e:
            self.ev("Shadow", "ERROR", rule=g.get("rule"), error=repr(e)[:200], tb=traceback.format_exc()[-600:])
        finally:
            st["busy"] = False

    def _limit(self, g, st, px, t):
        """v12 limit_entry_2pct while pending. -> True if the ghost must skip this tick (still waiting, or closed unfilled)."""
        P = EXIT_V9[g["rule"]]; lvl = g["ref"] * (1 - P["lim"])
        if px <= lvl:   # filled at the limit (resting order: no slippage, network fee only); re-base the ghost there
            fill = lvl * (1 + self.cfg.get("net_fee_usd", 0) / g["size_usd"])
            g["ref"], g["entry_px"], g["qty"] = lvl, fill, g["size_usd"] / fill
            st.update(filled=True, peak=lvl)
            with self.lock:
                self.db.execute("UPDATE shadow_ghosts SET ref=?,entry_px=?,qty=?,state=? WHERE id=?", (lvl, fill, g["qty"], json.dumps(st), g["id"]))
            return True
        if t - g["opened_ts"] >= P["wait_min"] * 60:
            st["proceeds"], st["rem"] = g["size_usd"], 0.0   # never filled: money untouched
            self._close(g, st, "limit_not_filled"); return True
        return True

    def _sell(self, g, st, frac, why, px, liq, reuse=None):
        q = frac * g["qty"]
        if liq:
            exit_px, src, model_px, _ = self.sell_fill(g["mint"], q, px, liq, reuse=reuse)
        else:
            exit_px, src = px * (1 - self.cfg["base_slippage"] - self.cfg["swap_fee"]), "model_no_liq"
        if not exit_px or exit_px <= 0 or not math.isfinite(exit_px): exit_px, src = px * 0.5, "guard_floor"   # v8.2: never a negative fill
        st["proceeds"] += max(0.0, exit_px * q); st["rem"] = max(0.0, st["rem"] - frac)
        if st["rem"] > 1e-9:
            self.sev(self.db, self.lock, self.jsonl, g["rule"], "PARTIAL", g["mint"], g["symbol"], g["position_id"], ghost_id=g["id"],
                     frac=round(frac, 3), reason=why, ref_price=px, exit_price=exit_px, fill_src=src, proceeds=round(exit_px * q, 2))

    def _close(self, g, st, reason):
        pnl = max(st["proceeds"] - g["size_usd"], -g["size_usd"]); pct = 100 * pnl / g["size_usd"] if g["size_usd"] else None
        actual = None
        if g["position_id"]:
            with self.lock:
                r = self.db.execute("SELECT status,pnl_usd,pnl_pct,exit_reason FROM positions WHERE id=?", (g["position_id"],)).fetchone()
            if r: actual = dict(status=r[0], pnl_usd=r[1], pnl_pct=r[2], reason=r[3])
        with self.lock:
            self.db.execute("""UPDATE shadow_ghosts SET status='CLOSED',state=?,closed_ts=?,closed_iso=?,exit_reason=?,proceeds=?,hyp_pnl_usd=?,hyp_pnl_pct=?
                WHERE id=?""", (json.dumps(st), now(), iso(), reason, st["proceeds"], pnl, pct, g["id"]))
        with self.mlock: self.open.pop(g["id"], None)
        self.sev(self.db, self.lock, self.jsonl, g["rule"], "SHADOW_EXIT", g["mint"], g["symbol"], g["position_id"], ghost_id=g["id"],
                 ghost_kind=g["kind"], reason=reason, size_usd=round(g["size_usd"], 2), hyp_pnl_usd=round(pnl, 2),
                 hyp_pnl_pct=None if pct is None else round(pct, 2), peak_gain_pct=round(100 * (st["peak"] / g["ref"] - 1), 2),
                 held_min=round((now() - g["opened_ts"]) / 60, 1), actual=actual)

    def loop(self, stop_evt=None):
        while not (stop_evt and stop_evt.is_set()):
            t0 = now()
            try: self.tick()
            except Exception as e: self.ev("Shadow", "ERROR", rule="ghost_v9", error=repr(e)[:200], tb=traceback.format_exc()[-600:])
            time.sleep(max(5, V9["poll_s"] - (now() - t0)))
