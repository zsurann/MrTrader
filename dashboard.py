"""Local web dashboard: python3 dashboard.py, then open http://localhost:8050"""
import collections, json, os, re, signal, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desk import is_wallet, load_smart_wallets

DIR = os.path.dirname(os.path.abspath(__file__))
PID = os.path.join(DIR, "desk.pid")


def tail_jsonl(name, n):
    path = os.path.join(DIR, "logs", name)
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path).readlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass  # half-written last line while desk is writing
    return out


def reason_kind(r):
    """'1h vol $20,982 < $50,000' -> '1h vol < $50,000' so kills group by rule."""
    return re.sub(r" [+$]?[\d.,]+%?( |$)", " ", r, count=1).strip()


def desk_pid():
    try:
        pid = int(open(PID).read())
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:  # our child exited: reap zombie
                return None
        except ChildProcessError:
            pass  # started by an earlier dashboard
        # cmdline is empty for zombies and differs if the pid was reused
        return pid if "desk.py" in open(f"/proc/{pid}/cmdline").read() else None
    except (OSError, ValueError):
        return None


def summary():
    st = {"cash": 10_000, "pos": {}, "closed": []}
    try:
        st = json.load(open(os.path.join(DIR, "state.json")))
    except (OSError, ValueError):
        pass
    pos = [{"symbol": p.get("symbol"), "mint": m, "usd": round(p["usd"], 2), "entry": p["entry"], "last": p["last"],
            "stop": p["stop"], "trailing": bool(p.get("trailing")), "pnl_pct": round((p["last"] / p["entry"] - 1) * 100, 2)}
           for m, p in st["pos"].items()]
    kills = tail_jsonl("kill.jsonl", 2000)
    reasons = collections.Counter(reason_kind(r) for k in kills for r in k.get("reasons", []))
    sells = [t for t in tail_jsonl("trade.jsonl", 500) if t.get("side") == "sell"]
    return {
        "running": desk_pid() is not None,
        "cash": round(st["cash"], 2),
        "equity": round(st["cash"] + sum(p["qty"] * p["last"] for p in st["pos"].values()), 2),
        "positions": pos,
        "trades": tail_jsonl("trade.jsonl", 30)[::-1],
        "kills": sorted({k.get("mint"): {k_: k.get(k_) for k_ in ("ts", "agent", "symbol", "reasons")}
                         for k in kills}.values(), key=lambda k: k["ts"] or "", reverse=True)[:40],  # latest kill per coin
        "top_reasons": reasons.most_common(8),
        "wins": sum(t["pnl_usd"] > 0 for t in sells), "losses": sum(t["pnl_usd"] <= 0 for t in sells),
        "events": tail_jsonl("events.jsonl", 8)[::-1],
        "wallets": sorted(load_smart_wallets()),
    }


