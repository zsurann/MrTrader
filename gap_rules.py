"""v17 SHADOW-ONLY rules against gap-through-stop losses (log only; the live rules do not use them).

Trigger: trade 281 'Unemployed' (v14) hit the -10% stop but filled at -21.7% after 39 s: the price gapped past the stop.
A "gap loss" here is a closed trade whose realized PnL is worse than GAP_LOSS_PCT (-12%).

Pure functions, used three ways:
  live      desk.py logs gates() as ENTRY_GATE shadow events and risk_mult() as a SIZE event for every live entry;
  features  stored in entry_meta["gap"] so later backfills have everything;
  backfill  backfill(db) rebuilds the same features for old trades from entry_meta + the FILTER_PASS event + old shadow events.
"""
import json
from collections import defaultdict

GAP_LOSS_PCT = -12.0

# name -> one-line rule text (what the trade must satisfy to be ALLOWED)
RULES = dict(
    liq_40k="pool liquidity at entry >= $40k",
    liq_75k="pool liquidity at entry >= $75k",
    h1_pump_15="1h price change <= +15% (no chasing a pump)",
    h1_pump_30="1h price change <= +30% (no chasing a pump)",
    m5_pump_8="5m price change <= +8% (stricter than the shadow m5_chase_10, +10%)",
    lp_burn_100="primary LP >= 99.5% burned/locked and >= 95% of all pool liquidity locked (live: 90% / 80%)",
    top10_max20="top-10 holders (ex pools/burn) < 20% (live: <= 35%)",
    dev_zero="creator/dev holds <= 0.1% of supply (live: <= 5%)",
    liq_drop_2="liquidity not down > 2% vs the oldest sample >= 20 min ago (live: 5%)",
    liq_peak_drop_10="liquidity not > 10% below its 2h peak (no recent large liquidity removal)",
    rc_score_10="RugCheck normalised risk score <= 10 (1 = no risks found)",
    holders_2000="at least 2000 holders (RugCheck totalHolders)",
    creator_no_dead="creator has no prior token now < $10k mcap (live blocks at 2+)",
    creator_first="creator has no prior tokens at all (no serial launcher)",
    anti_rug_strict="all of lp_burn_100, top10_max20, dev_zero, liq_drop_2, creator_no_dead",
)
STRICT_PARTS = ("lp_burn_100", "top10_max20", "dev_zero", "liq_drop_2", "creator_no_dead")

# risk-scaled sizing: multiplier on the live size (floor 0.25); factors multiply
SIZE_STEPS = dict(liq=((40_000, 0.5), (75_000, 0.75)),   # liquidity below -> factor
                  h1=((30.0, 0.5), (15.0, 0.75)),        # 1h change above -> factor
                  vol=((8.0, 0.5), (4.0, 0.75)))         # |5m change| above -> factor (volatility proxy)
SIZE_FLOOR = 0.25

def _f(x):
    try: return None if x is None else float(x)
    except (TypeError, ValueError): return None

def features_live(c, fp, entry_liq, liq_hist, ts):
    """Flat feature dict for one live Analyst pass. entry_liq = fresh DexScreener liquidity of the entry (or Scout liq)."""
    ck = fp.checks or {}; rc = fp.rugcheck or {}; pc = c.price_change or {}
    hist = liq_hist.get(c.mint) or []
    old = [l for t, l in hist if ts - t >= 20 * 60]
    chg = (100 * (c.liq_usd / old[0] - 1)) if (old and old[0] and c.liq_usd) else None
    peak = max((l for _, l in hist if l), default=None)
    pdrop = (100 * (1 - c.liq_usd / peak)) if (peak and c.liq_usd) else None
    return dict(liq=_f(entry_liq or c.liq_usd), h1=_f(pc.get("h1")), m5=_f(pc.get("m5")),
                lp_primary=_f(ck.get("lp_primary_locked_pct")), lp_weighted=_f(ck.get("lp_weighted_locked_pct")),
                top10=_f(ck.get("top10_pct_ex_pools")), dev_pct=_f(ck.get("dev_pct")),
                liq_chg_pct=None if chg is None else round(chg, 2), liq_peak_drop_pct=None if pdrop is None else round(pdrop, 2),
                rc_score=_f(rc.get("score")), holders=_f(rc.get("totalHolders")),
                creator_prior=_f(ck.get("creator_prior_tokens")), creator_dead=_f(ck.get("creator_dead_tokens")))

def _lt(v, lim): return None if v is None else v < lim
def _gt(v, lim): return None if v is None else v > lim

