#!/usr/bin/env python3
"""Morning report for MemeDesk (paper). Usage: python3 report.py [--hours N]"""
import json, sqlite3, sys, os, time
from collections import Counter
BASE = os.path.dirname(os.path.abspath(__file__))

def bucket(r):
    r = r.lower()
    for key, lab in [("too young", "Scout: age < 30m"), ("too old", "Scout: age > 72h"), ("mcap", "Scout: mcap out of $300k-$20M"),
                     ("liquidity $", "Filter: liquidity < $40k"), ("1h volume", "Filter: 1h volume < $50k"), ("already +", "Filter: already +80% in 1h"),
                     ("mint authority", "Filter: mint authority not revoked"), ("freeze authority", "Filter: freeze authority not revoked"),
                     ("lp not burned", "Filter: LP not burned/locked"), ("top-10", "Filter: top-10 > 25%"), ("dev holds", "Filter: dev > 3%"),
                     ("past rugs", "Filter: dev past rugs"), ("rugged", "Filter: RugCheck rugged"), ("bundled", "Filter: bundled/insiders > 10%"),
                     ("score", "Analyst: score < 70"), ("data unavailable", "data unavailable (fail-closed)"),
                     ("transfer fee", "Filter: transfer fee"), ("permanent delegate", "Filter: permanent delegate")]:
        if key in r: return lab
    return "Executor/other: " + r[:50]

def build_report(db_path=os.path.join(BASE, "desk.sqlite"), since_hours=None):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    t0 = time.time() - since_hours * 3600 if since_hours else 0
    L = []; P = L.append
    win = f"last {since_hours}h" if since_hours else "since start"
    P(f"MEMEDESK PAPER REPORT — generated {time.strftime('%Y-%m-%d %H:%M %Z')} ({win})")
    P("PAPER TRADING ONLY — no wallet, no keys, no real orders.\n")
    cyc = db.execute("SELECT COUNT(*),MIN(ts_iso),MAX(ts_iso) FROM cycles WHERE ts>=?", (t0,)).fetchone()
    P(f"Cycles: {cyc[0]}  ({cyc[1]} → {cyc[2]})")
    stats = [json.loads(s) for (s,) in db.execute("SELECT stats FROM cycles WHERE ts>=?", (t0,))]
    if stats:
        P(f"Avg per cycle: discovered {sum(s.get('discovered',0) for s in stats)/len(stats):.0f} mints, "
          f"{sum(s.get('in_window',0) for s in stats)/len(stats):.1f} in age/mcap window, "
          f"{sum(s.get('filter_pass',0) for s in stats)/len(stats):.2f} passed Filter, "
          f"{sum(s.get('analyst_pass',0) for s in stats)/len(stats):.2f} scored 70+")
    uniq = db.execute("SELECT COUNT(DISTINCT mint) FROM kills WHERE ts>=?", (t0,)).fetchone()[0]
    uniq_s = db.execute("SELECT COUNT(DISTINCT mint) FROM scores WHERE ts>=?", (t0,)).fetchone()[0]
    P(f"Unique coins seen: {len(set(m for (m,) in db.execute('SELECT mint FROM kills WHERE ts>=? UNION SELECT mint FROM scores WHERE ts>=?', (t0, t0))))}"
      f"  | unique coins killed at least once: {uniq} | unique coins scored: {uniq_s}")
    # kills by stage & reason
    kc, kr = Counter(), Counter()
    for stage, reasons in db.execute("SELECT stage,reasons FROM kills WHERE ts>=?", (t0,)):
        kc[stage] += 1
        for r in json.loads(reasons): kr[bucket(r)] += 1
    P(f"\nKill events by stage: " + ", ".join(f"{k} {v}" for k, v in kc.most_common()))
    P("Kill reasons (a kill can carry several):")
    for k, v in kr.most_common(): P(f"  {v:6d}  {k}")
    P("\nLatest Filter kills (on-chain checks):")
    for ts, sym, mint, reasons in db.execute("SELECT ts_iso,symbol,mint,reasons FROM kills WHERE stage='Filter' AND ts>=? AND metrics NOT LIKE '%skipped%' ORDER BY id DESC LIMIT 8", (t0,)):
        P(f"  {ts[11:16]} {sym:<10} {mint[:6]}…  {'; '.join(json.loads(reasons))}")
    P("\nScores (best per coin):")
    for sym, mint, sc, comp, n in db.execute("""SELECT symbol,mint,MAX(score),components,COUNT(*) FROM scores WHERE ts>=? GROUP BY mint ORDER BY MAX(score) DESC LIMIT 12""", (t0,)):
        c = json.loads(comp); parts = " ".join(f"{k[:5]}={c[k]}" for k in ("momentum", "unique_buyers", "buy_sell", "holder_growth", "liq_mcap") if k in c)
        P(f"  {sc:5.1f} {'PASS' if sc >= 70 else 'fail'} {sym:<10} {mint[:6]}…  ({n}x)  {parts}")
    P("\nTrades:")
    rows = db.execute("SELECT symbol,mint,status,opened_iso,entry_price,size_usd,stop_price,peak_price,trail_active,last_price,closed_iso,exit_price,exit_reason,pnl_usd,pnl_pct,score FROM positions WHERE opened_ts>=? OR status='OPEN' ORDER BY id", (t0,)).fetchall()
    if not rows: P("  none")
    for r in rows:
        sym, mint, st, oi, ep, sz, sp, pk, tr, lp, ci, xp, xr, pnl, pp, sc = r
        if st == "OPEN":
            upnl = (lp / ep - 1) * 100
            P(f"  OPEN   {sym:<10} {mint[:6]}… opened {oi[5:16]} size ${sz:,.0f} entry {ep:.8g} last {lp:.8g} ({upnl:+.1f}%) stop {sp:.8g} {'TRAIL' if tr else 'fixed'} score {sc}")
        else:
            P(f"  CLOSED {sym:<10} {mint[:6]}… {oi[5:16]}→{ci[5:16]} size ${sz:,.0f} {ep:.8g}→{xp:.8g} {xr}  PnL ${pnl:+,.2f} ({pp:+.1f}%)")
    cash = db.execute("SELECT v FROM ledger WHERE k='cash'").fetchone()[0]
    mtm = sum(q * (lp or ep) for q, lp, ep in db.execute("SELECT qty,last_price,entry_price FROM positions WHERE status='OPEN'"))
    real = db.execute("SELECT COALESCE(SUM(pnl_usd),0),COUNT(*),SUM(pnl_usd>0) FROM positions WHERE status='CLOSED'").fetchone()
    P(f"\nPaper equity: ${cash + mtm:,.2f} (cash ${cash:,.2f} + open MTM ${mtm:,.2f}) vs start $10,000 → {(cash + mtm - 10000) / 100:+.2f}%")
    P(f"Realised PnL: ${real[0]:+,.2f} over {real[1]} closed trades ({real[2] or 0} winners)")
    return "\n".join(L) + "\n"

if __name__ == "__main__":
    h = float(sys.argv[sys.argv.index("--hours") + 1]) if "--hours" in sys.argv else None
    print(build_report(since_hours=h))
