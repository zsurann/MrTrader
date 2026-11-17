#!/usr/bin/env python3
"""
MemeDesk Telegram alerts (@sasowlbot) — PAPER desk. Runs as a thread inside syncer.py (separate from trading).
Reads desk.sqlite read-only and sends only rare events, in Italian:
  coin passes Filter / Analyst (deduped per coin, 6h), ENTRY, EXIT (with PnL), desk start/restart, desk process down,
  no cycle for > 5 min, daily paper loss beyond -3%, and a short summary at most every 3h.
Rate-limited (>= 2s between messages, <= 20/hour). The bot token comes from the env and is never logged or printed.

Delivery is retried: timeouts, exponential backoff, and a small on-disk queue so a failed send
(network/429) is not lost. Failures go to telegram.log (never the token, and never the API URL
which contains the token). The VM watchdog uses the same client and queue.

CLI (secrets from the environment only, never printed):
  python3 notify.py --getme     # check getMe; prints HTTP/ok/username only
  python3 notify.py --test MSG  # getMe + send MSG (default: a short VM test line)
"""
import fcntl, json, os, sqlite3, sys, threading, time, urllib.error, urllib.parse, urllib.request
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("MEMEDESK_DATA", BASE)
DB = os.path.join(DATA, "desk.sqlite")
STATE = os.path.join(DATA, "notify_state.json")
QUEUE = os.path.join(DATA, "telegram_queue.jsonl")
STATUS = os.path.join(DATA, "telegram_status.json")
LOGF = os.path.join(DATA, "telegram.log")
ROME = ZoneInfo("Europe/Rome")
CFG = dict(poll_s=15, min_gap_s=2.0, max_per_hour=20, pass_dedupe_h=6, no_cycle_s=300, down_s=60,
           daily_loss_pct=-3.0, summary_every_h=3, start_equity=10000.0,
           http_tries=4, http_timeout=15, queue_max=200)
TEST_MSG = "MemeDesk su VM Google: test Telegram OK"

def chat_id():
    if os.environ.get("TELEGRAM_CHAT_ID"): return os.environ["TELEGRAM_CHAT_ID"].strip()
    try:
        for line in open(os.path.join(BASE, ".env.telegram")):
            if line.startswith("TELEGRAM_CHAT_ID="): return line.split("=", 1)[1].strip()
    except Exception: return None

def hm(ts): return datetime.fromtimestamp(ts, ROME).strftime("%H:%M")
def usd(x): return ("−" if x < 0 else "") + "$" + f"{abs(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
def pct(x): return ("+" if x >= 0 else "−") + f"{abs(x):.1f}".replace(".", ",") + "%"

def _own(path):
    """If we created a file as root, hand it to the owner of the app dir (memedesk)."""
    try:
        st = os.stat(DATA)
        os.chown(path, st.st_uid, st.st_gid)
    except Exception:
        pass

def tlog(msg, also=None):
    # Never write tokens, URLs, or env values. `msg` must already be sanitized.
    line = time.strftime("%Y-%m-%d %H:%M:%S %Z") + " " + str(msg)
    try:
        existed = os.path.exists(LOGF)
        with open(LOGF, "a") as f:
            f.write(line + "\n")
        if not existed: _own(LOGF)
    except Exception:
        pass
    if also:
        try: also(line)
        except Exception: pass

