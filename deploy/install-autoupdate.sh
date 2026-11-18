#!/bin/bash
# One-time install of the MemeDesk auto-update timer. Run as root from the checkout in /var/lib/memedesk-src.
# Refuses if the root files in /opt/memedesk differ from the checkout (local VM changes would be overwritten),
# unless FORCE=1.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/memedesk
diffs=$(cd "$SRC" && git ls-files | grep -v / | grep -vxE '\.env\.example|\.gitignore' | while read -r f; do cmp -s "$f" "$APP/$f" || echo "$f"; done)
if [ -n "$diffs" ] && [ "${FORCE:-0}" != 1 ]; then
  echo "File diversi tra /opt/memedesk e GitHub (verrebbero sovrascritti):"; echo "$diffs"
  echo "Niente installato. Rilancia con FORCE=1 se va bene."; exit 2
fi
install -m 755 "$SRC/deploy/memedesk-autoupdate" /usr/local/sbin/memedesk-autoupdate
install -m 755 "$SRC/deploy/memedesk-safe-restart" /usr/local/sbin/memedesk-safe-restart
install -m 644 "$SRC/deploy/memedesk-autoupdate.service" "$SRC/deploy/memedesk-autoupdate.timer" /etc/systemd/system/
[ -n "$diffs" ] || git -C "$SRC" rev-parse HEAD > "$APP/.deployed_rev"
systemctl daemon-reload
systemctl enable --now memedesk-autoupdate.timer
echo "OK: aggiornamento automatico attivo (ogni 10 minuti)."
