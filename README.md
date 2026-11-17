# MemeDesk

Desk in paper trading su memecoin Solana. **Nessun wallet, nessuna chiave privata, nessuna firma, nessun ordine reale.** Ogni fill è simulato.

Il codice in questo repository sostituisce il vecchio progetto MrTrader. La cronologia git precedente resta nel repository.

## Come è fatto

Sei agenti con handoff rigidi: Scout → Filter → Analyst → Executor → Risk, più Logger.

- **Scout**: scopre token da DexScreener, Jupiter e GeckoTerminal. Scarta età fuori da 30 minuti–72 ore e market cap fuori da 300k–20M $.
- **Filter**: prima i controlli di mercato (liquidità, volume, pompa), poi RugCheck e RPC. Scarta autorità di mint/freeze, LP non bloccata, top holder e dev troppo concentrati, storico rug, bundle, transfer fee e permanent delegate.
- **Analyst**: punteggio 0–100, passa con **70+**. Momentum, compratori unici, buy/sell, crescita holder, rapporto liquidità/mcap, più un fattore opzionale sui wallet intelligenti (se i dati mancano, i pesi vengono rinormalizzati: non penalizza).
- **Executor**: al massimo **8%** dell'equity paper per trade; partenza 10.000 $; massimo 10 posizioni aperte; cooldown di rientro 6 ore. Il fill usa una quote Jupiter sulla size esatta (solo quote, niente firma), con fallback al modello DexScreener.
- **Risk**: stop **-6%** sullo stesso fill; trailing che si attiva a **+12%** con distanza **6%**. Poll ogni 30 secondi.
- **Logger**: tutto finisce in SQLite (`desk.sqlite`) e in `events.jsonl`.

Regole ombra (solo log, non muovono i trade): `partial_tp`, `smart_exit`, `skeptic`, `min2_smart`, `trail_4pct`, `combo_tp_trail4`. Adottarle sul live paper richiede una conferma esplicita.

## Avvio locale (paper)

Le variabili d'ambiente sono elencate in `.env.example` (solo i nomi). Non committare mai i valori.

```bash
# una tantum: carica le variabili in una shell, oppure usa un EnvironmentFile
chmod +x run.sh
setsid nohup ./run.sh >/dev/null 2>&1 < /dev/null &
```

`run.sh` tiene in vita `desk.py`, `syncer.py` e `smartwallets.py`. Se il sync o i wallet intelligenti si fermano, il desk continua a fare paper trading.

```bash
kill $(cat supervisor.pid)                 # stop
MEMEDESK_DATA=/tmp/somedir python3 desk.py --once
python3 report.py                          # report dall'avvio
python3 report.py --hours 8
```

File di stato (restano in locale, sono nel `.gitignore`): `desk.sqlite`, `events.jsonl`, `summary.txt`, `desk.out`, `sync.out`, `smart.out`.

## Dashboard in tempo reale

L'interfaccia è in `web/` (Next.js, export statico, UI in italiano, orari Europe/Rome). I dati arrivano da Supabase Realtime (solo lettura, RLS: `SELECT` per `anon` / `authenticated`).

Lo schema è in `supabase/migrations/0001_init.sql`. `syncer.py` legge `desk.sqlite` in sola lettura e invia i lotti alla Edge Function `desk-ingest` (già deployata sul progetto Supabase; il sorgente della funzione non è in questo repository perché contiene un hash del token di ingest).

Build della dashboard:

```bash
cp .env.example web/.env.local   # valorizza NEXT_PUBLIC_SUPABASE_URL e NEXT_PUBLIC_SUPABASE_ANON_KEY
cd web && npx next build && cd ..
python3 deploy/build_worker.py   # genera deploy/worker.js (file generati, non committati)
```

Il worker Cloudflare `memedesk-dashboard` è già in produzione e non dipende da questo repository (upload diretto, non un collegamento Git).

## Deploy su una VM

Unità systemd e configurazioni utili sono in `deploy/`. Il desk va in `/opt/memedesk`, i segreti in `/etc/memedesk/memedesk.env` (`chmod 600`), caricati con `EnvironmentFile`. Comandi generici e procedura di migrazione: `VM.md`. Controllo da remoto:

```bash
VM=user@host KEY=/path/to/ssh_key ./vm_status.sh
```

## Avvisi Telegram

`notify.py` invia avvisi di ingresso, uscita, pass di Filter/Analyst, riavvii e salute del desk. Se un invio fallisce, il messaggio resta in coda e viene ritentato. Il watchdog sulla VM controlla anche che il bot risponda.
