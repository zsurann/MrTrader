#!/usr/bin/env python3
"""Dump a MemeDesk dashboard snapshot as JSON.

Usage:
  python3 dashboard_data.py              # write JSON to stdout
  python3 dashboard_data.py -o FILE      # write JSON to FILE (and still print nothing else)
  python3 dashboard_data.py --pretty     # indent stdout (default when writing to a TTY)

The snapshot is meant to be copied into the MemeDesk Page so it can be
refreshed later without inventing numbers. All counts come from desk.sqlite
(and the live PID files). Paper trading only.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "desk.sqlite")
PID_PATH = os.path.join(BASE, "desk.pid")
SUPERVISOR_PID_PATH = os.path.join(BASE, "supervisor.pid")
TZ = ZoneInfo("Europe/Rome")
MONTHS_IT = (
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
)

# Order matters: more specific patterns first. "mcap" must come after
# "past rugs" because rug-proxy reasons also contain the word "mcap".
REASON_BUCKETS = (
    ("too young", "age_lt_30m", "Scout: età < 30 min"),
    ("too old", "age_gt_72h", "Scout: età > 72 ore"),
    ("past rugs", "dev_past_rugs", "Filter: rug precedenti del dev"),
    ("rugged", "rugged", "Filter: segnalato rugged da RugCheck"),
    ("mcap", "mcap_out", "Scout: mcap fuori da 300k–20M $"),
    ("liquidity $", "liq_lt_40k", "Filter: liquidità < 40k $"),
    ("1h volume", "vol_lt_50k", "Filter: volume 1h < 50k $"),
    ("already +", "already_up_80", "Filter: già +80% in 1h"),
    ("mint authority", "mint_auth", "Filter: mint authority non revocata"),
    ("freeze authority", "freeze_auth", "Filter: freeze authority non revocata"),
    ("lp not burned", "lp_not_locked", "Filter: LP non bruciata/bloccata"),
    ("top-10", "top10", "Filter: top 10 > 25%"),
    ("dev holds", "dev_gt_3", "Filter: dev > 3%"),
    ("bundled", "bundled", "Filter: bundled/insider > 10%"),
    ("score", "score_lt_70", "Analyst: punteggio < 70"),
    ("data unavailable", "data_unavailable", "Dati non disponibili (fail-closed)"),
    ("transfer fee", "transfer_fee", "Filter: transfer fee"),
    ("permanent delegate", "perm_delegate", "Filter: permanent delegate"),
    ("re-entry cooldown", "reentry_cooldown", "Executor: cooldown di rientro"),
)

COMPONENT_KEYS = (
    "momentum",
    "unique_buyers",
    "buy_sell",
    "holder_growth",
    "liq_mcap",
    "smart_wallets",
)

STAGE_ORDER = ("Scout", "Filter", "Analyst", "Executor")

# Static desk parameters and known gaps (also confirmed from latest DESK_START).
RULES = {
    "mode": "PAPER",
    "note_it": "Solo paper trading: nessun wallet, nessuna chiave, nessun ordine reale.",
    "parameters": [
        {"key": "age_window", "label_it": "Finestra di età della pair (Scout)", "value": "30 minuti – 72 ore"},
        {"key": "mcap_window", "label_it": "Finestra di market cap (Scout)", "value": "300.000 – 20.000.000 $"},
        {"key": "min_liq", "label_it": "Liquidità minima (Filter)", "value": "100.000 $"},
        {"key": "min_vol_h1", "label_it": "Volume 1h minimo (Filter)", "value": "50.000 $"},
        {"key": "max_h1_change", "label_it": "Già salita troppo in 1h (Filter)", "value": "+80%"},
        {"key": "mint_freeze", "label_it": "Mint / freeze authority (Filter)", "value": "Devono essere revocate"},
        {"key": "lp_lock", "label_it": "LP bruciata o bloccata (Filter)", "value": "Pair primaria ≥ 90%, oppure media ponderata su tutte le pool ≥ 80%"},
        {"key": "max_top10_pct", "label_it": "Top 10 holder (esclusi AMM/locker/vault/burn)", "value": "≤ 25%"},
        {"key": "max_dev_pct", "label_it": "Quota del dev", "value": "≤ 3%"},
        {"key": "max_bundle_pct", "label_it": "Lancio bundled / insider", "value": "≤ 10%"},
        {"key": "pass_score", "label_it": "Soglia Analyst", "value": "70 / 100 (sotto, kill)"},
        {"key": "weights_effective", "label_it": "Pesi Analyst (v6: lo smart wallet può solo aggiungere)", "value": "base = momentum 0,235; buy/sell 0,235; unique buyers 0,176; holder growth 0,176; liq/mcap 0,176. Poi + 0,15 × (smart/100) × (100 − base): 0 smart wallet = nessuna penalità, 3+ = come prima"},
        {"key": "start_equity", "label_it": "Equity iniziale paper", "value": "10.000 $"},
        {"key": "max_pos_frac", "label_it": "Taglia massima per trade (Executor)", "value": "5% dell'equity paper, non oltre il cash e non oltre lo 0,25% della liquidità del pool"},
        {"key": "max_open", "label_it": "Posizioni aperte massime", "value": "10"},
        {"key": "reentry_cooldown_h", "label_it": "Cooldown di rientro sullo stesso mint", "value": "6 ore"},
        {"key": "stop_pct", "label_it": "Stop loss (scritto prima dell'ingresso)", "value": "−10% sul prezzo di mercato all'ingresso (non sul fill)"},
        {"key": "trail", "label_it": "Trailing stop (Risk)", "value": "Si attiva a +12% sul prezzo di mercato all'ingresso, poi stop = max(stop, picco × 0,94)"},
        {"key": "scan_interval_s", "label_it": "Intervallo tra i cicli Scout", "value": "180 secondi"},
        {"key": "risk_poll_s", "label_it": "Polling Risk", "value": "30 secondi"},
        {"key": "fill_model", "label_it": "Modello di fill paper", "value": "Prezzo DexScreener × (1 + slippage 0,5% + fee 0,25% + impatto size/(liq/2)). Le uscite usano lo stesso modello."},
    ],
    "data_gaps": [
        {
            "key": "smart_wallets",
            "label_it": "Smart wallets",
            "detail_it": "Non esiste un feed gratuito di smart-money. Il peso è 0 e gli altri cinque fattori sono rinormalizzati (momentum 0,235, buy/sell 0,235, unique buyers / holder growth / liq/mcap 0,176 ciascuno).",
        },
        {
            "key": "bundled_proxy",
            "label_it": "Lanci bundled",
            "detail_it": "Stima per proxy da RugCheck: max tra la somma della supply dei top holder segnalati come insider e la quota insiderNetworks, rispetto alla supply. Kill se > 10%.",
        },
        {
            "key": "dev_past_rugs_proxy",
            "label_it": "Rug precedenti del dev",
            "detail_it": "Oltre al flag rugged / rischi rug di RugCheck, proxy = il creator ha almeno 2 altri token ora sotto i 10.000 $ di market cap.",
        },
        {
            "key": "pumpfun_blocked",
            "label_it": "Pump.fun",
            "detail_it": "Pump.fun blocca l'accesso automatizzato, quindi non è una fonte di scoperta. Scout usa DexScreener, Jupiter e GeckoTerminal.",
        },
        {
            "key": "fail_closed",
            "label_it": "Dati mancanti",
            "detail_it": "Se un controllo non ottiene i dati che gli servono, il token viene ucciso (fail-closed) e il motivo viene registrato.",
        },
    ],
}


def bucket(reason: str) -> tuple[str, str]:
    low = reason.lower()
    for key, code, label_it in REASON_BUCKETS:
        if key in low:
            return code, label_it
    return "other", "Altro: " + reason[:80]


def parse_iso(ts_iso: str | None) -> datetime | None:
    if not ts_iso:
        return None
    s = ts_iso.strip()
    # desk writes +0100; fromisoformat wants +01:00
    if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def format_it(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return f"{dt.day} {MONTHS_IT[dt.month - 1]} {dt.year}, {dt.strftime('%H:%M:%S')} Europe/Rome"


def format_it_short(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%H:%M:%S")


def short_mint(mint: str | None, n: int = 6) -> str:
    if not mint:
        return ""
    if len(mint) <= n + 1:
        return mint
    return mint[:n] + "…"


def pid_alive(path: str) -> dict:
    info = {"pid": None, "running": False, "cmdline": None}
    try:
        with open(path) as f:
            raw = f.read().strip()
        if not raw:
            return info
        pid = int(raw.split()[0])
        info["pid"] = pid
    except (OSError, ValueError):
        return info
    try:
        os.kill(pid, 0)
        info["running"] = True
    except (OSError, ProcessLookupError):
        return info
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            info["cmdline"] = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        pass
    return info


def connect_ro(db_path: str) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def jload(value, default=None):
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def load_cfg(db: sqlite3.Connection) -> dict:
    row = db.execute(
        "SELECT data FROM events WHERE kind='DESK_START' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}
    data = jload(row[0], {}) or {}
    return data.get("cfg") or {}


def build_snapshot(db_path: str = DB_PATH) -> dict:
    generated = datetime.now(TZ)
    desk = pid_alive(PID_PATH)
    supervisor = pid_alive(SUPERVISOR_PID_PATH)
    running = bool(desk["running"] and desk.get("cmdline") and "desk.py" in (desk.get("cmdline") or ""))
    if desk["running"] and not desk.get("cmdline"):
        running = True

    if not os.path.exists(db_path):
        return {
            "generated_at": generated.isoformat(),
            "generated_at_it": format_it(generated),
            "timezone": "Europe/Rome",
            "paper_only": True,
            "error": f"database not found: {db_path}",
            "status": {"running": running, "desk": desk, "supervisor": supervisor},
        }

    db = connect_ro(db_path)
    try:
        cfg = load_cfg(db)
        start_equity = float(cfg.get("start_equity") or 10000.0)
        pass_score = float(cfg.get("pass_score") or 70.0)

        cycles_rows = list(db.execute("SELECT cycle, ts, ts_iso, stats FROM cycles ORDER BY cycle"))
        cycles = []
        for r in cycles_rows:
            stats = jload(r["stats"], {}) or {}
            dt = parse_iso(r["ts_iso"])
            cycles.append({
                "cycle": r["cycle"],
                "ts": r["ts"],
                "ts_iso": r["ts_iso"],
                "time_it": format_it(dt),
                "time_short": format_it_short(dt),
                "stats": stats,
            })

        last_start = db.execute(
            "SELECT cycle, ts_iso FROM events WHERE kind='CYCLE_START' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_done = db.execute(
            "SELECT cycle, ts_iso FROM events WHERE kind='CYCLE_DONE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        completed_cycles = len(cycles)
        last_completed_cycle = cycles[-1]["cycle"] if cycles else 0
        last_completed_iso = cycles[-1]["ts_iso"] if cycles else None
        last_completed_it = cycles[-1]["time_it"] if cycles else None
        cycle_in_progress = False
        in_progress_cycle = None
        in_progress_started_it = None
        if last_start and (not last_done or last_start["cycle"] > (last_done["cycle"] or 0)):
            cycle_in_progress = True
            in_progress_cycle = last_start["cycle"]
            in_progress_started_it = format_it(parse_iso(last_start["ts_iso"]))

        cash_row = db.execute("SELECT v FROM ledger WHERE k='cash'").fetchone()
        cash = float(cash_row[0]) if cash_row else start_equity

        open_rows = list(db.execute(
            "SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_ts"
        ))
        closed_rows = list(db.execute(
            "SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_ts"
        ))

        def pos_dict(r, closed=False):
            entry = r["entry_price"]
            last = r["last_price"] if r["last_price"] is not None else entry
            qty = r["qty"] or 0
            size = r["size_usd"] or 0
            if closed:
                pnl_usd = r["pnl_usd"]
                pnl_pct = r["pnl_pct"]
            else:
                mtm = qty * (last or 0)
                pnl_usd = round(mtm - size, 2) if entry else 0.0
                pnl_pct = round(((last / entry) - 1) * 100, 2) if entry else None
            return {
                "id": r["id"],
                "symbol": r["symbol"],
                "mint": r["mint"],
                "mint_short": short_mint(r["mint"]),
                "pair": r["pair"],
                "status": r["status"],
                "opened_iso": r["opened_iso"],
                "opened_it": format_it(parse_iso(r["opened_iso"])),
                "entry_price": r["entry_price"],
                "current_price": None if closed else last,
                "stop_price": r["stop_price"],
                "initial_stop": r["initial_stop"],
                "peak_price": r["peak_price"],
                "trail_active": bool(r["trail_active"]),
                "trail_status_it": ("trailing attivo" if r["trail_active"] else "stop fisso (−6%)") if not closed else None,
                "size_usd": r["size_usd"],
                "qty": r["qty"],
                "score": r["score"],
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "closed_iso": r["closed_iso"] if closed else None,
                "closed_it": format_it(parse_iso(r["closed_iso"])) if closed else None,
                "exit_price": r["exit_price"] if closed else None,
                "exit_reason": r["exit_reason"] if closed else None,
            }

        open_positions = [pos_dict(r, closed=False) for r in open_rows]
        closed_trades = [pos_dict(r, closed=True) for r in closed_rows]

        mtm = sum((r["qty"] or 0) * ((r["last_price"] if r["last_price"] is not None else r["entry_price"]) or 0)
                  for r in open_rows)
        equity = cash + mtm
        unrealized = mtm - sum((r["size_usd"] or 0) for r in open_rows)
        realized = db.execute(
            "SELECT COALESCE(SUM(pnl_usd),0), COUNT(*), COALESCE(SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END),0) FROM positions WHERE status='CLOSED'"
        ).fetchone()
        realized_pnl = float(realized[0] or 0)
        closed_count = int(realized[1] or 0)
        winners = int(realized[2] or 0)

        # Funnel
        def sum_stat(key):
            return sum(float(c["stats"].get(key) or 0) for c in cycles)

        last_stats = cycles[-1]["stats"] if cycles else {}
        unique_kills = {m for (m,) in db.execute("SELECT DISTINCT mint FROM kills") if m}
        unique_scores = {m for (m,) in db.execute("SELECT DISTINCT mint FROM scores") if m}
        unique_seen = unique_kills | unique_scores
        unique_scout_kill = {m for (m,) in db.execute("SELECT DISTINCT mint FROM kills WHERE stage='Scout'") if m}
        unique_filter_kill = {m for (m,) in db.execute("SELECT DISTINCT mint FROM kills WHERE stage='Filter'") if m}
        unique_analyst_kill = {m for (m,) in db.execute("SELECT DISTINCT mint FROM kills WHERE stage='Analyst'") if m}
        unique_passed_scout = unique_filter_kill | unique_scores  # in-window coins
        unique_passed_filter = unique_scores
        unique_passed_analyst = {m for (m,) in db.execute("SELECT DISTINCT mint FROM scores WHERE passed=1") if m}
        unique_entries = {m for (m,) in db.execute("SELECT DISTINCT mint FROM positions") if m}

        kill_events_by_stage = {st: 0 for st in STAGE_ORDER}
        unique_kills_by_stage = {st: 0 for st in STAGE_ORDER}
        for st, n, u in db.execute("SELECT stage, COUNT(*), COUNT(DISTINCT mint) FROM kills GROUP BY stage"):
            kill_events_by_stage[st] = n
            unique_kills_by_stage[st] = u

        funnel = {
            "last_completed_cycle": {
                "cycle": last_completed_cycle or None,
                "discovered": last_stats.get("discovered", 0),
                "priced": last_stats.get("priced", 0),
                "scout": last_stats.get("discovered", 0),
                "passed_scout": last_stats.get("in_window", 0),
                "passed_filter": last_stats.get("filter_pass", 0),
                "passed_analyst": last_stats.get("analyst_pass", 0),
                "executor_entries": last_stats.get("entries", 0),
                "scout_kills": last_stats.get("scout_kills", 0),
                "secs": last_stats.get("secs"),
            },
            "cumulative_events": {
                "discovered": int(sum_stat("discovered")),
                "priced": int(sum_stat("priced")),
                "scout": int(sum_stat("discovered")),
                "passed_scout": int(sum_stat("in_window")),
                "passed_filter": int(sum_stat("filter_pass")),
                "passed_analyst": int(sum_stat("analyst_pass")),
                "executor_entries": int(sum_stat("entries")),
                "scout_kills": int(sum_stat("scout_kills")) or kill_events_by_stage.get("Scout", 0),
                "filter_kills": kill_events_by_stage.get("Filter", 0),
                "analyst_kills": kill_events_by_stage.get("Analyst", 0),
            },
            "unique_coins": {
                "seen": len(unique_seen),
                "scout": len(unique_seen),
                "passed_scout": len(unique_passed_scout),
                "passed_filter": len(unique_passed_filter),
                "passed_analyst": len(unique_passed_analyst),
                "executor_entries": len(unique_entries),
                "killed_at_least_once": len(unique_kills),
                "scored": len(unique_scores),
            },
            "kill_events_by_stage": kill_events_by_stage,
            "unique_kills_by_stage": unique_kills_by_stage,
        }

        # Kills by reason, split by stage. A single kill can carry several reasons.
        by_code = defaultdict(lambda: {st: 0 for st in STAGE_ORDER})
        labels = {}
        for stage, reasons in db.execute("SELECT stage, reasons FROM kills"):
            for raw in jload(reasons, []) or []:
                code, label = bucket(raw)
                labels[code] = label
                if stage in by_code[code]:
                    by_code[code][stage] += 1
                else:
                    by_code[code][stage] = by_code[code].get(stage, 0) + 1

        kills_by_reason = []
        for code, counts in by_code.items():
            total = sum(counts.get(st, 0) for st in STAGE_ORDER)
            kills_by_reason.append({
                "code": code,
                "label_it": labels.get(code, code),
                "total": total,
                "by_stage": {st: counts.get(st, 0) for st in STAGE_ORDER},
            })
        kills_by_reason.sort(key=lambda x: x["total"], reverse=True)

        recent_filter = []
        for r in db.execute(
            "SELECT ts_iso, cycle, symbol, mint, reasons, metrics FROM kills WHERE stage='Filter' ORDER BY id DESC LIMIT 40"
        ):
            reasons = jload(r["reasons"], []) or []
            metrics = jload(r["metrics"], {}) or {}
            recent_filter.append({
                "ts_iso": r["ts_iso"],
                "time_it": format_it(parse_iso(r["ts_iso"])),
                "time_short": format_it_short(parse_iso(r["ts_iso"])),
                "cycle": r["cycle"],
                "symbol": r["symbol"],
                "mint": r["mint"],
                "mint_short": short_mint(r["mint"]),
                "reasons": reasons,
                "reasons_text": "; ".join(reasons),
                "onchain_checks": metrics.get("onchain_checks"),
            })

        latest_per_mint = []
        for r in db.execute(
            """
            SELECT k.ts_iso, k.cycle, k.symbol, k.mint, k.reasons, k.metrics
            FROM kills k
            JOIN (
                SELECT mint, MAX(id) AS mid FROM kills WHERE stage='Filter' GROUP BY mint
            ) x ON k.id = x.mid
            ORDER BY k.id DESC
            """
        ):
            reasons = jload(r["reasons"], []) or []
            latest_per_mint.append({
                "ts_iso": r["ts_iso"],
                "time_it": format_it(parse_iso(r["ts_iso"])),
                "time_short": format_it_short(parse_iso(r["ts_iso"])),
                "cycle": r["cycle"],
                "symbol": r["symbol"],
                "mint": r["mint"],
                "mint_short": short_mint(r["mint"]),
                "reasons": reasons,
                "reasons_text": "; ".join(reasons),
            })

        scores = []
        for r in db.execute(
            """
            SELECT symbol, mint, score, passed, components, ts_iso, cycle
            FROM scores s
            WHERE id = (SELECT id FROM scores s2 WHERE s2.mint = s.mint ORDER BY score DESC, id DESC LIMIT 1)
            ORDER BY score DESC, id DESC
            """
        ):
            # The query above can return one row per score id that equals the mint's best;
            # fall back to a Python collapse if SQLite returns extras.
            comp = jload(r["components"], {}) or {}
            parts = {k: comp.get(k) for k in COMPONENT_KEYS}
            scores.append({
                "symbol": r["symbol"],
                "mint": r["mint"],
                "mint_short": short_mint(r["mint"]),
                "score": r["score"],
                "passed": bool(r["passed"]) or (r["score"] is not None and r["score"] >= pass_score),
                "pass_fail_it": "PASS" if (r["score"] is not None and r["score"] >= pass_score) else "FAIL",
                "components": parts,
                "weights_effective": comp.get("_weights_effective"),
                "ts_iso": r["ts_iso"],
                "time_it": format_it(parse_iso(r["ts_iso"])),
                "cycle": r["cycle"],
            })
        # de-dupe by mint keeping first (highest score)
        seen_m = set()
        deduped = []
        for s in scores:
            if s["mint"] in seen_m:
                continue
            seen_m.add(s["mint"])
            deduped.append(s)
        scores = deduped

        first_it = cycles[0]["time_it"] if cycles else None
        status_it = "In esecuzione"
        if not running:
            status_it = "Fermo"
        elif cycle_in_progress:
            status_it = f"In esecuzione (ciclo {in_progress_cycle} in corso)"

        snapshot = {
            "generated_at": generated.isoformat(timespec="seconds"),
            "generated_at_it": format_it(generated),
            "timezone": "Europe/Rome",
            "paper_only": True,
            "source": {
                "sqlite": db_path,
                "events_jsonl": os.path.join(BASE, "events.jsonl"),
            },
            "status": {
                "running": running,
                "status_it": status_it,
                "cycle_in_progress": cycle_in_progress,
                "in_progress_cycle": in_progress_cycle,
                "in_progress_started_it": in_progress_started_it,
                "cycles_completed": completed_cycles,
                "last_cycle": last_completed_cycle,
                "last_cycle_iso": last_completed_iso,
                "last_cycle_it": last_completed_it,
                "first_cycle_it": first_it,
                "desk": desk,
                "supervisor": supervisor,
            },
            "account": {
                "start_equity": start_equity,
                "cash": cash,
                "open_mtm": mtm,
                "equity": equity,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized,
                "equity_change": equity - start_equity,
                "equity_change_pct": ((equity - start_equity) / start_equity * 100) if start_equity else 0.0,
                "closed_trades": closed_count,
                "winners": winners,
                "open_positions": len(open_positions),
            },
            "funnel": funnel,
            "cycles": [
                {
                    "cycle": c["cycle"],
                    "time_it": c["time_it"],
                    "time_short": c["time_short"],
                    "ts_iso": c["ts_iso"],
                    "discovered": c["stats"].get("discovered", 0),
                    "priced": c["stats"].get("priced", 0),
                    "in_window": c["stats"].get("in_window", 0),
                    "scout_kills": c["stats"].get("scout_kills", 0),
                    "filter_pass": c["stats"].get("filter_pass", 0),
                    "analyst_pass": c["stats"].get("analyst_pass", 0),
                    "entries": c["stats"].get("entries", 0),
                    "equity": c["stats"].get("equity"),
                    "secs": c["stats"].get("secs"),
                }
                for c in cycles
            ],
            "kills_by_reason": kills_by_reason,
            "filter_kills_recent": recent_filter,
            "filter_kills_latest_per_mint": latest_per_mint,
            "scores": scores,
            "pass_score": pass_score,
            "open_positions": open_positions,
            "closed_trades": closed_trades,
            "rules": RULES,
            "cfg": cfg,
        }
        return snapshot
    finally:
        db.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Dump MemeDesk dashboard data as JSON")
    p.add_argument("-o", "--output", help="Write JSON to this file instead of stdout")
    p.add_argument("--pretty", action="store_true", help="Indent JSON (default for TTY or -o)")
    p.add_argument("--db", default=DB_PATH, help="Path to desk.sqlite")
    args = p.parse_args(argv)

    snap = build_snapshot(args.db)
    pretty = args.pretty or bool(args.output) or (not args.output and sys.stdout.isatty())
    text = json.dumps(snap, ensure_ascii=False, indent=2 if pretty else None, default=str)
    if not pretty:
        text = json.dumps(snap, ensure_ascii=False, separators=(",", ":"), default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\n")
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
