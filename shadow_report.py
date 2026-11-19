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
           ("ingresso", "old_rules_v5"), ("ingresso", "new_pair_6h"), ("ingresso", "buy_pressure")}

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
    r = db.execute(f"SELECT pnl_usd, entry_slip_pct, exit_ref, exit_price, entry_price, peak_price, {tr} FROM positions "
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
                mae_pct=avg([100 * (x[6] / x[4] - 1) for x in r if x[4] and x[6]]))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("MEMEDESK_DATA", "/opt/memedesk"), "desk.sqlite"))
    ap.add_argument("--ruleset", help="only positions tagged with this ruleset (e.g. v8_loose, v10_sel)")
    ap.add_argument("--all", action="store_true", help="also list the rules that are live now (retired from shadow)")
    a = ap.parse_args()
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
