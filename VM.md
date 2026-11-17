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
