# MemeDesk

Desk in paper trading su memecoin Solana. **Nessun wallet, nessuna chiave privata, nessuna firma, nessun ordine reale.** Ogni fill è simulato.

Il codice in questo repository sostituisce il vecchio progetto MrTrader. La cronologia git precedente resta nel repository.

## Come è fatto

Sei agenti con handoff rigidi: Scout → Filter → Analyst → Executor → Risk, più Logger.

Regole live attuali: **`v11_clean`** (= regole `v10_sel` approvate 2026-11-19 + costi di rete). Fonte di verità: `CFG` in `desk.py`. Il saldo paper è ripartito da zero con **1.000 $** (i prezzi sono in dollari); i trade precedenti restano nel database ma dashboard e report contano dall'ultima ripartenza.

- **Scout**: scopre token da DexScreener, Jupiter e GeckoTerminal. Scarta età fuori da 30 minuti–**6 ore** e market cap fuori da 100k–20M $.
- **Filter**: prima i controlli di mercato (liquidità ≥ 25k $, volume 1h ≥ 15k $, salita 1h ≤ +150%), poi RugCheck e RPC. Scarta autorità di mint/freeze, LP non bloccata, top 10 holder > 35%, dev > 5%, bundle > 20%, storico rug, transfer fee e permanent delegate. Dati mancanti = scarto.
- **Analyst**: punteggio 0–100, passa con **55+**. In più il punteggio "vecchio" (smart 0 conta 0) deve essere **≥ 70** e la pressione d'acquisto positiva (buy/sell ≥ 1,2 a 5 min e ≥ 1,1 a 1 h, compratori netti 1 h > 0).
- **Executor**: **3%** dell'equity per trade (minimo 10 $), al massimo lo 0,25% della liquidità del pool; massimo 15 posizioni aperte; cooldown di rientro 1 ora. Se esiste il file `PAUSE_ENTRIES` non apre nuove posizioni. Il fill usa una quote Jupiter sulla size esatta (solo quote, niente firma), con fallback al modello DexScreener.
- **Risk**: stop **-10%** dal prezzo di mercato all'ingresso; **breakeven**: superato +8% sul fill, lo stop sale a fill +1%; trailing da **+12%** con distanza **4%**; **smart exit**: chiude quando un wallet intelligente tracciato vende. Prezzo di uscita in tempo reale via WebSocket Helius, fallback al polling DexScreener ogni 30 secondi.
- **Costi**: ogni acquisto e vendita paper paga slippage, fee del pool e **0,05 $ di commissione di rete Solana**.
- **Logger**: tutto finisce in SQLite (`desk.sqlite`) e in `events.jsonl`. Ogni score e posizione ha il tag `ruleset`; ogni posizione ha `entry_meta`.

Regole ombra (solo log, non muovono i trade): `strict_v7` (le vecchie regole di ingresso v6/v7), uscita `old_rules_v5`, `partial_tp`, `skeptic`, `min2_smart`, `combo_tp_trail4`, `time_stop_60m`, più le v9 in `shadow_v9.py` (ghost con 8 varianti di uscita, ingressi e filtri ombra). Budget Helius: tetto globale 24k crediti/giorno, cache smart 30 min. Adottarle sul live paper richiede una conferma esplicita.

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