PAGE = """<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MrTrader Desk</title><style>
:root{--bg:#0f1115;--card:#181b22;--fg:#e6e6e6;--mut:#8a8f98;--g:#3ecf8e;--r:#ef5b5b}
body{margin:0;font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg);padding:16px}
h1{font-size:20px;margin:0 0 12px}h2{font-size:15px;margin:0 0 8px;color:var(--mut)}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:12px}
.card{background:var(--card);border-radius:10px;padding:12px;margin-bottom:12px;overflow-x:auto}
.big{font-size:24px;font-weight:600}table{width:100%;border-collapse:collapse}td,th{padding:4px 6px;text-align:left;border-bottom:1px solid #262a33;white-space:nowrap}
.g{color:var(--g)}.r{color:var(--r)}.m{color:var(--mut)}button{font-size:15px;padding:8px 18px;border:0;border-radius:8px;cursor:pointer;margin-right:8px}
#start{background:var(--g)}#stop{background:var(--r);color:#fff}</style></head><body>
<h1>MrTrader Desk <span class="m">(soldi finti)</span></h1>
<div class="card"><button id="start" onclick="act('start')">▶ Avvia</button><button id="stop" onclick="act('stop')">■ Ferma</button> <b id="run"></b></div>
<div class="row"><div class="card"><h2>Capitale</h2><div class="big" id="eq"></div></div><div class="card"><h2>Contanti</h2><div class="big" id="cash"></div></div>
<div class="card"><h2>Vinte / Perse</h2><div class="big" id="wl"></div></div><div class="card"><h2>Posizioni aperte</h2><div class="big" id="np"></div></div></div>
<div class="card"><h2>Posizioni aperte</h2><table id="pos"></table></div>
<div class="card"><h2>Operazioni</h2><table id="tr"></table></div>
<div class="card"><h2>Perché scarta (motivi più frequenti)</h2><table id="why"></table></div>
<div class="card"><h2>Ultimi scarti</h2><table id="kills"></table></div>
<div class="card"><h2>Smart wallet (<span id="nw">0</span>)</h2>
<p class="m">Incolla gli indirizzi, uno per riga. Quelli non validi vengono scartati.</p>
<textarea id="wl_in" rows="6" style="width:100%;box-sizing:border-box;background:#0f1115;color:var(--fg);border:1px solid #333;border-radius:8px;padding:8px;font:12px monospace"></textarea>
<button style="background:var(--g)" onclick="saveW()">Salva lista</button> <span id="wmsg" class="g"></span></div>
<div class="card"><h2>Attività</h2><table id="ev"></table></div>
<script>
const $=id=>document.getElementById(id), usd=x=>'$'+Number(x).toLocaleString('it-IT',{maximumFractionDigits:2});
const hm=s=>s?new Date(s).toLocaleString('it-IT',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const tbl=(h,rows)=>'<tr>'+h.map(x=>'<th>'+x+'</th>').join('')+'</tr>'+(rows.length?rows.join(''):'<tr><td class="m" colspan="9">niente per ora</td></tr>');
let wLoaded=false;
async function saveW(){const r=await (await fetch('/wallets',{method:'POST',body:$('wl_in').value})).json();$('wmsg').textContent='Salvati '+r.saved;wLoaded=false;load()}
async function act(a){await fetch('/'+a,{method:'POST'});load()}
async function load(){const d=await (await fetch('/api')).json();
$('run').innerHTML=d.running?'<span class="g">● acceso</span>':'<span class="r">● spento</span>';
$('eq').innerHTML=usd(d.equity)+' <span class="'+(d.equity>=10000?'g':'r')+'">'+((d.equity/10000-1)*100).toFixed(2)+'%</span>';
$('nw').textContent=d.wallets.length;if(!wLoaded){$('wl_in').value=d.wallets.join('\\n');wLoaded=true}
$('cash').textContent=usd(d.cash);$('wl').textContent=d.wins+' / '+d.losses;$('np').textContent=d.positions.length;
$('pos').innerHTML=tbl(['Moneta','Investito','Entrata','Ora','Stop','P&L'],d.positions.map(p=>`<tr><td>${esc(p.symbol)}</td><td>${usd(p.usd)}</td><td>${p.entry.toPrecision(4)}</td><td>${p.last.toPrecision(4)}</td><td>${p.stop.toPrecision(4)}${p.trailing?' (trail)':''}</td><td class="${p.pnl_pct>=0?'g':'r'}">${p.pnl_pct}%</td></tr>`));
$('tr').innerHTML=tbl(['Ora','Lato','Moneta','Dettaglio'],d.trades.map(t=>`<tr><td>${hm(t.ts)}</td><td>${t.side=='buy'?'COMPRA':'VENDE'}</td><td>${esc(t.symbol)}</td><td>${t.side=='buy'?'voto '+t.score+', '+usd(t.usd):`<span class="${t.pnl_usd>0?'g':'r'}">${t.pnl_pct}% (${usd(t.pnl_usd)})</span> ${esc(t.reason)}`}</td></tr>`));
$('why').innerHTML=tbl(['Motivo','Volte'],d.top_reasons.map(r=>`<tr><td>${esc(r[0])}</td><td>${r[1]}</td></tr>`));
$('kills').innerHTML=tbl(['Ora','Agente','Moneta','Motivi'],d.kills.map(k=>`<tr><td>${hm(k.ts)}</td><td>${esc(k.agent)}</td><td>${esc(k.symbol)}</td><td>${esc((k.reasons||[]).join(' · '))}</td></tr>`));
$('ev').innerHTML=tbl(['Ora','Tipo','Dettaglio'],d.events.map(e=>`<tr><td>${hm(e.ts)}</td><td>${esc(e.kind)}</td><td class="m">${esc(JSON.stringify(e).slice(0,140))}</td></tr>`));}
load();setInterval(load,5000);</script></body></html>"""


def start_desk():
    p = subprocess.Popen([sys.executable, os.path.join(DIR, "desk.py")], cwd=DIR, start_new_session=True,
                         stdout=open(os.path.join(DIR, "desk.out"), "a"), stderr=subprocess.STDOUT)
    open(PID, "w").write(str(p.pid))


class H(BaseHTTPRequestHandler):
    def send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == "/api":
            self.send(json.dumps(summary()))
        else:
            self.send(PAGE, "text/html")

    def do_POST(self):
        if self.path == "/start" and not desk_pid():
            start_desk()
        elif self.path == "/wallets":
            text = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 200_000)).decode(errors="ignore")
            good = sorted({w for w in re.split(r"[\s,;]+", text) if is_wallet(w)})
            tmp = os.path.join(DIR, "smart_wallets.txt.tmp")
            open(tmp, "w").write("\n".join(good) + "\n")
            os.replace(tmp, os.path.join(DIR, "smart_wallets.txt"))
            return self.send(json.dumps({"saved": len(good)}))
        elif self.path == "/stop" and desk_pid():
            os.kill(desk_pid(), signal.SIGTERM)
            os.remove(PID)  # no pid file = user wants it off, so no autostart after reboot
        self.send("{}")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if os.path.exists(PID) and not desk_pid():
        start_desk()  # desk was on when the machine/dashboard went down: resume it
    print("Apri nel browser: http://localhost:8050   (Ctrl+C per chiudere)")
    ThreadingHTTPServer(("127.0.0.1", 8050), H).serve_forever()
