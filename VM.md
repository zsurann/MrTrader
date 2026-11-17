# MemeDesk su una VM (solo paper)

Questo file descrive come è installato il desk su una macchina virtuale. Non contiene indirizzi IP, chiavi SSH o segreti.

## Layout

- Codice e stato: `/opt/memedesk` (utente di sistema `memedesk`)
- Segreti: `/etc/memedesk/memedesk.env` (root, modalità 600), caricati da systemd con `EnvironmentFile`. Variabili: vedi `.env.example`. Non ci sono file `.env` nella directory dell'app: `syncer.py` e `notify.py` usano l'ambiente se quei file mancano.
- Python: `/usr/bin/python3` (stdlib, senza venv)
- Fuso orario consigliato: Europe/Rome

## Unità systemd (copie in `deploy/`)

- `memedesk.service`: avvia `run.sh` (desk + syncer + smartwallets), `Restart=always`, abilitato al boot. Limiti memoria e hardening (`MemoryHigh` / `MemoryMax`, `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`).
- `memedesk-watchdog.timer` → `memedesk-watchdog.service`: ogni 5 minuti esegue `vm_watchdog.py`. Se non c'è un ciclo da più di 10 minuti, riavvia il servizio e manda al massimo un avviso Telegram all'ora. Controlla anche che il bot Telegram risponda e svuota la coda.

Altri file in `deploy/`: rotazione log, tetto di journald, sysctl (swappiness).

## Comandi sulla VM

```bash
sudo systemctl restart memedesk
systemctl status memedesk --no-pager
tail -f /opt/memedesk/desk.out
grep " cycle " /opt/memedesk/desk.out | tail -1
sudo systemctl stop memedesk
# Se lo stop deve durare, ferma anche il watchdog, altrimenti dopo 10 minuti riparte:
sudo systemctl disable --now memedesk memedesk-watchdog.timer
```

Da un altro computer, con host e chiave impostati da te (nessun valore predefinito in questo repo):

```bash
VM=user@host KEY=/path/to/ssh_key ./vm_status.sh
```

## Migrazione verso un'altra VM

La copia live dello stato è sulla VM di origine, non su un computer di sviluppo.

1. Installa sulle destinazione le stesse unità e lo stesso `EnvironmentFile`.
2. Sulla sorgente: `sudo systemctl disable --now memedesk-watchdog.timer memedesk`, poi una copia coerente del database:

   ```bash
   sudo -u memedesk python3 -c "import sqlite3;s=sqlite3.connect('/opt/memedesk/desk.sqlite');d=sqlite3.connect('/tmp/desk.sqlite');s.backup(d)"
   ```

   Copia `/tmp/desk.sqlite` e, se servono, `events.jsonl`, `*_state.json`, `summary.txt` e i log `*.out` in `/opt/memedesk` sulla destinazione.
3. Avvia il servizio sulla destinazione, verifica che i cicli partano e che la dashboard si aggiorni, poi spegni in modo definitivo la sorgente.

Non avviare una seconda copia del desk sullo stesso stato. Se usi un checkout di sviluppo, lascia un marker locale `RUNNING_ON_VM` (è nel `.gitignore`) così `run.sh` rifiuta l'avvio.