@contextmanager
def _flock(path):
    fd = open(path, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.path.getsize(path) == 0: _own(path)
        yield fd
    finally:
        try: fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception: pass
        fd.close()


class Telegram:
    """Shared send/getMe/queue client. Safe to use from the syncer thread and from the VM watchdog."""
    def __init__(self, log=None):
        self.token, self.chat = os.environ.get("TELEGRAM_BOT_TOKEN"), chat_id()
        self.enabled = bool(self.token and self.chat)
        self.log = log
        self.sent, self.last_send, self.dropped = [], 0.0, 0
        self.lock = threading.Lock()

    def _out(self, *a):
        tlog(" ".join(str(x) for x in a), also=(lambda line: self.log(line) if self.log else None))

    def _write_status(self, **kv):
        st = {}
        try:
            if os.path.exists(STATUS): st = json.load(open(STATUS))
        except Exception: st = {}
        st.update(kv)
        st["queue_len"] = self.queue_len()
        st["ts"] = time.time()
        try:
            tmp = STATUS + ".tmp"
            json.dump(st, open(tmp, "w"))
            os.replace(tmp, STATUS)
            _own(STATUS)
        except Exception:
            pass

    def _api(self, method, data=None, tries=None, timeout=None):
        """POST/GET a Telegram Bot API method. Returns the parsed body or None.
        The URL contains the token and is never logged; only method name + status/type."""
        if not self.enabled: return None
        tries = CFG["http_tries"] if tries is None else tries
        timeout = CFG["http_timeout"] if timeout is None else timeout
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        delay = 1.0
        last = ""
        for i in range(tries):
            try:
                req = urllib.request.Request(url, data=data)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                    code = r.status
                    payload = json.loads(raw.decode()) if raw else {}
                if payload.get("ok"):
                    return payload
                last = f"ok=false {str(payload.get('description') or '')[:80]}"
                if code == 429 or (isinstance(payload.get("error_code"), int) and payload.get("error_code") == 429):
                    ra = (payload.get("parameters") or {}).get("retry_after")
                    time.sleep(float(ra) if ra else delay)
                else:
                    # 400-level (bad chat, etc.) is not worth retrying
                    if isinstance(payload.get("error_code"), int) and 400 <= payload["error_code"] < 500 and payload["error_code"] != 429:
                        break
                    time.sleep(delay)
            except urllib.error.HTTPError as e:
                last = f"HTTP {getattr(e, 'code', '')}"
                if e.code == 429:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    time.sleep(float(ra) if ra and str(ra).replace(".", "", 1).isdigit() else delay)
                elif e.code in (500, 502, 503, 504):
                    time.sleep(delay)
                else:
                    break
            except Exception as e:
                last = type(e).__name__
                time.sleep(delay)
            delay = min(delay * 2, 30)
        self._out(f"{method} failed after {tries} tries: {last}")
        self._write_status(ok=False, last_err=last, last_fail=time.time())
        return None

    def get_me(self):
        d = self._api("getMe", data=None, tries=3, timeout=15)
        ok = bool(d and d.get("ok"))
        username = ((d or {}).get("result") or {}).get("username") if ok else None
        self._write_status(ok=ok, last_getme=time.time(), last_err=(None if ok else "getMe failed"), username=username)
        if ok: self._out(f"getMe ok username={username}")
        return ok, username

    def queue_len(self):
        try:
            with open(QUEUE) as f:
                return sum(1 for line in f if line.strip())
        except FileNotFoundError:
            return 0
        except Exception:
            return -1

    def enqueue(self, text, reason="send_failed"):
        rec = dict(ts=time.time(), text=text[:3900], reason=reason)
        with _flock(QUEUE + ".lock"):
            rows = []
            if os.path.exists(QUEUE):
                with open(QUEUE) as f:
                    rows = [line for line in f if line.strip()]
            rows.append(json.dumps(rec, ensure_ascii=False) + "\n")
            overflow = len(rows) - CFG["queue_max"]
            if overflow > 0:
                rows = rows[overflow:]
                self._out(f"queue overflow, dropped {overflow} oldest")
            with open(QUEUE, "w") as f:
                f.writelines(rows)
            _own(QUEUE)
        self._write_status(ok=False, last_err=reason, last_enqueue=time.time())
        self._out(f"queued alert ({reason}), queue_len={self.queue_len()}")

    def _send_once(self, text):
        if not self.enabled: return False
        t = time.time()
        with self.lock:
            self.sent = [x for x in self.sent if t - x < 3600]
            if len(self.sent) >= CFG["max_per_hour"]:
                self.dropped += 1
                return "rate"
            if self.dropped:
                text = text + f"\n(+{self.dropped} avvisi saltati per limite di frequenza)"
                self.dropped = 0
            wait = self.last_send + CFG["min_gap_s"] - time.time()
        if wait > 0: time.sleep(wait)
        body = urllib.parse.urlencode(dict(chat_id=self.chat, text=text[:3900], disable_web_page_preview="true")).encode()
        d = self._api("sendMessage", data=body)
        ok = bool(d and d.get("ok"))
        with self.lock:
            self.last_send = time.time()
            if ok: self.sent.append(self.last_send)
        if ok:
            mid = (d.get("result") or {}).get("message_id")
            self._write_status(ok=True, last_ok=time.time(), last_err=None, last_message_id=mid)
        return ok

    def send(self, text, queue_on_fail=True):
        if not self.enabled:
            self._out("send skipped (token or chat id missing)")
            return False
        r = self._send_once(text)
        if r is True: return True
        if queue_on_fail:
            self.enqueue(text, reason=("rate_limit" if r == "rate" else "send_failed"))
        return False

    def flush_queue(self):
        if not self.enabled: return 0
        sent_n = 0
        with _flock(QUEUE + ".lock"):
            if not os.path.exists(QUEUE): return 0
            recs = []
            with open(QUEUE) as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try: recs.append(json.loads(line))
                    except Exception: continue
            rest = []
            for rec in recs:
                text = rec.get("text") or ""
                if not text:
                    continue
                r = self._send_once(text)
                if r is True:
                    sent_n += 1
                    continue
                rest.append(rec)
                if r == "rate":
                    rest.extend(recs[recs.index(rec) + 1:])
                    break
                # leave the rest queued too after a hard failure, try later
                rest.extend(recs[recs.index(rec) + 1:])
                break
            with open(QUEUE, "w") as f:
                for rec in rest:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            _own(QUEUE)
        if sent_n:
            self._out(f"flushed {sent_n} queued alerts, remaining={len(rest) if recs else 0}")
        self._write_status(last_flush=time.time())
        return sent_n


class Notifier:
    def __init__(self, log=print):
        self.tg = Telegram(log=log)
        self.enabled = self.tg.enabled
        self.log = log
        self.st = json.load(open(STATE)) if os.path.exists(STATE) else {}
        self.db = None
        try:
            self.db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10, check_same_thread=False)
            self.db.execute("PRAGMA busy_timeout=5000")
        except Exception as e:
            tlog(f"notify db open failed {type(e).__name__}")

    def send(self, text, queue_on_fail=True):
        return self.tg.send(text, queue_on_fail=queue_on_fail)

    def save(self):
        tmp = STATE + ".tmp"; json.dump(self.st, open(tmp, "w")); os.replace(tmp, STATE)

    def q(self, sql, a=()):
        if not self.db: return []
        return self.db.execute(sql, a).fetchall()

    # ---------------- rare desk events
    def events(self):
        mx = (self.q("SELECT MAX(id) FROM events") or [[0]])[0][0] or 0
        wm = self.st.get("wm")
        if wm is None:   # first run: don't replay history (only the last 2 minutes, e.g. the start that launched us)
            wm = self.st["wm"] = (self.q("SELECT COALESCE(MAX(id),0) FROM events WHERE ts<?", (time.time() - 120,)) or [[0]])[0][0]
        rows = self.q("""SELECT id,ts,kind,mint,symbol,data FROM events WHERE id>? AND id<=?
                         AND kind IN ('FILTER_PASS','SCORE_PASS','ENTRY','EXIT','DESK_START') ORDER BY id""", (wm, mx))
        dd = self.st.setdefault("dedupe", {})
        for i, ts, kind, mint, sym, data in rows:
            d = json.loads(data or "{}")
            if kind in ("FILTER_PASS", "SCORE_PASS"):
                k = f"{kind}:{mint}"
                if time.time() - dd.get(k, 0) < CFG["pass_dedupe_h"] * 3600: continue
                dd[k] = time.time()
                if kind == "FILTER_PASS":
                    ch = d.get("checks") or {}
                    self.send(f"🔎 {sym} ha passato il Filter ({hm(ts)})\nliq {usd(ch.get('liquidity_usd') or 0)} · vol 1h {usd(ch.get('vol_h1') or 0)}"
                              f" · top10 {ch.get('top10_pct_ex_pools')}%\nhttps://dexscreener.com/solana/{mint}")
                else:
                    self.send(f"🧠 {sym} score Analyst {d.get('score')} ≥ 70 ({hm(ts)}) · smart wallet: "
                              f"{(d.get('components') or {}).get('smart_wallets') if (d.get('components') or {}).get('smart_wallets') is not None else 'n/d'}")
            elif kind == "ENTRY":
                o = d.get("order") or {}
                self.send(f"🟦 INGRESSO paper {sym} ({hm(ts)})\nsize {usd(o.get('size_usd') or 0)} · prezzo {o.get('fill_price'):.6g}"
                          f" · slippage {o.get('slippage_pct')}%\nstop −{100 * (1 - (o.get('stop_price') or 0) / (o.get('ref_price') or o.get('fill_price') or 1)):.0f}% dal prezzo di mercato a {o.get('stop_price'):.6g} · score {o.get('score')}")
            elif kind == "EXIT":
                p = d.get("pnl_usd") or 0
                self.send(f"{'🟩' if p >= 0 else '🟥'} USCITA paper {sym} ({hm(ts)}) · {d.get('reason')}\nPnL {usd(p)} ({pct(d.get('pnl_pct') or 0)})")
            elif kind == "DESK_START":
                self.send(f"🔄 MemeDesk (ri)avviato alle {hm(ts)} · modalità PAPER, nessun wallet")
        self.st["wm"] = mx
        for k in [k for k, v in dd.items() if time.time() - v > 86400]: dd.pop(k)

    # ---------------- health + money
    def equity(self):
        cash = (self.q("SELECT v FROM ledger WHERE k='cash'") or [[0]])[0][0]
        opn = self.q("SELECT qty,last_price,entry_price FROM positions WHERE status='OPEN'")
        return cash + sum(q * (lp or ep) for q, lp, ep in opn), len(opn)

    def health(self):
        t = time.time()
        try: pid = int(open(os.path.join(DATA, "desk.pid")).read().strip())
        except Exception: pid = None
        alive = bool(pid) and os.path.exists(f"/proc/{pid}")
        if not alive:
            self.st.setdefault("down_since", t)
            if t - self.st["down_since"] > CFG["down_s"] and not self.st.get("down_alerted"):
                self.send("🔴 Il processo del desk non è attivo da oltre 1 minuto (il supervisore prova a riavviarlo)."); self.st["down_alerted"] = True
        else:
            if self.st.get("down_alerted"): self.send("🟢 Desk di nuovo attivo.")
            self.st.pop("down_since", None); self.st["down_alerted"] = False
        last = (self.q("SELECT MAX(ts) FROM cycles") or [[None]])[0][0]
        if last and t - last > CFG["no_cycle_s"]:
            if not self.st.get("stall_alerted"):
                self.send(f"⚠️ Nessun ciclo completato da {int((t - last) // 60)} minuti (ultimo alle {hm(last)})."); self.st["stall_alerted"] = True
        elif last:
            if self.st.get("stall_alerted"): self.send("✅ I cicli sono ripartiti.")
            self.st["stall_alerted"] = False
        eq, nopen = self.equity()
        day = datetime.fromtimestamp(t, ROME).strftime("%Y-%m-%d")
        if self.st.get("day") != day: self.st.update(day=day, day_start_eq=eq, loss_alerted=False)
        dpct = 100 * (eq / self.st["day_start_eq"] - 1) if self.st.get("day_start_eq") else 0
        if dpct <= CFG["daily_loss_pct"] and not self.st.get("loss_alerted"):
            self.send(f"📉 Perdita paper di oggi {pct(dpct)} (oltre −3%): equity {usd(eq)}."); self.st["loss_alerted"] = True
        if t - self.st.get("last_summary", 0) >= CFG["summary_every_h"] * 3600:
            since = t - CFG["summary_every_h"] * 3600
            ncy = (self.q("SELECT COUNT(*) FROM cycles WHERE ts>?", (since,)) or [[0]])[0][0]
            cl = (self.q("SELECT COUNT(*), COALESCE(SUM(pnl_usd),0) FROM positions WHERE status='CLOSED' AND closed_ts>?", (since,)) or [[0, 0]])[0]
            try: sm = (self.q("SELECT COUNT(*) FROM smart_wallets WHERE is_smart=1") or [[0]])[0][0]
            except Exception: sm = 0
            self.send(f"📊 Riepilogo MemeDesk ({hm(t)})\nequity {usd(eq)} ({pct(100 * (eq / CFG['start_equity'] - 1))} dall'inizio, {pct(dpct)} oggi)\n"
                      f"posizioni aperte {nopen} · cicli ultime 3h {ncy} · trade chiusi {cl[0]} ({usd(cl[1])})\nsmart wallet in lista: {sm}")
            self.st["last_summary"] = t

    def loop(self):
        while True:
            try:
                self.tg.flush_queue()
                self.events(); self.health(); self.save()
            except Exception as e:
                tlog(f"notify error {type(e).__name__}: {repr(e)[:160]}")
                if self.log: self.log(time.strftime("%F %T"), "notify error", type(e).__name__, repr(e)[:160])
            time.sleep(CFG["poll_s"])

