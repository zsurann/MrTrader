"""
MemeDesk v9 SHADOW rules (LOG ONLY). Nothing here ever opens, closes or resizes a live paper position.

Ghost tracker: every live entry spawns one hypothetical copy ("ghost") per exit variant, with the same entry
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

V9 = dict(poll_s=30, horizon_h=24.0, max_entry_ghosts=40, entry_cooldown_h=1.0, sample_keep=20)

# Exit variants (parameters are fractions of the market reference price at entry, like the live rules).
EXIT_V9 = {
    "ghost_live_baseline": dict(stop=0.10, activate=0.12, dist=0.06),                     # live rules at ghost cadence
    "tp2x_half_trail25":   dict(stop=0.10, tp_x=2.0, tp_frac=0.50, trail=0.25),            # take initials at 2x, rest trails 25%
    "ladder_2x4x_trail18": dict(stop=0.30, steps=((2.0, 0.50), (4.0, 0.25)), trail=0.18),  # -30% stop, 50% @2x, 25% @4x, rest 18% under high (never below entry)
    "profit_lock":         dict(stop=0.10, locks=((0.5, 0.0), (1.0, 0.5)), trail_after=3.0, trail=0.40),
    "time_stop_15m":       dict(stop=0.10, activate=0.12, dist=0.06, minutes=15, min_gain=0.05),
    "be20_trail15":        dict(stop=0.10, be_arm=0.20, be_lock=0.02, trail_arm=0.40, trail=0.15),
    "chandelier_3atr":     dict(stop=0.10, n=14, mult=3.0),
    "momentum_wide_trail": dict(stop=0.10, activate=0.12, dist=0.06, wide_dist=0.12, h1_min=30.0, m5_min=5.0),
}
# Shadow entry ideas (ghost uses the live exit rules: -10% stop, trail 6% from +12%).
ENTRY_V9 = {
    "dip_after_pump": dict(h6_min=50.0, h1_max=-10.0, h1_min=-35.0, m5_min=0.0),
    "score_45_55":    dict(lo=45.0, hi=55.0),
    "migration_dip":  dict(max_since_mig_h=6.0, h1_max=-20.0, m5_min=0.0),
}
LIVE_EXIT = dict(stop=0.10, activate=0.12, dist=0.06)

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
    return g


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
                self.open[d["id"]] = d
        self.mlock = threading.Lock()

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
        for rule in EXIT_V9:
            try: self._insert(rule, "exit_variant", pid, order.mint, order.symbol, order.pair_address, order.ref_price, order.fill_price,
                              order.qty, order.size_usd, meta)
            except Exception as e: self.ev("Shadow", "ERROR", rule=rule, error=repr(e)[:200])

    def consider_entries(self, fp, score, mig_ts, live_open_mints, equity):
        """Shadow entry ideas for a Filter-passed candidate the live desk did not enter."""
        c = fp.cand
        if c.mint in live_open_mints or not c.price_usd or not c.liq_usd: return
        pc = c.price_change or {}; h6, h1, m5 = pc.get("h6"), pc.get("h1"), pc.get("m5")
        hits = []
        E = ENTRY_V9["dip_after_pump"]
        if None not in (h6, h1, m5) and h6 >= E["h6_min"] and E["h1_min"] <= h1 <= E["h1_max"] and m5 >= E["m5_min"]: hits.append("dip_after_pump")
        E = ENTRY_V9["score_45_55"]
        if score is not None and E["lo"] <= score < E["hi"]: hits.append("score_45_55")
        E = ENTRY_V9["migration_dip"]
        if mig_ts and (now() - mig_ts) / 3600 <= E["max_since_mig_h"] and h1 is not None and m5 is not None and h1 <= E["h1_max"] and m5 >= E["m5_min"]:
            hits.append("migration_dip")
        if not hits: return
        with self.mlock:
            n_entry = sum(1 for g in self.open.values() if g["kind"] == "entry")
            open_keys = {(g["rule"], g["mint"]) for g in self.open.values() if g["kind"] == "entry"}
        for rule in hits:
            if n_entry >= V9["max_entry_ghosts"] or (rule, c.mint) in open_keys: continue
            with self.lock:
                last = self.db.execute("SELECT MAX(closed_ts) FROM shadow_ghosts WHERE rule=? AND mint=?", (rule, c.mint)).fetchone()[0]
            if last and now() - last < V9["entry_cooldown_h"] * 3600: continue
            size = min(self.cfg["max_pos_frac"] * equity, self.cfg["max_liq_frac"] * c.liq_usd)
            if size < 50: continue
            ref = c.price_usd
            fill = ref * (1 + self.cfg["base_slippage"] + self.cfg["swap_fee"] + size / (c.liq_usd / 2))   # model fill (no Jupiter quote for ghosts)
            self._insert(rule, "entry", None, c.mint, c.symbol, c.pair_address, ref, fill, size / fill, size,
                         dict(score=score, chg_h6=h6, chg_h1=h1, chg_m5=m5, liq=c.liq_usd, mcap=c.mcap, age_h=c.age_h, fill_src="model"))
            n_entry += 1

    # ---------- exit logic (pure: returns list of (fraction_of_original, reason), and whether the ghost is done)
    @staticmethod
    def decide(rule, kind, g, st, px, t):
        ref = g["ref"]; peak = st["peak"]; rem = st["rem"]; out = []
        P = EXIT_V9.get(rule, LIVE_EXIT) if kind == "exit_variant" else LIVE_EXIT
        def live_like(dist):
            stop = ref * (1 - P["stop"])
            if peak >= ref * (1 + P.get("activate", 0.12)): stop = max(stop, peak * (1 - dist))
            return px <= stop
        if kind == "entry" or rule == "ghost_live_baseline":
            if live_like(P["dist"]): out.append((rem, "live_rules_stop"))
        elif rule == "tp2x_half_trail25":
            if "tp" not in st["steps"] and px >= ref * P["tp_x"]:
                out.append((P["tp_frac"], "tp_2x")); st["steps"].append("tp"); rem -= P["tp_frac"]
            if "tp" in st["steps"]:
                if px <= peak * (1 - P["trail"]) and rem > 1e-9: out.append((rem, "trail_25"))
            elif px <= ref * (1 - P["stop"]): out.append((rem, "stop_10"))
        elif rule == "ladder_2x4x_trail18":
            for i, (x, f) in enumerate(P["steps"]):
                k = f"s{i}"
                if k not in st["steps"] and px >= ref * x:
                    out.append((f, f"tp_{x:g}x")); st["steps"].append(k); rem -= f
            if st["steps"]:
                if px <= max(ref, peak * (1 - P["trail"])) and rem > 1e-9: out.append((rem, "trail_18"))
            elif px <= ref * (1 - P["stop"]): out.append((rem, "stop_30"))
        elif rule == "profit_lock":
            pk = peak / ref - 1; lvl = ref * (1 - P["stop"])
            if pk >= P["trail_after"]: lvl = max(lvl, peak * (1 - P["trail"]))
            elif pk >= P["locks"][1][0]: lvl = max(lvl, ref * (1 + P["locks"][1][1]))
            elif pk >= P["locks"][0][0]: lvl = max(lvl, ref * (1 + P["locks"][0][1]))
            if px <= lvl: out.append((rem, "profit_lock"))
        elif rule == "time_stop_15m":
            if not st["checked"] and t - g["opened_ts"] >= P["minutes"] * 60:
                st["checked"] = True
                if px < ref * (1 + P["min_gain"]): out.append((rem, "time_15m"))
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
        with self.mlock: ghosts = list(self.open.values())
        if not ghosts: return
        prices = self._prices(sorted({g["pair"] for g in ghosts}))
        t = now()
        for g in ghosts:
            st = g["state"]; px, liq = prices.get(g["pair"], (0, 0))
            horizon = t - g["opened_ts"] >= V9["horizon_h"] * 3600
            if not px:
                if horizon or t - (st.get("last_ts") or g["opened_ts"]) > 3600:   # no quote for 1h (pair gone) -> close at last price, flagged
                    self._sell(g, st, st["rem"], "no_price", st.get("last_px") or g["ref"], 0)
                    self._close(g, st, "no_price")
                continue
            st["peak"] = max(st["peak"], px); st["last_px"], st["last_ts"] = px, t
            st["samples"] = (st["samples"] + [px])[-V9["sample_keep"]:]
            acts = [] if horizon else self.decide(g["rule"], g["kind"], g, st, px, t)
            if horizon: acts = [(st["rem"], "horizon_24h")]
            reason = None
            for frac, why in acts:
                frac = min(frac, st["rem"])
                if frac <= 1e-9: continue
                self._sell(g, st, frac, why, px, liq); reason = why
            if st["rem"] <= 1e-9: self._close(g, st, reason)
            else:
                with self.lock:
                    self.db.execute("UPDATE shadow_ghosts SET state=? WHERE id=?", (json.dumps(st), g["id"]))

    def _sell(self, g, st, frac, why, px, liq):
        q = frac * g["qty"]
        if liq:
            exit_px, src, model_px, _ = self.sell_fill(g["mint"], q, px, liq)
        else:
            exit_px, src = px * (1 - self.cfg["base_slippage"] - self.cfg["swap_fee"]), "model_no_liq"
        st["proceeds"] += exit_px * q; st["rem"] = max(0.0, st["rem"] - frac)
        if st["rem"] > 1e-9:
            self.sev(self.db, self.lock, self.jsonl, g["rule"], "PARTIAL", g["mint"], g["symbol"], g["position_id"], ghost_id=g["id"],
                     frac=round(frac, 3), reason=why, ref_price=px, exit_price=exit_px, fill_src=src, proceeds=round(exit_px * q, 2))

    def _close(self, g, st, reason):
        pnl = st["proceeds"] - g["size_usd"]; pct = 100 * pnl / g["size_usd"] if g["size_usd"] else None
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
