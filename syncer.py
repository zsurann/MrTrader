#!/usr/bin/env python3
"""
MemeDesk -> Supabase realtime mirror (one-way, read-only on the desk side).

Runs as its OWN process: it only READS desk.sqlite (read-only connection) and POSTs batches to the
token-protected `desk-ingest` Edge Function. If Supabase is down, the desk keeps trading untouched;
watermarks are only advanced after a successful push, so everything is re-sent (= automatic backfill).

Mirrored: every Filter/Analyst/Executor kill (row by row), every Scout kill aggregated per cycle by reason
(~720/cycle would blow the free Realtime quota), scores, positions, trade events, cycles, equity, heartbeat.
"""
import json, os, re, sqlite3, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("MEMEDESK_DATA", BASE)
DB = os.path.join(DATA, "desk.sqlite")
STATE = os.path.join(DATA, "sync_state.json")
POLL_S, MIN_PUSH_GAP_S, HEARTBEAT_S, EQUITY_S, CHUNK, SMART_S = 2.0, 2.0, 20.0, 60.0, 1000, 30.0

def load_env(path=os.path.join(BASE, ".env.supabase")):
    # VM: secrets come from the systemd EnvironmentFile; box: from .env.supabase (file wins if present)
    env = {k: os.environ[k] for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "DESK_INGEST_TOKEN") if os.environ.get(k)}
    if not os.path.exists(path): return env
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); env[k] = v
    return env
ENV = load_env()
URL = ENV["SUPABASE_URL"] + "/functions/v1/desk-ingest"

def ts(x):
    return None if x is None else datetime.fromtimestamp(float(x), timezone.utc).isoformat()

# ---------------------------------------------------------------- reason -> chart category (Italian UI)
CATS = [
    (r"pair too young", "età < 30m"), (r"pair too old", "età > 72h"),
    (r"mcap .*< \$300k", "mcap < $300k"), (r"mcap .*> \$20M", "mcap > $20M"),
    (r"^liquidity", "liquidità < $40k"), (r"^1h volume", "volume 1h < $50k"),
    (r"^already \+", "già +80% in 1h"), (r"mint authority NOT", "mint non revocato"),
    (r"freeze authority NOT", "freeze non revocato"), (r"LP not burned", "LP non bruciata/bloccata"),
    (r"^top-10", "top10 > 25%"), (r"^dev holds", "dev > 3%"), (r"past rugs|rugged", "rug passati / rugged"),
    (r"bundled", "bundle/insider > 10%"), (r"permanent delegate|transfer fee", "token-2022 rischioso"),
    (r"^score ", "score < 70"), (r"^data unavailable", "dati mancanti"),
]
CATS = [(re.compile(p), c) for p, c in CATS]
def category(reason):
    for rx, c in CATS:
        if rx.search(reason): return c
    return "executor: " + reason[:40]

