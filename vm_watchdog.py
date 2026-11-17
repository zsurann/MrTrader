#!/usr/bin/env python3
"""VM-side watchdog for the PAPER MemeDesk. Runs every 5 min via systemd timer (as root).

1) If desk.out shows no new 'cycle' line for > 10 min, restart memedesk.service and enqueue
   ONE Telegram alert (at most 1 alert per hour). Alerts go through notify.Telegram so a failed
   send is retried later from the on-disk queue instead of being lost.

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
STALE_S, ALERT_GAP_S, TG_FAIL_S = 600, 3600, 1800
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

def main():
    now = time.time()
    st = json.load(open(ST)) if os.path.exists(ST) else {}
    ts = last_cycle_ts()
    # grace period right after a (re)start of the service
    try:
        act = subprocess.run(["systemctl", "show", "memedesk.service", "-p", "ActiveEnterTimestampMonotonic"],
                             capture_output=True, text=True).stdout.strip().split("=")[-1]
        up_s = time.monotonic() - int(act) / 1e6 if act and act != "0" else 0
    except Exception:
        up_s = 1e9
    age = (now - ts) if ts else 1e9
    if age <= STALE_S or up_s < STALE_S:
        print(f"ok: last cycle {int(age)}s ago, service up {int(up_s)}s")
    else:
        print(f"STALE: last cycle {int(age)}s ago -> restarting memedesk.service")
        subprocess.run(["systemctl", "restart", "memedesk.service"])
        st["restarts"] = st.get("restarts", 0) + 1
        st["last_restart"] = now
        if now - st.get("last_alert", 0) >= ALERT_GAP_S:
            try:
                tg = _telegram()
                mins = int(age // 60) if ts else -1
                ok = tg.send(f"⚠️ MemeDesk VM: nessun ciclo da {mins} min, servizio riavviato automaticamente (PAPER).")
                print("alert sent" if ok else "alert queued or not sent")
                st["last_alert"] = now  # even if queued: do not stampede
            except Exception as e:
                print("alert failed", type(e).__name__)
    try:
        check_telegram(st, now)
    except Exception as e:
        _loud(f"telegram health error {type(e).__name__}")
    tmp = ST + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, ST)

if __name__ == "__main__":
    main()
