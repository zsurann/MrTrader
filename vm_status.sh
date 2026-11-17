#!/bin/bash
# Prints MemeDesk status on the VM over SSH (PAPER ONLY). No secrets.
# Usage: VM=user@host KEY=/path/to/ssh_key ./vm_status.sh
set -euo pipefail
if [ -z "${VM:-}" ] || [ -z "${KEY:-}" ]; then
  echo "Set VM=user@host and KEY=/path/to/ssh_key" >&2
  exit 1
fi
ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 "$VM" bash -s <<'REMOTE'
echo "== memedesk.service: $(systemctl is-active memedesk) (since $(systemctl show memedesk -p ActiveEnterTimestamp --value))"
echo "== watchdog timer:   $(systemctl is-active memedesk-watchdog.timer)"
echo "== last cycles:"
sudo grep " cycle " /opt/memedesk/desk.out | tail -5
echo "== processes:"
ps -u memedesk -o pid,rss,pcpu,etime,cmd --no-headers
echo "== memory:"
free -m | sed -n 1,3p
echo "== telegram:"
if sudo test -f /opt/memedesk/telegram_status.json; then sudo cat /opt/memedesk/telegram_status.json; echo; fi
q=/opt/memedesk/telegram_queue.jsonl
if sudo test -f "$q"; then echo "queue_lines $(sudo wc -l < "$q")"; else echo "queue_lines 0"; fi
echo "== telegram.log (last):"
sudo tail -n 2 /opt/memedesk/telegram.log 2>/dev/null || true
echo "== recent errors (desk.out/sync.out):"
sudo tail -n 300 /opt/memedesk/desk.out /opt/memedesk/sync.out | grep -iE "traceback|error" | tail -3 || true
REMOTE
