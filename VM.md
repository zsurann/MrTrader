# MemeDesk su una VM (solo paper)

Questo file descrive come è installato il desk su una macchina virtuale. Non contiene indirizzi IP, chiavi SSH o segreti.

Il desk live gira su una VM Oracle Always Free (Ubuntu, forma Micro AMD) nella home region. La copia di sviluppo non deve essere avviata se è presente il marker locale `RUNNING_ON_VM` (è nel `.gitignore`): `run.sh` rifiuta l'avvio.

## Layout

- Codice e stato: `/opt/memedesk` (utente di sistema `memedesk`)
- Segreti: `/etc/memedesk/memedesk.env` (root, modalità 600), caricati da systemd con `EnvironmentFile`. Variabili: vedi `.env.example`. Non ci sono file `.env` nella directory dell'app: `syncer.py` e `notify.py` usano l'ambiente se quei file mancano.
- Python: `/usr/bin/python3` (stdlib, senza venv)
- Fuso orario consigliato: Europe/Rome, con NTP attivo
- Swap consigliato su una VM da ~1 GB, con `vm.swappiness=10`

## Unità systemd (copie in `deploy/`)

- `memedesk.service`: avvia `run.sh` (desk + syncer + smartwallets), `Restart=always`, abilitato al boot. Limiti memoria e hardening (`MemoryHigh=700M` / `MemoryMax=850M`, `OOMScoreAdjust=-500`, `ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, `PrivateTmp`).
- `memedesk-watchdog.timer` → `memedesk-watchdog.service`: ogni 5 minuti esegue `vm_watchdog.py`. Se ci sono almeno 3 errori consecutivi o nessun ciclo riuscito da più di 10 minuti, riavvia il servizio e manda al massimo un avviso Telegram all'ora, ma solo se non ci sono posizioni paper aperte. Se ci sono posizioni aperte (o lo stato non è leggibile) non riavvia e avvisa. Controlla anche che il bot Telegram risponda e svuota la coda.
- `memedesk-keepalive.service` (script `keepalive.py`): carico CPU a priorità minima per il recupero Always Free da inattività. `Nice=19`, `CPUSchedulingPolicy=idle`, `IOSchedulingClass=idle`, `CPUAffinity=0`, `MemoryMax=20M`, `Restart=always`, abilitato al boot. Si ferma da solo mentre il desk è in esecuzione. Per spegnerlo, senza toccare il desk:

  ```bash
  sudo systemctl disable --now memedesk-keepalive
  ```

Altri file in `deploy/`: rotazione log, tetto di journald, sysctl (swappiness), unità keepalive.

Un controllo esterno (Cloudflare Worker cron, piano Free) avvisa su Telegram se la VM intera è irraggiungibile per più di 15 minuti. Non usa minuti di Grok Bot. Sorgente in `monitor/`.

## Comandi sulla VM

```bash
# Riavvio solo se non ci sono posizioni paper aperte (obbligatorio per i deploy):
sudo memedesk-safe-restart
# Per forzare comunque (sconsigliato con posizioni aperte): sudo memedesk-safe-restart --force
# Non usare `systemctl restart memedesk` nei deploy: ignora le posizioni aperte.
systemctl status memedesk --no-pager
sudo tail -f /opt/memedesk/desk.out
sudo grep " cycle " /opt/memedesk/desk.out | tail -1
sudo systemctl stop memedesk
# Se lo stop deve durare, ferma anche il watchdog, altrimenti dopo 10 minuti riparte:
sudo systemctl disable --now memedesk memedesk-watchdog.timer
```

Da un altro computer, con host e chiave impostati da te (nessun valore predefinito in questo repo):

```bash
VM=user@host KEY=/path/to/ssh_key ./vm_status.sh
```

In console Oracle non usare Terminate: cancellerebbe la VM. Stop e Reboot fermano il desk.

## Regole live attuali (v10_sel, 2026-11-19)

Solo paper. Le sezioni sotto sono uno storico. Fonte di verità: `CFG` in `desk.py`. Classifica delle regole ombra: `sudo python3 /opt/memedesk/shadow_report.py [--ruleset v10_sel]`.

Scelta dell'utente dopo 203 trade `v8_loose` (PnL −1.389 $; nessuna regola ombra rendeva la strategia positiva). Applicate le uscite che miglioravano gli stessi trade e i 3 filtri d'ingresso con la perdita per trade più bassa:

| regola ex-ombra | delta su v8_loose | ora live |
| --- | --- | --- |
| `smart_exit` | +159 $ (146 trade) | chiude quando un wallet intelligente vende (segnale da `smartwallets.py`, ogni ~2 min) |
| `trail_4pct` | +43 $ | trailing 6% → 4% |
| `breakeven_8` | +39 $ | dopo +8% sul fill, stop a fill +1% |
| `new_pair_6h` | −2,9 $/trade contro −6,6 | età massima Scout 168 h → 6 h |
| `buy_pressure` | −3,9 $/trade | buy/sell ≥ 1,2 (5 min) e ≥ 1,1 (1 h), compratori netti 1 h > 0 |
| `old_rules_v5` | −4,0 $/trade | punteggio vecchio (smart 0 = 0) ≥ 70 |

Il saldo paper riparte da **1.000 $** al primo avvio di `v10_sel` (chiave `epoch_v10_sel` in `ledger`; solo con 0 posizioni aperte). Taglia 3% = ~30 $, minimo ordine 10 $. Le posizioni vecchie restano nel database con `ruleset='v8_loose'`.

### v11_clean (2026-11-19): ripartenza da zero

Su richiesta dell'utente: nuovo `RULESET` → al primo avvio con 0 posizioni il saldo paper riparte da 1.000 $ (`ledger.epoch_ts`). Nessun trade cancellato: report (`--ruleset v11_clean`) e dashboard (filtro su `cfg.epoch_ts`) contano solo dalla ripartenza.

- Commissione di rete Solana 0,05 $ per swap dentro ogni fill paper (live, ombra e ghost).
- `trough_price` sulle posizioni: minimo toccato (MAE). Il report mostra vincenti %, profit factor, media/trade, drawdown, costo medio d'ingresso/uscita, MFE/MAE e un intervallo di confidenza 90% (bootstrap) per ogni regola ombra: "solido" se anche il bordo basso è positivo.
- Nuova regola ombra `entry_slip_15`: salta gli ingressi con fill > 1,5% sopra il mercato.
- Feed Helius: in un crollo resta la fonte di uscita se il prezzo dalle riserve è sotto i riferimenti e uno snapshot RPC recente lo conferma.
- Vendite: quote Jupiter sotto la fascia del 50% usata se l'impatto della route è ≤ 5% (prezzo vero dopo un crollo). Scout: solo coppie SOL/USDC/USDT.

### Ombra "fomo" (solo log, 2026-11-19)

Dal video TikTok di un bot "fomo" (solo schermate, nessuna regola pubblica): ingresso `fomo_score_70` (score ≥ 70) e uscita ghost `fomo_stop6_trail10` (stop −6%, trailing 10% da +15%; il trailing è una nostra ipotesi). La loro taglia, 25–40% del saldo per trade, non è copiata: cambierebbe il rischio, non l'esito percentuale del trade.

### Nuove regole ombra v10 (solo log, 2026-11-19)

Da una ricerca online: non esistono backtest seri pubblici su memecoin Solana, quindi sono ipotesi da misurare, non regole provate.

- Filtri d'ingresso (`shadow_events`, `ENTRY_GATE`): `top10_15` (top 10 ≤ 15%: i lanci ad alto rischio hanno detentori iniziali più concentrati, MemeTrans arXiv 2602.13480), `small_trades_200` (volume 1 h per trade ≤ 200 $: volume medio per compratore alto = lancio a rischio, stesso studio), `turnover_03_3` (volume 1 h / liquidità tra 0,3 e 3), `dip_in_uptrend` (1 h ≥ +5% e 5 min ≤ 0: comprare il ritracciamento).
- Uscite (ghost v9, confronto con `ghost_live_baseline` che ora replica le regole v10): `tranche_25_50_100` (vende un quarto a +25%, +50%, +100%, il resto con trailing 10%), `stale_3h` (regole live + uscita dopo 3 h se sotto +5%), `wide_stop50_trail20` (stop −50%, trailing 20% da +30%).

## Aggiornamento automatico da GitHub

`memedesk-autoupdate.timer` ogni 10 minuti scarica il branch `claude/brave-turing-8snpk9`. Se c'è un commit nuovo e **0 posizioni paper aperte**: backup in `/opt/memedesk/pre_autoupdate/`, copia dei file della radice del repo in `/opt/memedesk`, `memedesk-safe-restart`, attesa di un ciclo riuscito (max 8 min). Se il ciclo non arriva: rollback automatico, il commit viene saltato (`.bad_rev`), avviso Telegram. Con posizioni aperte rimanda al giro dopo. Le unità systemd in `deploy/` non si aggiornano da sole (solo `run.sh` e i `.py` della radice).

Installazione (una volta, da root sulla VM). Il token serve solo se il repo è privato (GitHub fine-grained, solo `Contents: read` su questo repo):

```bash
sudo install -m 600 /dev/stdin /etc/memedesk/github_token <<< 'TOKEN'
sudo git -c http.extraHeader="Authorization: Basic $(printf x-access-token:%s "$(sudo cat /etc/memedesk/github_token)" | base64 -w0)" clone -q --depth 1 -b claude/brave-turing-8snpk9 https://github.com/zsurann/MrTrader.git /var/lib/memedesk-src
sudo bash /var/lib/memedesk-src/deploy/install-autoupdate.sh
```

Se i file in `/opt/memedesk` sono diversi da GitHub, l'installer si ferma e li elenca; `FORCE=1` per sovrascriverli. Log: `journalctl -u memedesk-autoupdate`. Stop: `sudo systemctl disable --now memedesk-autoupdate.timer`.

## Fonti dati e limiti API (v3, 2026-11-17)

Solo paper. Le regole live non cambiano (stop −6%, trailing +12%/6%, 8% per trade, punteggio 70+). Le novità sono solo shadow (solo log).

Perché GeckoTerminal rallentava il desk:

- L'API pubblica di GeckoTerminal è documentata a circa 10 chiamate/minuto (la pagina keyless parla di circa 5/minuto, variabile, con un pool condiviso per IP). Il desk andava a 2,8 s tra chiamate (21/minuto) con tre utenti: scoperta del desk (17 pagine ogni 3 minuti), smartwallets (22 pagine per aggiornamento), Analyst e Skeptic. Nei cicli 117–125: 17 risposte OK e 9 risposte 429 (35% rifiutate). Ogni 429 aspetta almeno 12 s e allarga il passo condiviso, quindi Filter/Analyst e lo Skeptic shadow restavano in coda (filter fino a 70 s, ciclo fino a 86 s).
- Jupiter: `lite-api.jup.ag` e `api.jup.ag` (anche con la nostra chiave) condividono una sola finestra per IP di 5 richieste ogni 10 s (header `x-ratelimit-*`). Il desk li trattava come due host da 1,25 s ciascuno, da qui 0,5–0,7 risposte 429 per ciclo.

Modifiche:

- Gruppi di rate limit: i due host Jupiter condividono il gruppo `jup` a 2,05 s e si rispetta `x-ratelimit-reset`. GeckoTerminal a 12,5 s di base.
- Le liste di scoperta Jupiter si aggiornano in un thread in background (stessi URL e TTL): il ciclo non aspetta più Jupiter. Il giro GeckoTerminal passa da 180 s a 420 s, con cache delle pagine condivisa con smartwallets.
- Lo Skeptic shadow (OHLCV GeckoTerminal) gira fuori dal percorso critico del ciclo.
- Shadow `fast_stop`: ogni 10 s una chiamata Jupiter price/v3 per le posizioni aperte. Registra quando il prezzo tocca lo stop live e, alla chiusura, quanti secondi prima e a che prezzo lo avrebbe visto. Non chiude mai trade.
- Shadow `pp_discovery`: un WebSocket gratuito PumpPortal (`subscribeMigration`, senza chiave) salva le migrazioni pump.fun → PumpSwap e misura quante sfuggono allo Scout. Non entra nella scoperta live.
- `wsmini.py`: client WebSocket minimo con sola stdlib.

Misure (log dei cicli, ora di Roma):

| | prima (cicli 100–125) | dopo, fattore smart ancora spento (cicli 126–138) |
| --- | --- | --- |
| durata ciclo media / mediana / max | 32,8 / 26,6 / 85,9 s | 17,9 / 18,1 / 28,0 s |
| filter_s max | 69,9 s | 16,3 s |
| 429 GeckoTerminal per ciclo | 1,0–1,3 | 0 |
| 429 Jupiter per ciclo | 0,5–0,7 | 0 |
| aggiornamento winners smartwallets | 389–698 s | 50–199 s |

Nota misurata dopo: dalle 13:22 circa la lista smart wallet ha raggiunto 10 indirizzi e il fattore `smart_wallets` dell'Analyst è diventato attivo. Serve una chiamata GeckoTerminal `/trades` per ogni coin che passa il Filter (2–3 per ciclo). Dalle 13:46 c'è un "gate": durante Filter/Analyst il giro GeckoTerminal in background e lo Skeptic shadow si fermano, e le chiamate dell'Analyst usano un passo prioritario di 6,2 s. Cicli 139–153: media 58,6 s (mediana 45,4, max 122,9), filter medio 47,2 s, 0,33 risposte 429 GeckoTerminal per ciclo, 0 per Jupiter e DexScreener. Ora il limite è questa chiamata GeckoTerminal (circa 5 al minuto per IP). Ritarda solo le entrate, mai le uscite (Risk gira in un thread separato su DexScreener). I cicli restano dentro l'intervallo di 180 s.

Prime prove shadow: `bul` (posizione 5) — `fast_stop` ha visto lo stop toccato 48,5 s prima dell'uscita live, a un prezzo 0,85% migliore. L'uscita live ha realizzato −10,7% con stop −6% (terza volta). `pp_discovery`: 46 migrazioni in 24 h, 38 non viste dallo Scout, 0 dentro la finestra dello Scout finora.

Rollback: rimettere `desk.py` e `smartwallets.py` della versione precedente e riavviare `memedesk`.

## Uscite in tempo reale con Helius (v4, 2026-11-17)

Solo paper, con OK esplicito. Le regole non cambiano (stop −6%, trailing da +12% con distanza 6%). Cambia solo da dove arriva il prezzo per le uscite e quanto è veloce.

- `HeliusFeed` (thread `helius-ws`): un solo WebSocket Helius standard, `accountSubscribe` sui due vault del pool di ogni posizione aperta. Pool supportati: PumpSwap, Raydium CPMM, Raydium AMM v4 (vault e mint verificati on-chain). CLMM, DLMM e Orca restano sul polling.
- Prezzo = riserva quote / riserva base × prezzo USD della quote (SOL da DexScreener).
- Il thread `ws-risk` valuta ogni 0,25 s quando arriva un prezzo nuovo e ricontrolla ogni 2 s. Stessa logica delle regole del polling.
- Si torna da solo al polling DexScreener (evento `PRICE_SOURCE` a ogni cambio) se: WS scollegato, nessun dato da più di 15 s, pool non supportato, differenza >3% al controllo ogni 60 s, budget crediti superato (6000/giorno stimati), nessun prezzo di riferimento fresco, oppure prezzo dalle riserve lontano più dell'8% da DexScreener/Jupiter.
- Shadow `poll_stop`: la vecchia uscita a polling continua a girare solo nei log. Gli eventi EXIT salvano `trigger_src`, `trigger_px` e `fill_vs_trigger_pct`.
- Crediti: tabella `helius_ws_usage` ed evento `WS_STATUS` ogni 10 minuti. Stima: circa 1,4k crediti al giorno per posizione aperta tutto il giorno (controllo ogni 60 s) più 1–5k per un pool molto attivo, solo mentre la posizione è aperta. Senza posizioni: circa 1 credito. Piano gratuito: 1M al mese.
- Prova isolata: BAWSAQ con −9% rispetto a DexScreener → ritorno al polling come previsto; CATE → uscita da helius_ws, prezzo di uscita −0,116% rispetto al trigger.
- Dopo il deploy (14:33–15:01): ciclo medio 55,9 s, 0 risposte 429 da Jupiter, nessun traceback, WS collegato, Telegram ok. Nessuna posizione aperta in quella finestra.
- Nuova chiave Jupiter: valida, ma stessa finestra per IP (5 richieste ogni ~10 s), quindi il limite resta a 2,05 s. Jupiter non ha un equivalente di GeckoTerminal `/trades`, quindi non può alleggerire GeckoTerminal.
- Rollback: rimettere `desk.py`, `wsmini.py` e `syncer.py` della versione precedente, oppure `helius_ws_live=False` (il feed resta solo shadow).

## Audit e fattore smart wallet su Helius (v5, 2026-11-17)

Solo paper. Le regole live non cambiano.

- Audit: tutti i componenti erano attivi e funzionanti (feed Helius, poll_stop/fast_stop, pp_discovery, fattore smart, regole shadow, chiave Jupiter, limiti e cache, keepalive, watchdog, monitor, Telegram, dashboard). Nessun guasto silenzioso. Nota: Helius conta `getSignaturesForAddress` e `getTransaction` a 1 credito, non 10.
- Il fattore `smart_wallets` dell'Analyst non usa più GeckoTerminal `/trades` (ultimi 300 trade del pool, spesso solo 7–60 minuti delle 6 ore previste, circa 5 chiamate al minuto per IP). Ora, per ogni smart wallet, legge da Helius la storia del suo token account per quel token e cerca un acquisto ≥ $50 nelle ultime 6 ore. Stessa domanda, finestra completa, tutti i pool. Circa 12–22 crediti e 0,2–2 s per controllo, tetto 15.000 crediti al giorno, GeckoTerminal resta come riserva. Un controllo su quattro viene confrontato con GeckoTerminal fuori dal percorso critico (`smart_src`).
- Validazione su pool reali: 48 acquirenti su 55 trovati (87%) nel caso che conta (chi compra riceve i token). Il 57% degli "acquirenti" di GeckoTerminal sono relayer o bot. Sulla lista smart reale: 8 pool su 10 identici; nei 2 diversi Helius vede un acquisto fuori dalla finestra di GeckoTerminal e GeckoTerminal conta un arbitraggio senza acquisto netto.
- Risultato: ciclo medio da 47,1 s a circa 18 s, Filter/Analyst da 36,8 s a circa 1,4 s.
- Nuove regole shadow (solo log): ingresso `organic_50`, `churn_50`, `m5_chase_10`, `top1_10`; uscita `time_stop_60m`, `breakeven_8`. La dashboard riceve i dati in `rule_v5` (interfaccia invariata).
- Rollback: rimettere i file della versione precedente, oppure `SW["helius_smart"]=False`.

## Regole live v6 (approvate dall'utente, 2026-11-17)

Solo paper, valgono per le nuove posizioni.

| regola | prima | v6 |
| --- | --- | --- |
| stop iniziale | −6% dal prezzo pagato | −10% dal prezzo di mercato all'ingresso |
| trailing | da +12% sul prezzo pagato, 6% sotto il picco | da +12% sul prezzo di mercato all'ingresso, 6% sotto il picco |
| taglia | 8% dell'equity | 5% dell'equity, al massimo lo 0,25% della liquidità del pool |
| liquidità minima | 40.000 $ | 100.000 $ |
| fattore smart wallet | 0 wallet = 0 (circa −15 punti) | può solo aggiungere: base + 0,15 × smart/100 × (100 − base); con 3+ wallet uguale a prima |
| soglia punteggio | 70 | 70 |

Perché: si comprava circa il 2,9% sopra il prezzo di mercato, quindi lo stop a −6% scattava dopo circa il 3% di discesa, e gli stop chiudevano a −11%. Entrata più uscita costavano circa il 5,5% con 800 $. Con lo 0,25% della liquidità il modello dà circa l'1,25% per lato, circa il 2,5% in totale.
La regola shadow `old_rules_v5` registra cosa avrebbero fatto le regole precedenti. Il trailing shadow al 4% ora parte dallo stesso prezzo di mercato della regola live.

## Rilevamento cicli falliti (v7, 2026-11-17)

Durante l'incidente v6 tutti i cicli fallivano ma l'heartbeat del syncer continuava ad aggiornarsi, quindi né il watchdog né il monitor se ne accorgevano.

- `desk.py` scrive dopo ogni ciclo `cycle_health.json` (scrittura atomica): errori consecutivi, ora e numero dell'ultimo ciclo riuscito, ultimo errore ripulito da eventuali segreti (max 200 caratteri).
- `syncer.py` invia questi campi a Supabase in `desk_status`: `last_success_at`, `last_success_cycle`, `consecutive_cycle_errors`, `last_cycle_error`, `last_cycle_error_at`.
- `vm_watchdog.py` (timer ogni 5 minuti): se ci sono almeno 3 errori consecutivi o nessun ciclo riuscito da più di 10 minuti (con 10 minuti di tolleranza dopo un avvio), riavvia `memedesk` e manda un avviso Telegram (al massimo uno all'ora), ma solo se non ci sono posizioni paper aperte in `desk.sqlite`. Se ci sono posizioni aperte, o se il conteggio non è leggibile, il riavvio viene saltato e arriva un avviso (anche questo al massimo uno all'ora). Quando torna un ciclo riuscito dopo un riavvio effettivo manda il messaggio di ripristino. Test sicuro, senza riavvio e senza toccare lo stato:

  ```bash
  sudo systemd-run --wait --pipe -q -p EnvironmentFile=/etc/memedesk/memedesk.env /usr/bin/python3 /opt/memedesk/vm_watchdog.py --simulate
  ```

- Il worker Cloudflare `memedesk-monitor` avvisa anche se `last_success_at` è più vecchio di 15 minuti (`cycles_failing`), con lo stesso intervallo di un'ora e lo stesso messaggio di ripristino. `/run?mode=test-cycle-alert` manda un messaggio di prova senza cambiare lo stato.

Le regole di trading non cambiano.

## Regole di ingresso v8_loose (approvate dall'utente, 2026-11-18 14:12 Europe/Rome)

Solo paper. L'utente ha chiesto molte più operazioni con soldi finti, per raccogliere dati e scegliere tra regole live, smart wallet e ombra. Cambiano solo le regole di INGRESSO; le uscite restano uguali (stop −10% dal prezzo di mercato all'ingresso, trailing da +12% con distanza 6%, uscite in tempo reale con Helius e fallback DexScreener). Tutte le regole ombra continuano a registrare. Deploy con `memedesk-safe-restart` (0 posizioni aperte). Backup: `/opt/memedesk/pre_v8/`.

| parametro | v6/v7 | v8_loose |
| --- | --- | --- |
| soglia score | 70 | 55 |
| liquidità minima | 100k $ | 25k $ |
| volume 1h minimo | 50k $ | 15k $ |
| mcap minima (Scout) | 300k $ | 100k $ |
| età massima (Scout) | 72 h | 168 h |
| già salita in 1h | +80% | +150% |
| top 10 holder | 25% | 35% |
| quota dev | 3% | 5% |
| bundle/insider | 10% | 20% |
| taglia | 5% dell'equity | 3% dell'equity (sempre ≤ cash e ≤ 0,25% della liquidità) |
| posizioni aperte max | 10 | 15 |
| cooldown di rientro | 6 h | 1 h |
| budget WS Helius/giorno | 6000 | 9000 |
| cache fattore smart (nuova, prestazioni) | — | 600 s (1800 s da v8.1) |

Restano: mint/freeze revocate, LP bruciata/bloccata, RugCheck rugged, rug passati del dev, transfer fee, permanent delegate, fail-closed sui dati mancanti.

Ogni trade ha `positions.ruleset = 'v8_loose'` e `positions.entry_meta` (JSON con score, fascia di score `55-60`/`60-65`/`65-70`/`70+`, componenti, liquidità, mcap, età, volumi, controlli Filter, `strict_v7_pass`). Ogni score ha `scores.ruleset`. Nuova regola ombra `strict_v7` in `shadow_events`: dice se le vecchie regole v6/v7 avrebbero bloccato l'ingresso. `old_rules_v5` mantiene la soglia storica 70.

Baseline prima di v8 (ultime 24 h): 0 trade; ~10,7 coin per ciclo nella finestra Scout, 0,1 passaggi Filter per ciclo, 4 coin con score (massimo 60–65). Primo ciclo dopo il deploy: 6 ingressi; 8 nei primi 10 minuti.

Rollback (solo con 0 posizioni aperte): ricopiare `pre_v8/*.py` e `sudo memedesk-safe-restart`.

## v8.1 budget Helius + regole ombra v9 (2026-11-18 14:51 Europe/Rome)

Regole di trading invariate. Chiuse a mercato le 2 posizioni paper aperte (autorizzato dall'utente) con `manual_close.py`, motivo `manual_close_deploy`: BUTTHOLE +5,87 $ (+1,98%), SHIT −18,74 $ (−8,20%). Sono escluse dalle statistiche di strategia (`exit_reason NOT LIKE 'manual_close%'`). Poi `memedesk-safe-restart` con 0 posizioni aperte. Backup: `/opt/memedesk/pre_v81/`.

- Pausa ingressi: se esiste `/opt/memedesk/PAUSE_ENTRIES` il desk non apre nuove posizioni (le uscite continuano).
- Helius (gratis: ~33k crediti/giorno, 10 richieste/s): cache smart wallet 10 → 30 min; lookup saltato se lo score non può arrivare a 55 nemmeno con 3+ smart wallet; budget smart 15.000 → 12.000/giorno distribuito nella giornata; tetto globale 24.000/giorno per i lookup smart (il feed WS delle uscite mantiene il suo tetto di 9.000 e poi passa al polling DexScreener); batch JSON-RPC conteggiati per chiamata nel limitatore, intervallo 0,12 → 0,15 s. Uso atteso 10–18k crediti/giorno, massimo ~23k.
- Regole ombra v9 (`shadow_v9.py`, solo log): tracker "ghost" con 8 varianti di uscita (`ghost_live_baseline`, `tp2x_half_trail25`, `ladder_2x4x_trail18`, `profit_lock`, `time_stop_15m`, `be20_trail15`, `chandelier_3atr`, `momentum_wide_trail`), 3 ingressi ombra (`dip_after_pump`, `score_45_55`, `migration_dip`) e 7 filtri ombra (`vol_spike_2x`, `buy_pressure`, `sell_spike_avoid`, `liq_growth`, `holder_growth_pos`, `new_pair_6h`, `migrated_24h`). Tabella `shadow_ghosts` con PnL ipotetico per trade.

## Servizi disattivati sulla VM Oracle (si possono riaccendere)

- `open-vm-tools`, `ModemManager`, `apport`: `sudo systemctl enable --now NOME`
- Plugin Oracle Cloud Agent `oci-wlp`: resta disattivato. `gomon` è stato riattivato così OCI Monitoring pubblica di nuovo `oci_computeagent` CpuUtilization (serve per il controllo anti-inattività). Per disattivarlo: in `/etc/oracle-cloud-agent/agent.yml` imposta `gomon.disabled: true` e poi `sudo snap restart oracle-cloud-agent`.
- Non disattivare `snapd`, l'agente Oracle, l'updater o `runcommand`. Non disattivare `iscsid` se potresti attaccare un volume.

Gli aggiornamenti di sicurezza automatici restano accesi; il riavvio automatico è disattivato (`Unattended-Upgrade::Automatic-Reboot "false"`).

## Niente riavvii deliberati con posizioni aperte (2026-11-18, Europe/Rome)

Obiettivo: mentre ci sono posizioni paper aperte, il desk non deve essere riavviato di proposito.

- `needrestart` non riavvia più automaticamente i servizi `memedesk*` dopo un aggiornamento delle librerie (file `deploy/needrestart-memedesk.conf`, installato in `/etc/needrestart/conf.d/memedesk.conf`). È così che Ubuntu, il 18 novembre alle 07:07 (Europe/Rome), ha riavviato il desk dopo l'aggiornamento di `libc6`.
- Il riavvio automatico della VM dopo `unattended-upgrades` resta disattivato (`deploy/99memedesk-reboot` → `/etc/apt/apt.conf.d/99memedesk-reboot`, `Unattended-Upgrade::Automatic-Reboot "false"`).
- Il watchdog, se il desk sembra fermo ma ci sono posizioni aperte (o non riesce a leggerle), non riavvia e avvisa su Telegram.
- I deploy successivi devono usare `/usr/local/sbin/memedesk-safe-restart` (sorgente in `deploy/memedesk-safe-restart`). Se ci sono posizioni aperte, o se lo stato non è leggibile, lo script rifiuta. `--force` lo supera, ma è sconsigliato.
- `OOMScoreAdjust=-500` sul servizio (e, a caldo, sui processi già in esecuzione) fa sì che, in caso di poca memoria, il kernel preferisca chiudere altri processi prima del desk.

Limiti che non si possono togliere: un crash del processo, un OOM che colpisce comunque il desk, una manutenzione Oracle o un reboot della VM. In quei casi il desk è già fermo; `Restart=always` deve restare, così il servizio riparte da solo. Un kernel nuovo può restare in attesa: si riavvia la VM solo a mano, e solo quando non ci sono posizioni aperte.

Le regole di trading non cambiano.


## Rischio di recupero per inattività (Always Free)

Oracle può recuperare un'istanza Always Free se, in 7 giorni, il 95° percentile della CPU è sotto il 20% **e** l'utilizzo di rete è sotto il 20%. La soglia sulla memoria vale solo per le forme A1. Il desk è prevalentemente in attesa di rete tra un ciclo e l'altro, quindi senza un keepalive entrambe le soglie resterebbero sotto il 20%. Un ping quotidiano non basta.

La mitigazione è attiva: `memedesk-keepalive.service` tiene un carico continuo pari a circa il 28% delle vCPU visibili (ciclo busy/sleep su un solo core, a priorità minima) quando il desk è fermo. È abbastanza sopra il 20% (P95) e dentro la fascia 25-30%. Sulla forma E2.1.Micro la metrica `CpuUtilization` è una percentuale del tempo totale del guest (come `mpstat` 100-%idle), non del baseline del 12,5% di un OCPU. La forma non è un burstable Flex ufficiale, ma dopo alcuni minuti sopra il baseline l'hypervisor limita l'allocazione al 12,5%. Per questo il keepalive:

- usa scatti a orologio (non insegue il 28% di CPU effettiva, altrimenti girerebbe al 100% del tempo sotto il tetto);
- è pinnato su una sola vCPU e si mette in pausa quando un processo di `memedesk.service` è in esecuzione, così il desk non deve spartire il subcore.

In misura, `oci_computeagent` (plugin `gomon`) resta intorno al 40-50%, ben sopra il 20%. La serie hypervisor `oci_vmi_resource_utilization`, dopo l'esaurimento dell'extra, si ferma a circa 12,5%. I cicli puliti del desk non sono diventati più lenti. Le regole di trading non cambiano. Nessuna routine di Grok Bot.


## Tentativi automatici per una A1 Always Free (2026-11-17, Europe/Rome)

Un timer systemd su questa E2.1.Micro riprova ogni 5 minuti (con un jitter fino a 90 s) a creare un'istanza Always Free `VM.Standard.A1.Flex` (2 OCPU, 12 GB, Ubuntu 24.04 aarch64, boot da 50 GB, IPv4 pubblico) in `eu-milan-1`, sulla VCN/subnet già usata da questa VM e con la stessa chiave SSH (`~/.ssh/oci_memedesk`).

Non sposta il desk. Quando l'istanza è pronta invia un messaggio Telegram con l'IP pubblico tramite `@sasowlbot` e disattiva il proprio timer. "Out of host capacity" è la risposta attesa a Milano e viene ritentata. Un 429 fa fare backoff (15 min, poi il doppio fino a 2 h). Una risposta che sembra un limite a pagamento o un upgrade ferma il timer e avvisa; il tenancy non viene mai passato a Pay As You Go. Lo script resta entro 2 OCPU / 12 GB / 50 GB anche se il Free Trial mostra quote A1 più alte: quelle quote extra non vengono usate, così il lancio resta nell'allowance Always Free.

Implementazione: uno script Python piccolo che firma le API REST di OCI con la libreria standard e `python3-cryptography` (già presente). La CLI OCI non è installata su questa VM da 1 GB. Le credenziali stanno solo sulla VM in `/etc/memedesk/oci/` (mode 700/600). Il codice è in `a1grab/`.

Stop:

```bash
sudo systemctl disable --now memedesk-a1grab.timer
```

Log: `journalctl -u memedesk-a1grab.service -n 50 --no-pager` oppure `sudo tail -50 /var/lib/memedesk/a1grab.log`.

## Migrazione verso un'altra VM

La copia live dello stato è sulla VM di origine, non su un computer di sviluppo.

1. Installa sulla destinazione le stesse unità e lo stesso `EnvironmentFile`.
2. Sulla sorgente: `sudo systemctl disable --now memedesk-watchdog.timer memedesk`, poi una copia coerente del database:

   ```bash
   sudo -u memedesk python3 -c "import sqlite3;s=sqlite3.connect('/opt/memedesk/desk.sqlite');d=sqlite3.connect('/tmp/desk.sqlite');s.backup(d)"
   ```

   Copia `/tmp/desk.sqlite` e, se servono, `events.jsonl`, `*_state.json`, `summary.txt` e i log `*.out` in `/opt/memedesk` sulla destinazione.
3. Avvia il servizio sulla destinazione, verifica che i cicli partano e che la dashboard si aggiorni, poi spegni in modo definitivo la sorgente.

Non avviare una seconda copia del desk sullo stesso stato.
