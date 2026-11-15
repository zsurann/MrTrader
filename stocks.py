"""US stock paper desk: the day-trader-skill pipeline on a loop, paper money only.

scan (opening-range breakouts) -> news -> exit plan -> time machine -> budget -> risk -> paper fill -> exits
Every step is the skill's own tool (.claude/skills/trader-*), run with --json; files live in stocks/trader/.
No broker is ever called: fills are recorded with trader-broker's `ticket.py fill` at the delayed Yahoo quote.

Run:  python3 stocks.py            (loops forever; does nothing while the NYSE is closed)
      python3 stocks.py --once     (one cycle)
"""
import datetime, json, os, subprocess, sys, threading, time

DIR = os.path.dirname(os.path.abspath(__file__))
SK = os.path.join(DIR, ".claude", "skills")
WORK = os.path.join(DIR, "stocks")  # tools read/write trader/* relative to this
T = os.path.join(WORK, "trader")
CFG = dict(budget=10_000, max_loss=50, max_trades=3, target="2R", tm_min_avg_r=0.0,
           scan_every_min=10, fresh_hit_min=15, first_scan="09:50", last_entry="15:30", flat_by="15:55", tick_s=60)
TOOLS = {"scan": "trader-scanner/scan.py", "news": "trader-news/news.py", "exit": "trader-exit/exit.py",
         "tm": "trader-timemachine/timemachine.py", "size": "trader-budget/size.py", "risk": "trader-risk/check.py",
         "ticket": "trader-broker/ticket.py", "alerts": "trader-alerts/alerts.py"}
LOCK = threading.Lock()  # exits and entries share trader/*.json and the desk state
EXTRA = {}  # "tool" or "tool subcommand" -> extra args (tests use it for --offline fixtures)