def gates(f):
    """-> {rule: (value, would_block True/False/None=undecidable, text)}."""
    lp_b = None
    if f.get("lp_primary") is not None:
        lp_b = f["lp_primary"] < 99.5 or (f.get("lp_weighted") is not None and f["lp_weighted"] < 95.0)
    g = dict(
        liq_40k=(f.get("liq"), _lt(f.get("liq"), 40_000)),
        liq_75k=(f.get("liq"), _lt(f.get("liq"), 75_000)),
        h1_pump_15=(f.get("h1"), _gt(f.get("h1"), 15.0)),
        h1_pump_30=(f.get("h1"), _gt(f.get("h1"), 30.0)),
        m5_pump_8=(f.get("m5"), _gt(f.get("m5"), 8.0)),
        lp_burn_100=(dict(primary=f.get("lp_primary"), weighted=f.get("lp_weighted")), lp_b),
        top10_max20=(f.get("top10"), None if f.get("top10") is None else f["top10"] >= 20.0),
        dev_zero=(f.get("dev_pct"), _gt(f.get("dev_pct"), 0.1)),
        liq_drop_2=(f.get("liq_chg_pct"), _lt(f.get("liq_chg_pct"), -2.0)),
        liq_peak_drop_10=(f.get("liq_peak_drop_pct"), _gt(f.get("liq_peak_drop_pct"), 10.0)),
        rc_score_10=(f.get("rc_score"), _gt(f.get("rc_score"), 10.0)),
        holders_2000=(f.get("holders"), _lt(f.get("holders"), 2000)),
        creator_no_dead=(f.get("creator_dead"), None if f.get("creator_dead") is None else f["creator_dead"] >= 1),
        creator_first=(f.get("creator_prior"), None if f.get("creator_prior") is None else f["creator_prior"] >= 1),
    )
    parts = [g[k][1] for k in STRICT_PARTS]
    sb = True if any(p is True for p in parts) else (False if all(p is False for p in parts) else None)
    g["anti_rug_strict"] = ({k: g[k][1] for k in STRICT_PARTS}, sb)
    return {k: (v, b, RULES[k]) for k, (v, b) in g.items()}

def risk_mult(f):
    """-> (multiplier on the live size, {factor: value}). Missing data counts as factor 1."""
    fac = {}
    liq, h1, m5 = f.get("liq"), f.get("h1"), f.get("m5")
    fac["liq"] = next((x for lim, x in SIZE_STEPS["liq"] if liq is not None and liq < lim), 1.0)
    fac["h1"] = next((x for lim, x in SIZE_STEPS["h1"] if h1 is not None and h1 > lim), 1.0)
    fac["vol"] = next((x for lim, x in SIZE_STEPS["vol"] if m5 is not None and abs(m5) > lim), 1.0)
    m = 1.0
    for v in fac.values(): m *= v
    return round(max(SIZE_FLOOR, m), 4), fac

# ---------------------------------------------------------------- backfill on old trades
def backfill(db, rulesets=None):
    """Rebuild the features of closed (non-manual) trades from what was logged at entry time.
    -> list of dict(id, ruleset, pnl_pct, pnl_usd, size_usd, f, src) . Sources:
      entry_meta (liq_usd fresh at entry, chg_h1, chg_m5, lp_*, top10, dev_pct), the last FILTER_PASS event of the mint
      before the entry (creator tokens), old shadow events (liq_growth value -> liq_chg_pct, holders_300 value -> holders),
      entry_meta["gap"] for trades opened since v17. RugCheck score and the 2h liquidity peak were not logged before v17."""
    q = lambda s, p=(): db.execute(s, p).fetchall()
    where = "status='CLOSED' AND COALESCE(exit_reason,'') NOT LIKE 'manual_close%' AND entry_meta IS NOT NULL AND ruleset IS NOT NULL"
    args = []
    if rulesets:
        where += " AND ruleset IN (%s)" % ",".join("?" * len(rulesets)); args += list(rulesets)
    pos = q(f"SELECT id, mint, opened_ts, ruleset, pnl_pct, pnl_usd, size_usd, entry_meta FROM positions WHERE {where} ORDER BY opened_ts", args)
    if not pos: return []
    mints = {p[1] for p in pos}
    fpass = defaultdict(list)
    for m, ts, d in q("SELECT mint, ts, data FROM events WHERE kind='FILTER_PASS'"):
        if m in mints: fpass[m].append((ts, d))
    sev = defaultdict(dict)
    for rule, pid, d in q("SELECT rule, position_id, data FROM shadow_events WHERE kind='ENTRY_GATE' AND position_id IS NOT NULL "
                          "AND rule IN ('liq_growth', 'holders_300')"):
        try: sev[pid][rule] = json.loads(d).get("value")
        except Exception: pass
    out = []
    for pid, mint, ots, rs, pct, usd, size, meta in pos:
        try: m = json.loads(meta)
        except Exception: continue
        if isinstance(m.get("gap"), dict):
            out.append(dict(id=pid, ruleset=rs, pnl_pct=pct or 0.0, pnl_usd=usd or 0.0, size_usd=size or 0.0, f=m["gap"], src="live")); continue
        ck = {}
        before = [x for x in fpass.get(mint, []) if x[0] <= (ots or 0) + 1]
        if before:
            try: ck = json.loads(max(before)[1]).get("checks") or {}
            except Exception: ck = {}
        lg = sev.get(pid, {}).get("liq_growth"); h = sev.get(pid, {}).get("holders_300")
        f = dict(liq=_f(m.get("liq_usd")), h1=_f(m.get("chg_h1")), m5=_f(m.get("chg_m5")),
                 lp_primary=_f(m.get("lp_primary")), lp_weighted=_f(m.get("lp_weighted")),
                 top10=_f(m.get("top10")), dev_pct=_f(m.get("dev_pct")),
                 liq_chg_pct=_f(lg) if not isinstance(lg, dict) else None, liq_peak_drop_pct=None, rc_score=None,
                 holders=_f(h) if not isinstance(h, dict) else None,
                 creator_prior=_f(ck.get("creator_prior_tokens")), creator_dead=_f(ck.get("creator_dead_tokens")))
        out.append(dict(id=pid, ruleset=rs, pnl_pct=pct or 0.0, pnl_usd=usd or 0.0, size_usd=size or 0.0, f=f, src="backfill"))
    return out
