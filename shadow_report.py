#!/usr/bin/env python3
"""Shadow rules scoreboard (READ ONLY, paper desk). Which shadow rules would have done better than the live rules?

  sudo python3 shadow_report.py [--db /opt/memedesk/desk.sqlite] [--ruleset v10_sel]

delta_usd > 0 means the shadow rule beats the live rules on the same trades. Manual closes are excluded.
  exit rules  : COMPARE rows (actual vs shadow PnL on the same position) and v9 ghosts vs ghost_live_baseline.
  entry gates : PnL of the closed live trades the gate would have blocked; delta = -that (blocking losers helps).
  shadow entry: v9 ghosts for coins the live desk did not buy (pure extra PnL with the live exit rules).
"""
import argparse, json, os, sqlite3
from collections import defaultdict

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=os.path.join(os.environ.get("MEMEDESK_DATA", "/opt/memedesk"), "desk.sqlite"))
ap.add_argument("--ruleset", help="only positions tagged with this ruleset (e.g. v8_loose, v10_sel)")
a = ap.parse_args()
db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
q = lambda s, p=(): db.execute(s, p).fetchall()
cols = {r[1] for r in q("PRAGMA table_info(positions)")}
where = "status='CLOSED' AND COALESCE(exit_reason,'') NOT LIKE 'manual_close%'"
if a.ruleset and "ruleset" in cols: where += f" AND ruleset='{a.ruleset}'"
pos = {r[0]: (r[1] or 0.0) for r in q(f"SELECT id, pnl_usd FROM positions WHERE {where}")}
print(f"posizioni chiuse considerate: {len(pos)}  PnL live totale: {sum(pos.values()):+.2f} $")
rows = []   # (kind, rule, n, live, shadow, delta)

for rule, in q("SELECT DISTINCT rule FROM shadow_events WHERE kind='COMPARE'"):
    seen = {}
    for pid, d in q("SELECT position_id, data FROM shadow_events WHERE rule=? AND kind='COMPARE'", (rule,)):
        d = json.loads(d)
        if pid in pos and d.get("shadow_pnl_usd") is not None and "actual_pnl_usd" in d:
            seen[pid] = (pos[pid], float(d["shadow_pnl_usd"]))
    if seen:
        live, sh = sum(v[0] for v in seen.values()), sum(v[1] for v in seen.values())
        rows.append(("uscita", rule, len(seen), live, sh, sh - live))

for rule, in q("SELECT DISTINCT rule FROM shadow_events WHERE kind='ENTRY_GATE'"):
    blk = {}
    for pid, d in q("SELECT position_id, data FROM shadow_events WHERE rule=? AND kind='ENTRY_GATE' AND position_id IS NOT NULL", (rule,)):
        d = json.loads(d)
        if pid in pos: blk[pid] = d.get("would_block") is True
    if blk:
        lost = sum(pos[p] for p, b in blk.items() if b)
        rows.append(("ingresso", rule + f" (blocca {sum(blk.values())})", len(blk), sum(pos[p] for p in blk), sum(pos[p] for p, b in blk.items() if not b), -lost))

try:
    g = q("SELECT rule, kind, position_id, hyp_pnl_usd FROM shadow_ghosts WHERE status='CLOSED' AND hyp_pnl_usd IS NOT NULL")
except sqlite3.OperationalError:
    g = []
base = {pid: p for r, k, pid, p in g if r == "ghost_live_baseline"}
by = defaultdict(dict)
for r, k, pid, p in g:
    if k == "entry": by[("entry", r)][len(by[("entry", r)])] = p
    elif r != "ghost_live_baseline" and pid in base: by[("exit", r)][pid] = p
for (k, r), d in sorted(by.items()):
    if k == "entry": rows.append(("ombra-ingr", r, len(d), 0.0, sum(d.values()), sum(d.values())))
    else: rows.append(("ghost-usc", r, len(d), sum(base[p] for p in d), sum(d.values()), sum(d.values()) - sum(base[p] for p in d)))

print(f"{'tipo':10} {'regola':38} {'n':>4} {'live $':>9} {'ombra $':>9} {'delta $':>9}")
for k, r, n, live, sh, dl in sorted(rows, key=lambda x: -x[5]):
    print(f"{k:10} {r[:38]:38} {n:4d} {live:+9.2f} {sh:+9.2f} {dl:+9.2f}{'  <-- meglio' if dl > 0 else ''}")
