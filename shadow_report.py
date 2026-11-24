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
# v14: new_pair_6h is shadow again (live age cap 24h); the worst / never-blocking shadow rules are retired (history kept).
RETIRED = {("uscita", "smart_exit"), ("uscita", "trail_4pct"), ("uscita", "breakeven_8"),
           ("ingresso", "old_rules_v5"), ("ingresso", "buy_pressure"),
           ("ingresso", "organic_50"), ("ingresso", "sell_spike_avoid"), ("ingresso", "liq_growth")}   # v13
RETIRED |= {(k, r) for r in ("ladder_2x4x_trail18", "profit_lock", "tp2x_half_trail25", "wide_stop50_trail20", "limit_entry_2pct",
                              "trail_early_6_3") for k in ("ghost-usc",)}
RETIRED |= {("ombra-ingr", r) for r in ("copy_smart2", "score_45_55", "migration_dip", "dip_after_pump")}
RETIRED |= {("ingresso", r) for r in ("churn_50", "top1_10", "small_trades_200", "fomo_score_70")}
BASELINES = ("ghost_live_baseline", "ghost_live_poll30")
MIN_N = 15        # fewer trades than this on the side that matters -> "too few", never "promising"
FDR_Q = 0.10      # Benjamini-Hochberg false discovery rate across all rows of one board

def _mean(v): return sum(v) / len(v) if v else None

def _boot(fn, items, n=400, seed=7):
    """Bootstrap: resample `items` (trades) with replacement, apply fn. -> (sorted stats without None)."""
    rnd = random.Random(seed); out = []
    for _ in range(n):
        x = fn(rnd.choices(items, k=len(items)))
        if x is not None: out.append(x)
    return sorted(out)

def _ci_p(vals):
    """-> (lo90, hi90, one-sided p that the true value <= 0)."""
    if len(vals) < 50: return None, None, None
    return (round(vals[int(0.05 * len(vals))], 2), round(vals[int(0.95 * len(vals)) - 1], 2),
            round(sum(1 for v in vals if v <= 0) / len(vals), 4))

