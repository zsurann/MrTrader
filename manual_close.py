#!/usr/bin/env python3
"""
PAPER ONLY. Close every OPEN paper position at the current market price, with the desk's own paper sell fill
(Jupiter quote with the live sanity band, else the slippage model), reason 'manual_close_deploy'.
Used before a deploy so `memedesk-safe-restart` can run with 0 positions open.

- Writes the same columns as a live Risk close (+ EXIT event with manual=True, stats_excluded=True).
- Conditional write (status='OPEN') and the cash credit only when the row really changed, so a race with
  a live stop cannot double-count. Ledger invariant (cash = start + closed PnL - open size) is checked after.
- Shadow COMPARE rows are left to the desk's own shadow_reconcile (it backfills them for closed positions);
  the actual_reason they carry is 'manual_close_deploy', so analysis can exclude them.
Run as the memedesk user with the unit's EnvironmentFile (Jupiter key for the quote):
  sudo systemd-run --wait --pipe -q -p User=memedesk -p WorkingDirectory=/opt/memedesk \
       -p EnvironmentFile=/etc/memedesk/memedesk.env /usr/bin/python3 /opt/memedesk/manual_close.py [--dry-run]
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desk as D

REASON = "manual_close_deploy"

def invariant(db):
    cash = db.execute("SELECT v FROM ledger WHERE k='cash'").fetchone()[0]
    closed = db.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM positions WHERE status='CLOSED'").fetchone()[0]
    opened = db.execute("SELECT COALESCE(SUM(size_usd),0) FROM positions WHERE status='OPEN'").fetchone()[0]
    return cash, D.CFG["start_equity"] + closed - opened

def main(dry=False):
    log = D.Logger()
    db = log.db
    rows = db.execute("SELECT id,mint,symbol,pair,entry_price,qty,size_usd,peak_price FROM positions WHERE status='OPEN'").fetchall()
    c0, e0 = invariant(db)
    print(f"open={len(rows)} ledger_cash={c0:.2f} expected={e0:.2f}")
    if not rows: return 0
    pairs = sorted({r[3] for r in rows}); prices = {}
    for i in range(0, len(pairs), 30):
        d = D.HTTP.get("https://api.dexscreener.com/latest/dex/pairs/solana/" + ",".join(pairs[i:i + 30])) or {}
        for p in d.get("pairs") or []:
            prices[p["pairAddress"]] = (float(p.get("priceUsd") or 0), (p.get("liquidity") or {}).get("usd") or 0)
    out = []
    for pid, mint, sym, pair, ep, qty, size, peak in rows:
        px, liq = prices.get(pair, (0, 0))
        if not px or not liq:
            print(f"#{pid} {sym}: no market price, left OPEN"); continue
        exit_px, src, model_px, q = D.paper_sell_fill(mint, qty, px, liq)
        proceeds = exit_px * qty; pnl = proceeds - size; pct = 100 * pnl / size
        if dry:
            print(f"DRY #{pid} {sym}: ref {px:.8g} fill {exit_px:.8g} ({src}) pnl {pnl:+.2f} ({pct:+.2f}%)"); continue
        with log.lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute("""UPDATE positions SET status='CLOSED',last_price=?,last_ts=?,peak_price=MAX(peak_price,?),closed_ts=?,closed_iso=?,
                    exit_ref=?,exit_price=?,exit_reason=?,pnl_usd=?,pnl_pct=? WHERE id=? AND status='OPEN'""",
                    (px, D.now(), px, D.now(), D.iso(), px, exit_px, REASON, pnl, pct, pid))
                changed = cur.rowcount == 1
                if changed: db.execute("UPDATE ledger SET v=v+? WHERE k='cash'", (proceeds,))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK"); raise
        if not changed:
            print(f"#{pid} {sym}: already closed by the desk meanwhile, skipped"); continue
        log.event("Risk", "EXIT", mint, sym, reason=REASON, manual=True, stats_excluded=True, ref_price=px, exit_price=exit_px,
                  entry=ep, pnl_usd=round(pnl, 2), pnl_pct=round(pct, 2), fill_src=src, trigger_src="manual")
        out.append((pid, sym, round(pnl, 2), round(pct, 2), src))
        print(f"CLOSED #{pid} {sym}: ref {px:.8g} fill {exit_px:.8g} ({src}) pnl {pnl:+.2f} ({pct:+.2f}%)")
    c1, e1 = invariant(db)
    nopen = db.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
    print(f"after: open={nopen} ledger_cash={c1:.2f} expected={e1:.2f} "
          f"drift={c1 - e1:+.4f} (before {c0 - e0:+.4f})")
    print(json.dumps(out))
    return 0

if __name__ == "__main__":
    sys.exit(main(dry="--dry-run" in sys.argv))