class Syncer:
    def __init__(self):
        self.state = json.load(open(STATE)) if os.path.exists(STATE) else dict(kills=0, scores=0, events=0, cycle=0)
        self.pos_sig, self.last_push, self.last_status, self.last_equity = {}, 0.0, 0.0, 0.0
        self.last_smart, self.smart_sig = 0.0, None
        self.db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        self.db.execute("PRAGMA busy_timeout=5000")

    def save(self):
        tmp = STATE + ".tmp"; json.dump(self.state, open(tmp, "w")); os.replace(tmp, STATE)

    def push(self, payload):
        req = urllib.request.Request(URL, data=json.dumps(payload, default=str).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + ENV["SUPABASE_ANON_KEY"],
            "apikey": ENV["SUPABASE_ANON_KEY"], "x-desk-token": ENV["DESK_INGEST_TOKEN"]})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    # ---------------- collectors (each returns rows + the watermark to commit after a successful push)
    def kills(self):
        rows = self.db.execute("SELECT id,ts,cycle,stage,mint,symbol,pair,reasons,metrics FROM kills WHERE id>? ORDER BY id LIMIT ?",
                               (self.state["kills"], CHUNK * 5)).fetchall()
        if not rows: return [], self.state["kills"]
        out = [dict(id=i, ts=ts(t), cycle=c, stage=s, mint=m, symbol=sy, pair=p, reasons=json.loads(r), metrics=json.loads(mt or "null"))
               for i, t, c, s, m, sy, p, r, mt in rows if s != "Scout"][:CHUNK]
        wm = rows[-1][0] if len(out) < CHUNK else out[-1]["id"]
        return out, wm

    def scores(self):
        rows = self.db.execute("SELECT id,ts,cycle,mint,symbol,score,passed,components FROM scores WHERE id>? ORDER BY id LIMIT ?",
                               (self.state["scores"], CHUNK)).fetchall()
        out = [dict(id=i, ts=ts(t), cycle=c, mint=m, symbol=s, score=sc, passed=bool(p), components=json.loads(cp or "null"))
               for i, t, c, m, s, sc, p, cp in rows]
        return out, (rows[-1][0] if rows else self.state["scores"])

    def trades(self):
        mx = self.db.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
        rows = self.db.execute("""SELECT id,ts,cycle,kind,mint,symbol,data FROM events WHERE id>? AND id<=?
                                  AND kind IN ('ENTRY','EXIT','TRAIL_ACTIVATED') ORDER BY id""", (self.state["events"], mx)).fetchall()
        return [dict(id=i, ts=ts(t), cycle=c, kind=k, mint=m, symbol=s, data=json.loads(d or "null")) for i, t, c, k, m, s, d in rows], mx

    def cycles(self):
        rows = self.db.execute("SELECT cycle,ts,stats FROM cycles WHERE cycle>? ORDER BY cycle LIMIT 200", (self.state["cycle"],)).fetchall()
        out = []
        for cyc, t, st in rows:
            reasons, stages = {}, {}
            for stage, rs in self.db.execute("SELECT stage,reasons FROM kills WHERE cycle=?", (cyc,)):
                stages[stage] = stages.get(stage, 0) + 1
                for r in set(category(x) for x in json.loads(rs)):
                    reasons[r] = reasons.get(r, 0) + 1
            out.append(dict(cycle=cyc, ts=ts(t), stats=json.loads(st), kill_reasons=reasons, kills_by_stage=stages))
        return out, (rows[-1][0] if rows else self.state["cycle"])

    def positions(self):
        cols = ("id,mint,symbol,pair,status,opened_ts,ref_price,entry_price,size_usd,qty,entry_slip_pct,initial_stop,stop_price,"
                "peak_price,trail_active,last_price,last_ts,score,closed_ts,exit_ref,exit_price,exit_reason,pnl_usd,pnl_pct")
        out, sigs = [], {}
        for r in self.db.execute(f"SELECT {cols} FROM positions"):
            d = dict(zip(cols.split(","), r))
            sig = (d["status"], d["last_ts"], d["stop_price"], d["closed_ts"])
            sigs[d["id"]] = sig
            if self.pos_sig.get(d["id"]) == sig: continue
            d["opened_at"] = ts(d.pop("opened_ts")); d["last_at"] = ts(d.pop("last_ts")); d["closed_at"] = ts(d.pop("closed_ts"))
            d["trail_active"] = bool(d["trail_active"]); d["updated_at"] = ts(time.time())
            out.append(d)
        return out, sigs

    def equity(self):
        cash = self.db.execute("SELECT v FROM ledger WHERE k='cash'").fetchone()
        cash = cash[0] if cash else 0.0
        opn = self.db.execute("SELECT qty,last_price,entry_price FROM positions WHERE status='OPEN'").fetchall()
        return cash, cash + sum(q * (lp or ep) for q, lp, ep in opn), len(opn)

    def smart(self):
        """One-row summary of the smart-wallet list + shadow rules (1 upsert / 30s at most, only when it changed)."""
        q = lambda sql, a=(): self.db.execute(sql, a).fetchall()
        try: q("SELECT 1 FROM smart_wallets LIMIT 1")
        except sqlite3.OperationalError: return None
        size = q("SELECT COUNT(*) FROM smart_wallets WHERE is_smart=1")[0][0]
        tracked = q("SELECT COUNT(*) FROM smart_wallets")[0][0]
        tok = dict(q("SELECT status,COUNT(*) FROM sw_tokens GROUP BY status"))
        bl = q("SELECT COUNT(*) FROM sw_blacklist")[0][0]
        top = [dict(a=a[:4] + "…" + a[-4:], score=sc, wins=w, losses=l, tokens=json.loads(t or "[]")[:4])
               for a, sc, w, l, t in q("SELECT address,score,wins,losses,tokens FROM smart_wallets WHERE is_smart=1 ORDER BY score DESC, wins DESC LIMIT 8")]
        winners = [dict(symbol=sy, status=st, buyers=nb, wins=nw) for sy, st, nb, nw in
                   q("SELECT symbol,status,n_buyers,n_wins FROM sw_tokens ORDER BY processed_ts DESC LIMIT 12")]
        factors = []
        for t, sy, sc, cp in q("SELECT ts,symbol,score,components FROM scores ORDER BY id DESC LIMIT 25"):
            c = json.loads(cp or "{}"); inf = (c.get("_inputs") or {}).get("smart_wallets")
            if not isinstance(inf, dict): continue   # pre-smart-list analyses
            factors.append(dict(ts=ts(t), symbol=sy, score=sc, factor=c.get("smart_wallets"), count=inf.get("count"),
                                status=inf.get("status")))
            if len(factors) >= 10: break
        # shadow (a): min 2 smart wallets at entry
        gates = [json.loads(d) | dict(position_id=pid, symbol=sy) for pid, sy, d in
                 q("SELECT position_id,symbol,data FROM shadow_events WHERE rule='min2_smart' AND kind='ENTRY_GATE' ORDER BY id")]
        pnl = {i: (pu, st) for i, pu, st in q("SELECT id,pnl_usd,status FROM positions")}
        ent = [g for g in gates if g.get("entered") and g.get("position_id")]
        closed = [g for g in ent if pnl.get(g["position_id"], (None, ""))[1] == "CLOSED"]
        a_actual = sum(pnl[g["position_id"]][0] or 0 for g in closed)
        a_shadow = sum(pnl[g["position_id"]][0] or 0 for g in closed if g.get("would_block") is not True)
        rule_a = dict(passes=len(gates), would_block=sum(1 for g in gates if g.get("would_block") is True),
                      undecidable=sum(1 for g in gates if g.get("would_block") is None), entries=len(ent),
                      entries_blocked=sum(1 for g in ent if g.get("would_block") is True), closed=len(closed),
                      actual_pnl_usd=round(a_actual, 2), shadow_pnl_usd=round(a_shadow, 2))
        # shadow (b): exit when smart money exits
        cmp_ = [json.loads(d) | dict(symbol=sy, position_id=pid) for pid, sy, d in
                q("SELECT position_id,symbol,data FROM shadow_events WHERE rule='smart_exit' AND kind='COMPARE' ORDER BY id")]
        rule_b = dict(watched=q("SELECT COUNT(DISTINCT position_id) FROM shadow_events WHERE rule='smart_exit' AND kind='WATCH'")[0][0],
                      triggered=q("SELECT COUNT(DISTINCT position_id) FROM shadow_events WHERE rule='smart_exit' AND kind='SHADOW_EXIT'")[0][0],
                      compared=len(cmp_), actual_pnl_usd=round(sum(c.get("actual_pnl_usd") or 0 for c in cmp_), 2),
                      shadow_pnl_usd=round(sum(c.get("shadow_pnl_usd") or 0 for c in cmp_), 2),
                      rows=[dict(symbol=c["symbol"], actual=c.get("actual_pnl_usd"), shadow=c.get("shadow_pnl_usd"),
                                 triggered=c.get("triggered")) for c in cmp_[-8:]])
        # Skeptic (shadow pre-entry checklist)
        sk = [json.loads(d) | dict(position_id=pid, symbol=sy) for pid, sy, d in
              q("SELECT position_id,symbol,data FROM shadow_events WHERE rule='skeptic' AND kind='ENTRY_GATE' ORDER BY id")]
        sk_ent = [g for g in sk if g.get("entered") and g.get("position_id")]
        sk_cl = [g for g in sk_ent if pnl.get(g["position_id"], (None, ""))[1] == "CLOSED"]
        last_fail = next((g for g in reversed(sk) if g.get("fails")), None)
        rule_s = dict(passes=len(sk), would_block=sum(1 for g in sk if g.get("would_block")), entries=len(sk_ent),
                      entries_blocked=sum(1 for g in sk_ent if g.get("would_block")), closed=len(sk_cl),
                      actual_pnl_usd=round(sum(pnl[g["position_id"]][0] or 0 for g in sk_cl), 2),
                      shadow_pnl_usd=round(sum(pnl[g["position_id"]][0] or 0 for g in sk_cl if not g.get("would_block")), 2),
                      last=(dict(symbol=last_fail["symbol"], fails=last_fail["fails"][:3]) if last_fail else None))
        # partial take-profit 40/60
        pt = [json.loads(d) | dict(symbol=sy) for sy, d in q("SELECT symbol,data FROM shadow_events WHERE rule='partial_tp' AND kind='COMPARE' ORDER BY id")]
        rule_p = dict(compared=len(pt), tp_hit=sum(1 for c in pt if c.get("tp_hit")),
                      actual_pnl_usd=round(sum(c.get("actual_pnl_usd") or 0 for c in pt), 2),
                      shadow_pnl_usd=round(sum(c.get("shadow_pnl_usd") or 0 for c in pt), 2),
                      rows=[dict(symbol=c["symbol"], actual=c.get("actual_pnl_usd"), shadow=c.get("shadow_pnl_usd"), triggered=c.get("tp_hit")) for c in pt[-8:]])
        # shadow trail 4% (activates at +12%, trails 4% under the peak)
        t4 = [json.loads(d) | dict(symbol=sy) for sy, d in q("SELECT symbol,data FROM shadow_events WHERE rule='trail_4pct' AND kind='COMPARE' ORDER BY id")]
        rule_t4 = dict(watched=q("SELECT COUNT(DISTINCT position_id) FROM shadow_events WHERE rule='trail_4pct' AND kind='WATCH'")[0][0],
                       triggered=q("SELECT COUNT(DISTINCT position_id) FROM shadow_events WHERE rule='trail_4pct' AND kind='SHADOW_EXIT'")[0][0],
                       compared=len(t4), actual_pnl_usd=round(sum(c.get("actual_pnl_usd") or 0 for c in t4), 2),
                       shadow_pnl_usd=round(sum(c.get("shadow_pnl_usd") or 0 for c in t4), 2),
                       rows=[dict(symbol=c["symbol"], actual=c.get("actual_pnl_usd"), shadow=c.get("shadow_pnl_usd"),
                                  triggered=c.get("triggered")) for c in t4[-8:]])
        # shadow combo: 40% at +15% + 4% trail on the remaining 60%
        cb = [json.loads(d) | dict(symbol=sy) for sy, d in q("SELECT symbol,data FROM shadow_events WHERE rule='combo_tp_trail4' AND kind='COMPARE' ORDER BY id")]
        rule_c = dict(watched=q("SELECT COUNT(DISTINCT position_id) FROM shadow_events WHERE rule='combo_tp_trail4' AND kind='WATCH'")[0][0],
                      triggered=q("SELECT COUNT(DISTINCT position_id) FROM shadow_events WHERE rule='combo_tp_trail4' AND kind IN ('PARTIAL','SHADOW_EXIT')")[0][0],
                      compared=len(cb), tp_hit=sum(1 for c in cb if c.get("tp_hit")),
                      trail_hit=sum(1 for c in cb if c.get("trail_hit")),
                      actual_pnl_usd=round(sum(c.get("actual_pnl_usd") or 0 for c in cb), 2),
                      shadow_pnl_usd=round(sum(c.get("shadow_pnl_usd") or 0 for c in cb), 2),
                      rows=[dict(symbol=c["symbol"], actual=c.get("actual_pnl_usd"), shadow=c.get("shadow_pnl_usd"),
                                 triggered=bool(c.get("tp_hit") or c.get("trail_hit"))) for c in cb[-8:]])
        # v4: Helius real-time exit trigger (live) vs the old DexScreener poll (shadow 'poll_stop')
        try:
            ex = [json.loads(d) | dict(symbol=sy) for sy, d in q("SELECT symbol,data FROM events WHERE stage='Risk' AND kind='EXIT' ORDER BY id DESC LIMIT 40")]
            src = [json.loads(d) for (d,) in q("SELECT data FROM events WHERE stage='Risk' AND kind='PRICE_SOURCE' AND ts>?", (time.time() - 86400,))]
            pc = [json.loads(d) | dict(symbol=sy) for sy, d in q("SELECT symbol,data FROM shadow_events WHERE rule='poll_stop' AND kind='COMPARE' ORDER BY id")]
            fs = [json.loads(d) | dict(symbol=sy) for sy, d in q("SELECT symbol,data FROM shadow_events WHERE rule='fast_stop' AND kind='COMPARE' ORDER BY id")]
            try: cr = (q("SELECT credits_est FROM helius_ws_usage WHERE day=?", (time.strftime("%Y-%m-%d"),)) or [[0]])[0][0]
            except sqlite3.OperationalError: cr = 0
            rule_ws = dict(exits_ws=sum(1 for e in ex if e.get("trigger_src") == "helius_ws"),
                           exits_poll=sum(1 for e in ex if e.get("trigger_src") == "dexscreener_poll"),
                           fallbacks_24h=sum(1 for e in src if e.get("source") == "dexscreener_poll" and e.get("previous") == "helius_ws"),
                           poll_compared=len(pc), credits_today=cr,
                           rows=[dict(symbol=c["symbol"], poll_seconds_later=c.get("poll_seconds_later"),
                                      poll_px_vs_live_pct=c.get("poll_px_vs_live_trigger_pct")) for c in pc[-8:]],
                           exits=[dict(symbol=e["symbol"], src=e.get("trigger_src"), pnl_pct=e.get("pnl_pct"),
                                       fill_vs_trigger_pct=e.get("fill_vs_trigger_pct")) for e in ex[:8]],
                           fast_stop=[dict(symbol=c["symbol"], seconds_earlier=c.get("seconds_earlier"),
                                           px_vs_live_ref_pct=c.get("px_vs_live_ref_pct")) for c in fs[-8:]])
        except Exception:
            rule_ws = None
        # v5: new shadow candidates (entry gates + exit variants) and the Helius smart-factor source check
        try:
            v5 = {}
            for r in ("organic_50", "churn_50", "m5_chase_10", "top1_10"):
                g = [json.loads(d) | dict(position_id=pid) for pid, d in q("SELECT position_id,data FROM shadow_events WHERE rule=? AND kind='ENTRY_GATE'", (r,))]
                ent = [x for x in g if x.get("entered") and x.get("position_id")]
                cl = [x for x in ent if pnl.get(x["position_id"], (None, ""))[1] == "CLOSED"]
                v5[r] = dict(passes=len(g), would_block=sum(1 for x in g if x.get("would_block") is True), entries=len(ent),
                             closed=len(cl), actual_pnl_usd=round(sum(pnl[x["position_id"]][0] or 0 for x in cl), 2),
                             shadow_pnl_usd=round(sum(pnl[x["position_id"]][0] or 0 for x in cl if x.get("would_block") is not True), 2))
            for r in ("time_stop_60m", "breakeven_8"):
                c_ = [json.loads(d) for (d,) in q("SELECT data FROM shadow_events WHERE rule=? AND kind='COMPARE'", (r,))]
                v5[r] = dict(compared=len(c_), triggered=sum(1 for x in c_ if x.get("triggered")),
                             actual_pnl_usd=round(sum(x.get("actual_pnl_usd") or 0 for x in c_), 2),
                             shadow_pnl_usd=round(sum(x.get("shadow_pnl_usd") or 0 for x in c_), 2))
            sc = [json.loads(d) for (d,) in q("SELECT data FROM shadow_events WHERE rule='smart_src' AND kind='COMPARE'")]
            try: hs = (q("SELECT credits,lookups,fallbacks FROM helius_smart_usage WHERE day=?", (time.strftime("%Y-%m-%d"),)) or [[0, 0, 0]])[0]
            except sqlite3.OperationalError: hs = (0, 0, 0)
            v5["smart_src"] = dict(compared=len(sc), agree=sum(1 for x in sc if x.get("agree")),
                                   only_helius=sum(x.get("only_helius") or 0 for x in sc), only_gt=sum(x.get("only_gt") or 0 for x in sc),
                                   credits_today=hs[0], lookups_today=hs[1], gt_fallbacks_today=hs[2])
            rule_v5 = v5
        except Exception:
            rule_v5 = None
        # Jupiter quoted vs modeled slippage (paper fills)
        fq = [json.loads(d) | dict(symbol=sy) for sy, d in q("SELECT symbol,data FROM events WHERE kind='FILL_QUOTE' ORDER BY id DESC LIMIT 10")]
        fills = [dict(symbol=f["symbol"], side=f.get("side"), modeled=f.get("modeled_slip_pct"), quoted=f.get("quoted_slip_pct"),
                      used=f.get("used")) for f in fq]
        try: hel = (q("SELECT calls FROM helius_usage WHERE day=?", (time.strftime("%Y-%m-%d"),)) or [[0]])[0][0]
        except sqlite3.OperationalError: hel = 0
        payload = dict(top=top, winners=winners, factors=factors, rule_a=rule_a, rule_b=rule_b, rule_skeptic=rule_s,
                       rule_ptp=rule_p, rule_trail4=rule_t4, rule_combo=rule_c, rule_ws=rule_ws, rule_v5=rule_v5, fills=fills, helius_calls_today=hel, min_list_for_factor=10)
        return dict(id=1, updated_at=ts(time.time()), list_size=size, tracked=tracked, tokens_done=tok.get("done", 0),
                    tokens_skipped=tok.get("too_busy", 0) + tok.get("too_busy_deep", 0) + tok.get("error", 0), blacklisted=bl, payload=payload)

    def status(self, cash, eq, nopen):
        pid = None
        try: pid = int(open(os.path.join(DATA, "desk.pid")).read().strip())
        except Exception: pass
        alive = bool(pid) and os.path.exists(f"/proc/{pid}")
        last = self.db.execute("SELECT cycle,ts FROM cycles ORDER BY cycle DESC LIMIT 1").fetchone() or (None, None)
        cfg = self.db.execute("SELECT data FROM events WHERE kind='DESK_START' ORDER BY id DESC LIMIT 1").fetchone()
        return [dict(id=1, heartbeat_at=ts(time.time()), mode="PAPER", desk_pid=pid, desk_alive=alive, cycle=last[0],
                     last_cycle_at=ts(last[1]), cash=cash, equity=eq, start_equity=10000.0, open_positions=nopen,
                     cfg=(json.loads(cfg[0]).get("cfg") if cfg else None))]

    def step(self):
        nowt = time.time()
        k, kw = self.kills(); s, sw = self.scores(); t, tw = self.trades(); c, cw = self.cycles(); p, psig = self.positions()
        cash, eq, nopen = self.equity()
        payload = {n: v for n, v in dict(kills=k, scores=s, trades=t, cycles=c, positions=p).items() if v}
        if c or nowt - self.last_equity >= EQUITY_S:
            payload["equity_snapshots"] = [dict(ts=ts(nowt), cash=cash, equity=eq, open_positions=nopen)]
        sm = None
        if nowt - self.last_smart >= SMART_S:
            try: sm = self.smart()
            except Exception as e: print(time.strftime("%F %T"), "smart summary error", repr(e), flush=True)
            self.last_smart = nowt
            if sm:
                sig = json.dumps({k: v for k, v in sm.items() if k != "updated_at"}, sort_keys=True, default=str)
                if sig != self.smart_sig: payload["smart_summary"] = [sm]
                else: sm = None
        data_due = bool(payload) and nowt - self.last_push >= MIN_PUSH_GAP_S
        if not data_due and nowt - self.last_status < HEARTBEAT_S: return False
        payload["desk_status"] = self.status(cash, eq, nopen)
        res = self.push(payload)
        self.state.update(kills=kw, scores=sw, events=tw, cycle=cw); self.save()
        self.pos_sig = psig; self.last_push = self.last_status = nowt
        if "equity_snapshots" in payload: self.last_equity = nowt
        if "smart_summary" in payload: self.smart_sig = json.dumps({k: v for k, v in sm.items() if k != "updated_at"}, sort_keys=True, default=str)
        if any(len(v) for n, v in payload.items() if n not in ("desk_status", "equity_snapshots", "smart_summary")):
            print(time.strftime("%F %T"), "pushed", res.get("done"), flush=True)
        return True

    def run(self):
        delay = 2.0
        while True:
            try:
                self.step()
                delay = 2.0
            except urllib.error.HTTPError as e:
                print(time.strftime("%F %T"), "push failed", e.code, e.read()[:300], flush=True); time.sleep(delay); delay = min(delay * 2, 120)
            except Exception as e:
                print(time.strftime("%F %T"), "sync error", repr(e), flush=True); time.sleep(delay); delay = min(delay * 2, 120)
            time.sleep(POLL_S)

if __name__ == "__main__":
    s = Syncer()
    if "--once" not in sys.argv:
        import notify   # Telegram alerts (rare events only), own thread + own read-only connection
        notify.start_thread(lambda *a: print(*a, flush=True))
    if "--once" in sys.argv:
        for _ in range(100):   # drain backlog (backfill), then exit
            s.last_push = 0
            if not s.step(): break
        print("state", s.state)
    else:
        s.run()
