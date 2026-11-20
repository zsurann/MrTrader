#!/usr/bin/env python3
"""Shadow rules scoreboard (READ ONLY, paper desk). Which shadow rules would have done better than the live rules?

  sudo python3 shadow_report.py [--db /opt/memedesk/desk.sqlite] [--ruleset v10_sel]

delta_usd > 0 means the shadow rule beats the live rules on the same trades. Manual closes are excluded.
  exit rules  : COMPARE rows (actual vs shadow PnL on the same position) and v9 ghosts vs ghost_live_baseline.
  entry gates : PnL of the closed live trades the gate would have blocked; delta = -that (blocking losers helps).
  shadow entry: v9 ghosts for coins the live desk did not buy (pure extra PnL with the live exit rules).
`board()` is also used by syncer.py for the dashboard.
"""
import argparse, json, os, random, sqlite3
from collections import defaultdict

# v10: these used to be shadow rules and are live now (their old rows are history, not candidates)
RETIRED = {("uscita", "smart_exit"), ("uscita", "trail_4pct"), ("uscita", "breakeven_8"),
           ("ingresso", "old_rules_v5"), ("ingresso", "new_pair_6h"), ("ingresso", "buy_pressure"),
           ("ingresso", "organic_50"), ("ingresso", "sell_spike_avoid"), ("ingresso", "liq_growth")}   # v13

def board(db, ruleset=None, include_retired=False):
    """-> dict(positions, live_pnl, rows=[dict(kind, rule, n, blocked, live, shadow, delta)]) sorted by delta desc."""
    q = lambda s, p=(): db.execute(s, p).fetchall()
    cols = {r[1] for r in q("PRAGMA table_info(positions)")}
    where, args = "status='CLOSED' AND COALESCE(exit_reason,'') NOT LIKE 'manual_close%'", []
    if ruleset and "ruleset" in cols: where += " AND ruleset=?"; args.append(ruleset)
    pos = {r[0]: (r[1] or 0.0) for r in q(f"SELECT id, pnl_usd FROM positions WHERE {where}", args)}
    rows = []
    def add(kind, rule, n, live, sh, blocked=None, per=None):
        lo, hi = ci90(per or [])
        rows.append(dict(kind=kind, rule=rule, n=n, blocked=blocked, live=round(live, 2), shadow=round(sh, 2), delta=round(sh - live, 2),
                         ci_lo=lo, ci_hi=hi))
    for rule, in q("SELECT DISTINCT rule FROM shadow_events WHERE kind='COMPARE'"):
        seen = {}
        for pid, d in q("SELECT position_id, data FROM shadow_events WHERE rule=? AND kind='COMPARE'", (rule,)):
            d = json.loads(d)
            if pid in pos and d.get("shadow_pnl_usd") is not None and "actual_pnl_usd" in d:
                seen[pid] = (pos[pid], float(d["shadow_pnl_usd"]))
        if seen: add("uscita", rule, len(seen), sum(v[0] for v in seen.values()), sum(v[1] for v in seen.values()),
                     per=[b - a for a, b in seen.values()])
    for rule, in q("SELECT DISTINCT rule FROM shadow_events WHERE kind='ENTRY_GATE'"):
        blk = {}
        for pid, d in q("SELECT position_id, data FROM shadow_events WHERE rule=? AND kind='ENTRY_GATE' AND position_id IS NOT NULL", (rule,)):
            if pid in pos: blk[pid] = json.loads(d).get("would_block") is True
        if blk:
            add("ingresso", rule, len(blk), sum(pos[p] for p in blk), sum(pos[p] for p, b in blk.items() if not b), sum(blk.values()),
                per=[-pos[p] if b else 0.0 for p, b in blk.items()])
    try:
        g = q("SELECT rule, kind, position_id, hyp_pnl_usd, opened_ts FROM shadow_ghosts WHERE status='CLOSED' AND hyp_pnl_usd IS NOT NULL")
        since = q("SELECT v FROM ledger WHERE k=?", ("epoch_" + ruleset,)) if ruleset else []
    except sqlite3.OperationalError:
        g, since = [], []
    t0 = since[0][0] if since else 0
    base = {pid: p for r, k, pid, p, t in g if r == "ghost_live_baseline"}
    by = defaultdict(list)
    for r, k, pid, p, t in g:
        if k == "entry" and t >= t0: by[("ombra-ingr", r)].append((0.0, p))
        elif k != "entry" and r != "ghost_live_baseline" and pid in base and pid in pos: by[("ghost-usc", r)].append((base[pid], p))
    for (k, r), v in by.items(): add(k, r, len(v), sum(x[0] for x in v), sum(x[1] for x in v), per=[b - a for a, b in v])
    if not include_retired: rows = [x for x in rows if (x["kind"], x["rule"]) not in RETIRED]
    rows.sort(key=lambda x: -x["delta"])
    return dict(positions=len(pos), live_pnl=round(sum(pos.values()), 2), rows=rows, stats=stats(db, where, args))