def start_thread(log=print):
    n = Notifier(log)
    if not n.enabled:
        tlog("telegram disabled (token or chat id missing)")
        log(time.strftime("%F %T"), "telegram disabled (token or chat id missing)")
        return None
    threading.Thread(target=n.loop, daemon=True, name="telegram").start()
    return n

def _load_local_env():
    # Box-only fallback; on the VM systemd already exported the vars. Never print values.
    for path in (os.path.join(BASE, ".env.telegram"), os.path.join(BASE, ".env.supabase")):
        if not os.path.exists(path): continue
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)

def main(argv):
    _load_local_env()
    tg = Telegram(log=print)
    if not tg.enabled:
        print("telegram disabled (token or chat id missing)")
        return 2
    if "--getme" in argv or "--test" in argv:
        ok, username = tg.get_me()
        print("getMe ok:", bool(ok), "username:", username or "?")
        if not ok: return 1
        if "--getme" in argv and "--test" not in argv: return 0
    if "--test" in argv:
        rest = [a for a in argv[1:] if not a.startswith("--")]
        msg = " ".join(rest) if rest else TEST_MSG
        ok = tg.send(msg, queue_on_fail=False)
        print("sendMessage ok:", bool(ok))
        return 0 if ok else 1
    print("usage: notify.py --getme | --test [message]")
    return 2

if __name__ == "__main__":
    sys.exit(main(sys.argv))