def log(kind, **data):
    os.makedirs(os.path.join(WORK, "logs"), exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, **data}
    with open(os.path.join(WORK, "logs", "stocks.jsonl"), "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def tool(name, *args, now=None, js=True):
    """Run one skill tool. Returns its parsed --json output (or stdout text), None on failure (logged)."""
    extra = EXTRA.get(f"{name} {args[0]}" if args else name, EXTRA.get(name, []))
    cmd = [sys.executable, os.path.join(SK, TOOLS[name]), *args, *extra]
    cmd += (["--json"] if js else []) + (["--now", now] if now else [])
    try:
        r = subprocess.run(cmd, cwd=WORK, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        log("tool_error", tool=name, args=list(args), error="timeout")
        return None
    if r.returncode != 0 and not r.stdout.strip().startswith("{"):
        log("tool_error", tool=name, args=list(args), error=(r.stderr or r.stdout).strip()[-300:])
        return None
    try:
        return json.loads(r.stdout) if js else r.stdout
    except ValueError:
        log("tool_error", tool=name, args=list(args), error="not json: " + r.stdout[-200:])
        return None


def notify(text):
    """Same phone topic as MrTrader (ntfy), if the crypto desk has one."""
    try:
        import desk
        desk.notify(desk.load_state(), "[AZIONI] " + text)
    except Exception as e:
        log("notify_error", error=str(e))


def load():
    os.makedirs(T, exist_ok=True)
    cfg = os.path.join(T, "config.json")
    if not os.path.exists(cfg):  # the skill's example config with the user's choices
        c = json.load(open(os.path.join(SK, "trader-budget", "config.example.json")))
        c.update(budget=CFG["budget"], max_loss_per_trade_usd=CFG["max_loss"], max_trades_per_day=CFG["max_trades"],
                 max_daily_loss_usd=3 * CFG["max_loss"], flat_by=CFG["flat_by"], paper=True)
        c.pop("sec_user_agent", None)
        json.dump(c, open(cfg, "w"), indent=1)
    p = os.path.join(T, "desk.json")
    return json.load(open(p)) if os.path.exists(p) else {"date": None, "trades": 0, "done": [], "pos": {}, "last_scan": None}


def save(st):
    p = os.path.join(T, "desk.json")
    json.dump(st, open(p + ".tmp", "w"), indent=1)
    os.replace(p + ".tmp", p)


def hhmm(s):
    """'10:15 AM' / '10:15' -> minutes after midnight."""
    t = datetime.datetime.strptime(s.strip(), "%I:%M %p" if s.strip()[-1] == "M" else "%H:%M")
    return t.hour * 60 + t.minute


def sell(st, sym, price, why, now):
    p = st["pos"].pop(sym)
    tool("ticket", "fill", "--symbol", sym, "--side", "sell", "--qty", str(p["qty"]), "--price", f"{price:.2f}",
         now=now, js=False)
    pnl = round((price - p["entry"]) * p["qty"], 2)
    log("trade", side="sell", symbol=sym, qty=p["qty"], price=price, entry=p["entry"], pnl_usd=pnl, reason=why)
    notify(f"VENDE {sym} {p['qty']} az. a ${price:.2f} ({pnl:+.2f}$) {why}")


def exits(st, now, t):
    with LOCK:
        _exits(st, now, t)


def _exits(st, now, t):
    if not st["pos"]:
        return
    q = (tool("alerts", "quote", *st["pos"], "--write", now=now) or {}).get("quotes") or {}
    for sym, p in list(st["pos"].items()):
        px = (q.get(sym) or {}).get("price")
        if t >= hhmm(CFG["flat_by"]):
            sell(st, sym, px or p["entry"], "flat_by", now)  # day trade: never held overnight
        elif px and px <= p["stop"]:
            sell(st, sym, px, "stop", now)
        elif px and px >= p["target"]:
            sell(st, sym, px, "target", now)


def entries(st, now, t):
    scan = tool("scan", "orb", "--quiet", "--no-write", now=now) or {}  # the slow part: runs without the lock
    hits = [h for h in scan.get("hits") or [] if h["symbol"] not in st["done"] and t - hhmm(h["at"]) <= CFG["fresh_hit_min"]]
    log("scan", hits=len(scan.get("hits") or []), fresh=[h["symbol"] for h in hits], seconds=scan.get("seconds"))
    for h in sorted(hits, key=lambda h: h["pct_over"]):  # closest to the range: least already run
        with LOCK:  # one candidate at a time, so an exit check never waits for more than one
            if st["trades"] >= CFG["max_trades"]:
                return
            _take(st, now, h)
            save(st)


def _take(st, now, h):
    sym = h["symbol"]
    st["done"].append(sym)  # one look per symbol per day, taken or not
    news = tool("news", "--tickers", sym, "--hours", "12", "--no-sec", now=now) or {}
    plan = tool("exit", "plan", "--symbol", sym, "--entry", str(h["price"]), "--or-high", str(h["or_high"]),
                "--or-low", str(h["or_low"]), "--stop", "or-mid", "--target", CFG["target"], "--time", h["at"],
                "--write", now=now)
    if not plan:
        return
    tm = tool("tm", "run", "--until", CFG["flat_by"], now=now)
    mc = (tm or {}).get("mc") or {}
    if mc.get("avg_R") is None or mc["avg_R"] <= CFG["tm_min_avg_r"]:
        log("skip", symbol=sym, why="time machine", avg_R=mc.get("avg_R"), verdict=(tm or {}).get("verdict"))
        return
    quote = ((tool("alerts", "quote", sym, "--write", now=now) or {}).get("quotes") or {}).get(sym) or {}
    size = tool("size", "--write", now=now)
    if not size or not (size.get("order") or size.get("orders")):
        log("skip", symbol=sym, why="budget refused", detail=(size or {}).get("refused") or (size or {}).get("error"))
        return
    risk = tool("risk", now=now) or {}
    res = (risk.get("results") or [{}])[0]
    if res.get("verdict") != "PASS":
        log("skip", symbol=sym, why="risk", reasons=res.get("reasons"))
        return
    qty, px = int(float(res["amount"].split()[0])), quote.get("price") or plan["entry"]
    tool("ticket", "fill", "--symbol", sym, "--side", "buy", "--qty", str(qty), "--price", f"{px:.2f}", now=now, js=False)
    st["pos"][sym] = {"qty": qty, "entry": px, "stop": plan["stop"], "target": plan["target"]}
    st["trades"] += 1
    log("trade", side="buy", symbol=sym, qty=qty, price=px, stop=plan["stop"], target=plan["target"],
        avg_R=mc["avg_R"], p_target=mc.get("p_target"), p_stop=mc.get("p_stop"), news=len(news.get("items") or []))
    notify(f"COMPRA {sym} {qty} az. a ${px:.2f} | stop {plan['stop']} target {plan['target']} | "
           f"time machine: target {mc.get('p_target', 0) * 100:.0f}% / stop {mc.get('p_stop', 0) * 100:.0f}%")


_scan = [None]  # the running background scan thread, if any


def cycle(st, now=None, background=False):
    clk = tool("alerts", "clock", now=now) or {}
    if not clk.get("open"):
        return
    day, t = clk["date"], hhmm(clk["now_new_york"][-5:])
    if st["date"] != day:
        st.update(date=day, trades=0, done=[], last_scan=None)
        tool("scan", "universe", js=False)  # ~5,900 US stocks, refreshed once a day
    exits(st, now, t)
    last = st.get("last_scan")
    if hhmm(CFG["first_scan"]) <= t <= hhmm(CFG["last_entry"]) and st["trades"] < CFG["max_trades"] \
            and (last is None or t - last >= CFG["scan_every_min"]):
        st["last_scan"] = t
        if not background:
            entries(st, now, t)
        elif not (_scan[0] and _scan[0].is_alive()):  # exits keep running every minute while it scans
            _scan[0] = threading.Thread(target=entries, args=(st, now, t), daemon=True)
            _scan[0].start()
    with LOCK:
        save(st)


def main(once=False):
    st = load()
    log("start", mode="paper", cfg=CFG)
    while True:
        cycle(st, background=not once)
        if once:
            return st
        time.sleep(CFG["tick_s"])


if __name__ == "__main__":
    main(once="--once" in sys.argv)