def ci90(per, n=400):
    """90% bootstrap interval of the TOTAL delta (resampled trades). Both ends > 0 = the gain is unlikely to be luck."""
    if len(per) < 5: return None, None
    rnd = random.Random(7); tot = sorted(sum(rnd.choices(per, k=len(per))) for _ in range(n))
    return round(tot[int(0.05 * n)], 2), round(tot[int(0.95 * n) - 1], 2)

def stats(db, where, args):
    """Live performance of the same closed trades: the numbers a desk is judged on."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(positions)")}
    tr = "trough_price" if "trough_price" in cols else "NULL"
    r = db.execute(f"SELECT pnl_usd, entry_slip_pct, exit_ref, exit_price, entry_price, peak_price, {tr}, stop_price, exit_reason FROM positions "
                   f"WHERE {where} ORDER BY closed_ts", args).fetchall()
    if not r: return None
    pnl = [x[0] or 0.0 for x in r]; w = [p for p in pnl if p > 0]; l = [p for p in pnl if p <= 0]
    eq = dd = peak = 0.0
    for p in pnl: eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)
    avg = lambda v: round(sum(v) / len(v), 2) if v else None
    return dict(trades=len(pnl), win_rate=round(100 * len(w) / len(pnl), 1), avg_win=avg(w), avg_loss=avg(l),
                profit_factor=round(sum(w) / -sum(l), 2) if l and sum(l) else None, expectancy=avg(pnl), max_drawdown=round(dd, 2),
                entry_slip_pct=avg([x[1] for x in r if x[1] is not None]),
                exit_slip_pct=avg([100 * (1 - x[3] / x[2]) for x in r if x[2] and x[3]]),
                mfe_pct=avg([100 * (x[5] / x[4] - 1) for x in r if x[4] and x[5]]),
                mae_pct=avg([100 * (x[6] / x[4] - 1) for x in r if x[4] and x[6]]),
                # how far below its stop each stop/trailing/breakeven exit actually filled (0 = exactly at the stop)
                stop_gap_pct=avg(g := [100 * (x[3] / x[7] - 1) for x in r if x[3] and x[7] and x[8] and x[8].startswith(("stop", "trailing", "breakeven"))]),
                stop_gap_worst_pct=round(min(g), 2) if g else None)

FEATURES = ("score", "score_old", "age_h", "liq_usd", "mcap", "vol_h1", "chg_m5", "chg_h1", "chg_h6", "top10", "top1",
            "dev_pct", "bundle", "organic", "smart_count", "entry_slip_pct", "hour", "weekday")

def meta_plus(meta, slip, ots):
    """entry_meta + entry slippage + entry hour / weekday in Europe/Rome (v15)."""
    m = json.loads(meta); m["entry_slip_pct"] = slip
    if ots:
        from datetime import datetime; from zoneinfo import ZoneInfo
        d = datetime.fromtimestamp(ots, ZoneInfo("Europe/Rome")); m["hour"], m["weekday"] = d.hour, d.weekday()
    return m

def features(db, ruleset=None):
    """v12: PnL per tertile of every entry feature (entry_meta, closed non-manual trades). A tertile whose 90% interval
    of the MEAN PnL is above 0 is a real edge candidate; one far below the others is a filter candidate."""
    where, args = "status='CLOSED' AND COALESCE(exit_reason,'') NOT LIKE 'manual_close%' AND entry_meta IS NOT NULL", []
    if ruleset: where += " AND ruleset=?"; args.append(ruleset)
    rows = []
    for pnl, slip, meta, ots in db.execute(f"SELECT pnl_usd, entry_slip_pct, entry_meta, opened_ts FROM positions WHERE {where}", args):
        rows.append((pnl or 0.0, meta_plus(meta, slip, ots)))
    out = []
    for f in FEATURES:
        v = sorted(((m[f], p) for p, m in rows if isinstance(m.get(f), (int, float))), key=lambda x: x[0])   # value only: ties keep trade order
        if len(v) < 15 or len({x[0] for x in v}) < 3: continue   # too few trades or values to split into thirds
        k = len(v) // 3
        for i, part in enumerate((v[:k], v[k:2 * k], v[2 * k:])):
            pn = [p for _, p in part]; lo, hi = ci90(pn)
            n = len(pn)
            out.append(dict(feature=f, group=("basso", "medio", "alto")[i], lo_val=part[0][0], hi_val=part[-1][0], n=n,
                            mean=round(sum(pn) / n, 2), win=round(100 * sum(1 for p in pn if p > 0) / n, 1),
                            ci_lo=None if lo is None else round(lo / n, 2), ci_hi=None if hi is None else round(hi / n, 2)))
    return dict(trades=len(rows), rows=out)

def mine(db, min_test=12):
    """v14: search 1- and 2-condition entry filters (feature <= or >= its tertile cut) on closed trades, in time order:
    first 70% = training, last 30% = test. Keep only filters with mean PnL > 0 in BOTH halves (test = trades the search
    did not choose from). Survivors are shadow candidates, not proof: 16 features x 4 cuts -> ~2000 combos tested."""
    where = "status='CLOSED' AND COALESCE(exit_reason,'') NOT LIKE 'manual_close%' AND entry_meta IS NOT NULL"
    rows = []
    for pnl, slip, meta, ots in db.execute(f"SELECT pnl_usd, entry_slip_pct, entry_meta, opened_ts FROM positions WHERE {where} ORDER BY closed_ts"):
        rows.append((pnl or 0.0, meta_plus(meta, slip, ots)))
    cut = int(0.7 * len(rows)); train, test = rows[:cut], rows[cut:]
    conds = []
    for f in FEATURES:
        v = sorted(m[f] for _, m in train if isinstance(m.get(f), (int, float)))
        if len(v) < 15 or len(set(v)) < 3: continue
        for t in (v[len(v) // 3], v[2 * len(v) // 3]):
            conds += [(f"{f} <= {t:.4g}", lambda m, f=f, t=t: isinstance(m.get(f), (int, float)) and m[f] <= t),
                      (f"{f} >= {t:.4g}", lambda m, f=f, t=t: isinstance(m.get(f), (int, float)) and m[f] >= t)]
    def stat(part, fs):
        p = [x for x, m in part if all(fn(m) for _, fn in fs)]
        return len(p), (sum(p) / len(p) if p else 0.0)
    out = []
    combos = [(c,) for c in conds] + [(a, b) for i, a in enumerate(conds) for b in conds[i + 1:] if a[0].split()[0] != b[0].split()[0]]
    for fs in combos:
        n1, m1 = stat(train, fs); n2, m2 = stat(test, fs)
        if n2 >= min_test and n1 >= 2 * min_test and m1 > 0 and m2 > 0:
            out.append(dict(rule=" AND ".join(c[0] for c in fs), n_train=n1, mean_train=round(m1, 2), n_test=n2, mean_test=round(m2, 2)))
    out.sort(key=lambda x: -min(x["mean_train"], x["mean_test"]))
    return dict(trades=len(rows), train=len(train), test=len(test), tested=len(combos), base_test=round(stat(test, ())[1], 2), rows=out[:20])

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("MEMEDESK_DATA", "/opt/memedesk"), "desk.sqlite"))
    ap.add_argument("--ruleset", help="only positions tagged with this ruleset (e.g. v8_loose, v10_sel)")
    ap.add_argument("--all", action="store_true", help="also list the rules that are live now (retired from shadow)")
    ap.add_argument("--features", action="store_true", help="PnL per tertile of each entry feature instead of the shadow board")
    ap.add_argument("--mine", action="store_true", help="search 1-2 condition filters profitable in both the old and the recent trades")
    a = ap.parse_args()
    if a.mine:
        r = mine(sqlite3.connect(f"file:{a.db}?mode=ro", uri=True))
        print(f"trade {r['trades']} (vecchi {r['train']} / recenti {r['test']}), combinazioni provate {r['tested']}, "
              f"media $/trade recenti senza filtro {r['base_test']:+.2f}; sopravvissute (positive in entrambi): {len(r['rows'])}")
        for x in r["rows"]:
            print(f"{x['rule'][:70]:70} vecchi n={x['n_train']:3d} {x['mean_train']:+6.2f}  recenti n={x['n_test']:3d} {x['mean_test']:+6.2f}")
        raise SystemExit
    if a.features:
        fr = features(sqlite3.connect(f"file:{a.db}?mode=ro", uri=True), a.ruleset)
        print(f"trade con caratteristiche d'ingresso: {fr['trades']}  (media $/trade per terzo; [IC 90%]; ✓ = anche il bordo basso > 0)")
        for x in fr["rows"]:
            ci = f"[{x['ci_lo']:+.1f}, {x['ci_hi']:+.1f}]" if x["ci_lo"] is not None else ""
            print(f"{x['feature']:15} {x['group']:5} {x['lo_val']:>10.4g} … {x['hi_val']:<10.4g} n={x['n']:3d} media {x['mean']:+6.2f} "
                  f"vinc {x['win']:5.1f}% {ci}{' ✓' if x['ci_lo'] is not None and x['ci_lo'] > 0 else ''}")
        raise SystemExit
    b = board(sqlite3.connect(f"file:{a.db}?mode=ro", uri=True), a.ruleset, a.all)
    print(f"posizioni chiuse considerate: {b['positions']}  PnL live totale: {b['live_pnl']:+.2f} $")
    if b["stats"]: print("live:", "  ".join(f"{k}={v}" for k, v in b["stats"].items()))
    print(f"{'tipo':10} {'regola':38} {'n':>4} {'live $':>9} {'ombra $':>9} {'delta $':>9}")
    for x in b["rows"]:
        lab = x["rule"] + (f" (blocca {x['blocked']})" if x["blocked"] is not None else "")
        ci = f" [{x['ci_lo']:+.0f}, {x['ci_hi']:+.0f}]" if x["ci_lo"] is not None else ""
        sure = x["ci_lo"] is not None and x["ci_lo"] > 0
        print(f"{x['kind']:10} {lab[:38]:38} {x['n']:4d} {x['live']:+9.2f} {x['shadow']:+9.2f} {x['delta']:+9.2f}{ci}"
              f"{'  <-- meglio (solido)' if sure else '  <-- meglio' if x['delta'] > 0 else ''}")