def board(db, ruleset=None, include_retired=False):
    """v14 metrics, in % per trade (trade sizes changed between rulesets, so $ totals mislead):
      ingresso   (entry gate on live trades): rule_pct = mean % of the trades the gate ALLOWS, live_pct = mean % of all of them,
                 diff_pct = rule_pct - live_pct (bootstrap over trades). Blocking losers in a losing strategy is NOT enough:
                 'promising' needs diff > 0 significant after Benjamini-Hochberg (q < 0.10) and >= 15 allowed trades;
                 'profitable' additionally needs the allowed trades' own mean % > 0 at the 90% lower bound.
      uscita / ghost-usc (exit variant): paired per-trade diff, variant % - baseline % on the same entries
                 (COMPARE rows vs the live trade; ghosts vs ghost_live_baseline, which is fed the live ticks since v14).
      ombra-ingr (shadow entry ghosts): their own mean % per trade (no live counterpart), live exit rules, 30 s poll pricing.
    $ fields (live, shadow, delta) are kept for the dashboard; ci_lo/ci_hi (the dashboard check mark) are only set when the
    verdict is 'promising'. -> dict(positions, live_pnl, rows, stats, drift, notes)."""
    q = lambda s, p=(): db.execute(s, p).fetchall()
    cols = {r[1] for r in q("PRAGMA table_info(positions)")}
    where, args = "status='CLOSED' AND COALESCE(exit_reason,'') NOT LIKE 'manual_close%'", []
    if ruleset and "ruleset" in cols: where += " AND ruleset=?"; args.append(ruleset)
    pos = {r[0]: (r[1] or 0.0, r[2] or 0.0, r[3] or 0.0) for r in q(f"SELECT id, pnl_usd, pnl_pct, size_usd FROM positions WHERE {where}", args)}
    rows = []
    def add(kind, rule, n, live, sh, blocked=None, n_rule=None, live_pct=None, rule_pct=None, boot=None, own=None, extra=None):
        lo, hi, p = _ci_p(boot) if boot is not None else (None, None, None)
        d = dict(kind=kind, rule=rule, n=n, blocked=blocked, live=round(live, 2), shadow=round(sh, 2), delta=round(sh - live, 2),
                 n_rule=n_rule, live_pct=None if live_pct is None else round(live_pct, 2), rule_pct=None if rule_pct is None else round(rule_pct, 2),
                 diff_pct=None if (live_pct is None or rule_pct is None) else round(rule_pct - live_pct, 2),
                 diff_ci=[lo, hi] if lo is not None else None, p=p, own_lo=own, ci_lo=None, ci_hi=None, _tot=(live, sh))
        if extra: d.update(extra)
        rows.append(d)
    # ---- exit rules logged as COMPARE (shadow PnL of the same position)
    for rule, in q("SELECT DISTINCT rule FROM shadow_events WHERE kind='COMPARE'"):
        pairs = {}
        for pid, dd in q("SELECT position_id, data FROM shadow_events WHERE rule=? AND kind='COMPARE'", (rule,)):
            dd = json.loads(dd)
            if pid in pos and dd.get("shadow_pnl_usd") is not None and "actual_pnl_usd" in dd and pos[pid][2]:
                pairs[pid] = (pos[pid][1], 100 * float(dd["shadow_pnl_usd"]) / pos[pid][2], pos[pid][0], float(dd["shadow_pnl_usd"]))
        if pairs:
            v = list(pairs.values())
            add("uscita", rule, len(v), sum(x[2] for x in v), sum(x[3] for x in v), n_rule=len(v),
                live_pct=_mean([x[0] for x in v]), rule_pct=_mean([x[1] for x in v]),
                boot=_boot(lambda s: _mean([b - a for a, b, _, _ in s]), v))
    # ---- entry gates on live trades: allowed vs all
    for rule, in q("SELECT DISTINCT rule FROM shadow_events WHERE kind='ENTRY_GATE'"):
        blk = {}
        for pid, dd in q("SELECT position_id, data FROM shadow_events WHERE rule=? AND kind='ENTRY_GATE' AND position_id IS NOT NULL", (rule,)):
            if pid in pos: blk[pid] = json.loads(dd).get("would_block") is True
        if not blk: continue
        items = [(pos[p][1], b) for p, b in blk.items()]
        allowed = [x for x, b in items if not b]; blocked_v = [x for x, b in items if b]
        def diff(s):
            a = [x for x, b in s if not b]
            return None if not a else _mean(a) - _mean([x for x, _ in s])
        own = _ci_p(_boot(lambda s: _mean([x for x, b in s if not b]), items))[0] if allowed else None
        add("ingresso", rule, len(items), sum(pos[p][0] for p in blk), sum(pos[p][0] for p, b in blk.items() if not b), sum(blk.values()),
            n_rule=len(allowed), live_pct=_mean([x for x, _ in items]), rule_pct=_mean(allowed),
            boot=_boot(diff, items) if allowed and blocked_v else None, own=own,
            extra=dict(blocked_pct=None if not blocked_v else round(_mean(blocked_v), 2)))
    # ---- ghosts
    try:
        g = q("SELECT rule, kind, position_id, hyp_pnl_usd, hyp_pnl_pct, opened_ts FROM shadow_ghosts WHERE status='CLOSED' AND hyp_pnl_usd IS NOT NULL")
        since = q("SELECT v FROM ledger WHERE k=?", ("epoch_" + ruleset,)) if ruleset else []
    except sqlite3.OperationalError:
        g, since = [], []
    t0 = since[0][0] if since else 0
    base = {pid: (u, pc or 0.0) for r, k, pid, u, pc, t in g if r == "ghost_live_baseline"}
    by = defaultdict(list)
    for r, k, pid, u, pc, t in g:
        if k == "entry" and t >= t0: by[("ombra-ingr", r)].append((0.0, 0.0, u, pc or 0.0))
        elif k != "entry" and r not in BASELINES and pid in base and pid in pos: by[("ghost-usc", r)].append((base[pid][0], base[pid][1], u, pc or 0.0))
    for (k, r), v in by.items():
        if k == "ombra-ingr":
            add(k, r, len(v), 0.0, sum(x[2] for x in v), n_rule=len(v), rule_pct=_mean([x[3] for x in v]),
                boot=_boot(lambda s: _mean([x[3] for x in s]), v))
        else:
            add(k, r, len(v), sum(x[0] for x in v), sum(x[2] for x in v), n_rule=len(v), live_pct=_mean([x[1] for x in v]),
                rule_pct=_mean([x[3] for x in v]), boot=_boot(lambda s: _mean([x[3] - x[1] for x in s]), v))
    # ---- drift: does each baseline ghost reproduce the live trade? (same entry, same rules; only pricing/sampling differ)
    drift = {}
    for b in BASELINES:
        dv = [pc - pos[pid][1] for r, k, pid, u, pc, t in g if r == b and pid in pos and pc is not None]
        if dv:
            sv = sorted(dv)
            drift[b] = dict(n=len(dv), mean_pp=round(_mean(dv), 2), median_pp=round(sv[len(sv) // 2], 2),
                            share_over_2pp=round(100 * sum(1 for x in dv if abs(x) > 2) / len(dv), 1),
                            worst_pp=round(max(dv, key=abs), 2))
    if not include_retired: rows = [x for x in rows if (x["kind"], x["rule"]) not in RETIRED]
    # ---- Benjamini-Hochberg over the rows that have a p-value; verdicts
    ps = sorted((x["p"], i) for i, x in enumerate(rows) if x["p"] is not None)
    m = len(ps); qv = {}
    prev = 1.0
    for rank in range(m, 0, -1):
        p, i = ps[rank - 1]; prev = min(prev, p * m / rank); qv[i] = round(prev, 4)
    for i, x in enumerate(rows):
        x["q_bh"] = qv.get(i)
        side_n = x["n_rule"] if x["kind"] != "ingresso" else min(x["n_rule"] or 0, x["blocked"] or 0)
        if x["p"] is None or (x["n_rule"] or 0) < MIN_N: x["verdict"] = "too few"
        elif x["q_bh"] is not None and x["q_bh"] < FDR_Q and (x["diff_pct"] if x["kind"] != "ombra-ingr" else x["rule_pct"]) > 0:
            x["verdict"] = "promising"
            if x["kind"] == "ingresso" and (x.get("own_lo") is None or x["own_lo"] <= 0): x["verdict"] = "promising (still losing)"
        else: x["verdict"] = "no evidence"
        if x["kind"] == "ingresso" and side_n < 5 and x["verdict"] != "too few": x["verdict"] = "too few"
        live, sh = x.pop("_tot")
        if x["verdict"].startswith("promising") and x["diff_ci"]:
            x["ci_lo"], x["ci_hi"] = x["diff_ci"]   # dashboard check mark only for BH-significant rules (values are % per trade)
    key = lambda x: -(x["diff_pct"] if x["diff_pct"] is not None else (x["rule_pct"] if x["rule_pct"] is not None else -1e9))
    rows.sort(key=key)
    notes = (f"{m} rules tested at once: with no real edge anywhere ~{FDR_Q:.0%} of BH 'promising' calls can still be luck; "
             f"verdicts need >= {MIN_N} trades on the rule side. Entry ghosts are priced by the 30 s poll: compare them with "
             f"the ghost_live_poll30 drift, not with live.")
    return dict(positions=len(pos), live_pnl=round(sum(v[0] for v in pos.values()), 2), rows=rows, stats=stats(db, where, args),
                drift=drift, notes=notes, metric="pct_per_trade")

def ci90(per, n=400):
    """90% bootstrap interval of the TOTAL (resampled trades). Used by --features."""
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
    for k, d in b["drift"].items():
        print(f"drift {k}: n={d['n']} media {d['mean_pp']:+.2f} pp, mediana {d['median_pp']:+.2f} pp, |drift|>2pp {d['share_over_2pp']}%, peggiore {d['worst_pp']:+.1f} pp")
    print("metrica: % per trade. ingresso = media dei trade che la regola LASCIA PASSARE vs media live; uscita = variante - baseline "
          "sugli stessi trade; ombra-ingr = media propria. q = Benjamini-Hochberg.")
    print(f"{'tipo':10} {'regola':30} {'n':>4} {'n regola':>8} {'live %':>7} {'regola %':>8} {'diff pp':>8} {'IC90 diff':>15} {'q':>6}  verdetto")
    for x in b["rows"]:
        lab = x["rule"] + (f" (blocca {x['blocked']})" if x["blocked"] is not None else "")
        f = lambda v, w=7: f"{v:+{w}.2f}" if v is not None else " " * (w - 1) + "—"
        ci = f"[{x['diff_ci'][0]:+.1f}, {x['diff_ci'][1]:+.1f}]" if x["diff_ci"] and x["kind"] != "ombra-ingr" else (
             f"[{x['diff_ci'][0]:+.1f}, {x['diff_ci'][1]:+.1f}]*" if x["diff_ci"] else "")
        print(f"{x['kind']:10} {lab[:30]:30} {x['n']:4d} {x['n_rule'] or 0:8d} {f(x['live_pct'])} {f(x['rule_pct'], 8)} {f(x['diff_pct'], 8)} "
              f"{ci:>15} {x['q_bh'] if x['q_bh'] is not None else '—':>6}  {x['verdict']}")
    print("* ombra-ingr: intervallo della media propria. " + b["notes"])
