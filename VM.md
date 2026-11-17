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

- `memedesk.service`: avvia `run.sh` (desk + syncer + smartwallets), `Restart=always`, abilitato al boot. Limiti memoria e hardening (`MemoryHigh=700M` / `MemoryMax=850M`, `ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, `PrivateTmp`).
- `memedesk-watchdog.timer` → `memedesk-watchdog.service`: ogni 5 minuti esegue `vm_watchdog.py`. Se non c'è un ciclo da più di 10 minuti, riavvia il servizio e manda al massimo un avviso Telegram all'ora. Controlla anche che il bot Telegram risponda e svuota la coda.
- `memedesk-keepalive.service` (script `keepalive.py`): carico CPU a priorità minima per il recupero Always Free da inattività. `Nice=19`, `CPUSchedulingPolicy=idle`, `IOSchedulingClass=idle`, `CPUAffinity=0`, `MemoryMax=20M`, `Restart=always`, abilitato al boot. Si ferma da solo mentre il desk è in esecuzione. Per spegnerlo, senza toccare il desk:

  ```bash
  sudo systemctl disable --now memedesk-keepalive
  ```

Altri file in `deploy/`: rotazione log, tetto di journald, sysctl (swappiness), unità keepalive.

Un controllo esterno (Cloudflare Worker cron, piano Free) avvisa su Telegram se la VM intera è irraggiungibile per più di 15 minuti. Non usa minuti di Grok Bot. Sorgente in `monitor/`.

## Comandi sulla VM

```bash
sudo systemctl restart memedesk
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

## Servizi disattivati sulla VM Oracle (si possono riaccendere)

- `open-vm-tools`, `ModemManager`, `apport`: `sudo systemctl enable --now NOME`
- Plugin Oracle Cloud Agent `oci-wlp`: resta disattivato. `gomon` è stato riattivato così OCI Monitoring pubblica di nuovo `oci_computeagent` CpuUtilization (serve per il controllo anti-inattività). Per disattivarlo: in `/etc/oracle-cloud-agent/agent.yml` imposta `gomon.disabled: true` e poi `sudo snap restart oracle-cloud-agent`.
- Non disattivare `snapd`, l'agente Oracle, l'updater o `runcommand`. Non disattivare `iscsid` se potresti attaccare un volume.

Gli aggiornamenti di sicurezza automatici restano accesi; il riavvio automatico è disattivato (`Unattended-Upgrade::Automatic-Reboot "false"`).

## Rischio di recupero per inattività (Always Free)

Oracle può recuperare un'istanza Always Free se, in 7 giorni, il 95° percentile della CPU è sotto il 20% **e** l'utilizzo di rete è sotto il 20%. La soglia sulla memoria vale solo per le forme A1. Il desk è prevalentemente in attesa di rete tra un ciclo e l'altro, quindi senza un keepalive entrambe le soglie resterebbero sotto il 20%. Un ping quotidiano non basta.

La mitigazione è attiva: `memedesk-keepalive.service` tiene un carico continuo pari a circa il 28% delle vCPU visibili (ciclo busy/sleep su un solo core, a priorità minima) quando il desk è fermo. È abbastanza sopra il 20% (P95) e dentro la fascia 25-30%. Sulla forma E2.1.Micro la metrica `CpuUtilization` è una percentuale del tempo totale del guest (come `mpstat` 100-%idle), non del baseline del 12,5% di un OCPU. La forma non è un burstable Flex ufficiale, ma dopo alcuni minuti sopra il baseline l'hypervisor limita l'allocazione al 12,5%. Per questo il keepalive:

- usa scatti a orologio (non insegue il 28% di CPU effettiva, altrimenti girerebbe al 100% del tempo sotto il tetto);
- è pinnato su una sola vCPU e si mette in pausa quando un processo di `memedesk.service` è in esecuzione, così il desk non deve spartire il subcore.

In misura, `oci_computeagent` (plugin `gomon`) resta intorno al 40-50%, ben sopra il 20%. La serie hypervisor `oci_vmi_resource_utilization`, dopo l'esaurimento dell'extra, si ferma a circa 12,5%. I cicli puliti del desk non sono diventati più lenti. Le regole di trading non cambiano. Nessuna routine di Grok Bot.

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
