#!/usr/bin/env python3
"""VM-side watchdog for the PAPER MemeDesk. Runs every 5 min via systemd timer (as root).

1) v7: restart memedesk.service when cycles FAIL, not only when they stop: >= 3 consecutive cycle errors
   (cycle_health.json, written by the desk) or no SUCCESSFUL cycle for > 10 min (also falls back to the
   last 'cycle N' line in desk.out). Telegram alert at most 1 per hour, one recovery message when cycles
   succeed again. Alerts go through notify.Telegram (on-disk retry queue).
   `--simulate` runs the same decision on a fake failing state: no restart, messages labelled TEST, real state untouched.

2) Telegram health: call getMe, flush the pending queue, and track failures. If Telegram has
   been down for > 30 min, log loudly to telegram.log (and syslog). On recovery, send one
   message saying alerts were delayed.

The bot token is read from env and never printed."""
import json, os, re, subprocess, sys, time
from datetime import datetime
APP = "/opt/memedesk"
OUT = os.path.join(APP, "desk.out")
ST = os.path.join(APP, "watchdog_state.json")
TGLOG = os.path.join(APP, "telegram.log")
HEALTH = os.path.join(APP, "cycle_health.json")
STALE_S, ALERT_GAP_S, TG_FAIL_S, MAX_ERRS = 600, 3600, 1800, 3
sys.path.insert(0, APP)

def last_cycle_ts():
    try:
        with open(OUT, "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 65536)); tail = f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return None
    ts = None
    for m in re.finditer(r"^(\S+) cycle \d+", tail, re.M):
        try: ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError: pass
    return ts

def _loud(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S %Z") + " WATCHDOG " + msg
    print(line)
    try:
        with open(TGLOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        subprocess.run(["logger", "-t", "memedesk-watchdog", msg], check=False)
    except Exception:
        pass

def _telegram():
    import notify
    return notify.Telegram(log=lambda *a: print(*a))

def check_telegram(st, now):
    try:
        tg = _telegram()
    except Exception as e:
        _loud(f"telegram client init failed {type(e).__name__}")
        return
    ok, username = False, None
    try:
        ok, username = tg.get_me()
    except Exception as e:
        _loud(f"getMe exception {type(e).__name__}")
        ok = False
    qlen = 0
    try:
        qlen = tg.queue_len()
        flushed = tg.flush_queue()
        if flushed:
            print(f"flushed {flushed} queued telegram alerts")
    except Exception as e:
        _loud(f"queue flush failed {type(e).__name__}")
    st["tg_last_check"] = now
    st["tg_last_ok"] = bool(ok)
    st["tg_queue_len"] = qlen
    if ok:
        down_since = st.get("tg_down_since")
        if down_since:
            mins = int((now - down_since) // 60)
            print(f"telegram recovered after {mins} min, username={username}")
            if mins >= 30 or st.get("tg_alerted") or qlen:
                try:
                    tg.send(
                        f"✅ Telegram di nuovo ok dopo {mins} min di interruzione. "
                        f"Gli avvisi in coda vengono recapitati ora (PAPER, VM Google)."
                    )
                except Exception as e:
                    _loud(f"recovery notice failed {type(e).__name__}")
            st.pop("tg_down_since", None)
            st["tg_alerted"] = False
        elif qlen > 20:
            _loud(f"telegram getMe ok but queue still long ({qlen})")
    else:
        st.setdefault("tg_down_since", now)
        down_for = now - st["tg_down_since"]
        _loud(f"telegram getMe FAILED, down_for={int(down_for)}s queue={qlen}")
        if down_for >= TG_FAIL_S and not st.get("tg_alerted"):
            _loud(f"LOUD: Telegram failing for more than 30 min (queue={qlen}). Will retry each watchdog run.")
            st["tg_alerted"] = True

def read_health():
    try: return json.load(open(HEALTH))
    except Exception: return {}

def decide(now, health, ts_line, up_s):
    """-> (problem or None, last_ok_ts). Pure function so --simulate exercises the real rule."""
    last_ok = max([t for t in (health.get("last_success_ts"), ts_line) if t] or [0]) or None
    age = (now - last_ok) if last_ok else 1e9
    errs = int(health.get("consecutive_errors") or 0)
    if errs >= MAX_ERRS:
        return f"{errs} cicli consecutivi falliti (ultimo errore: {(health.get('last_error') or 'n/d')[:120]})", last_ok
    if age > STALE_S and up_s >= STALE_S:
        return f"nessun ciclo riuscito da {int(age // 60)} min", last_ok
    return None, last_ok

def act(st, now, problem, simulate=False, last_ok=None):
    tag = "🧪 TEST watchdog — " if simulate else ""
    if problem:
        print(("SIMULATE would restart: " if simulate else "PROBLEM -> restarting memedesk.service: ") + problem)
        if not simulate:
            subprocess.run(["systemctl", "restart", "memedesk.service"])
            st["restarts"] = st.get("restarts", 0) + 1
            st["last_restart"] = now
            st["cycle_down"] = True
        if simulate or now - st.get("last_alert", 0) >= ALERT_GAP_S:
            try:
                ok = _telegram().send(f"{tag}⚠️ MemeDesk VM: {problem}. Servizio riavviato automaticamente (PAPER).")
                print("alert sent" if ok else "alert queued or not sent")
                if not simulate: st["last_alert"] = now   # even if queued: do not stampede
            except Exception as e:
                print("alert failed", type(e).__name__)
    elif simulate or (st.get("cycle_down") and (last_ok or 0) > st.get("last_restart", 0)):   # a real successful cycle after the restart
        try:
            ok = _telegram().send(f"{tag}✅ MemeDesk VM: i cicli sono di nuovo regolari (PAPER).")
            print("recovery sent" if ok else "recovery queued or not sent")
        except Exception as e:
            print("recovery failed", type(e).__name__)
        if not simulate: st["cycle_down"] = False

def main():
    now = time.time()
    if "--simulate" in sys.argv:   # safe test: fake failing state, then fake recovery; no restart, real state untouched
        fake = dict(consecutive_errors=3, last_error="KeyError('simulated')", last_success_ts=now - 900)
        problem, _ = decide(now, fake, None, 1e9)
        act({}, now, problem, simulate=True)
        problem2, _ = decide(now, dict(consecutive_errors=0, last_success_ts=now - 60), None, 1e9)
        act({}, now, problem2, simulate=True)
        return
    st = json.load(open(ST)) if os.path.exists(ST) else {}
    # grace period right after a (re)start of the service
    try:
        a_ = subprocess.run(["systemctl", "show", "memedesk.service", "-p", "ActiveEnterTimestampMonotonic"],
                            capture_output=True, text=True).stdout.strip().split("=")[-1]
        up_s = time.monotonic() - int(a_) / 1e6 if a_ and a_ != "0" else 0
    except Exception:
        up_s = 1e9
    health = read_health()
    problem, last_ok = decide(now, health, last_cycle_ts(), up_s)
    if not problem:
        print(f"ok: last successful cycle {int(now - last_ok) if last_ok else -1}s ago, "
              f"consecutive errors {int(health.get('consecutive_errors') or 0)}, service up {int(up_s)}s")
    act(st, now, problem, last_ok=last_ok)
    try:
        check_telegram(st, now)
    except Exception as e:
        _loud(f"telegram health error {type(e).__name__}")
    tmp = ST + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, ST)

if __name__ == "__main__":
    main()
