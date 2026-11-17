#!/bin/bash
# Pipe Telegram/Supabase secrets into the deployed memedesk-monitor Worker.
# Never prints secret values. Requires `wrangler login` or CLOUDFLARE_API_TOKEN.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SECDIR="${MEMEDESK_SECRET_DIR:-/tmp/memedesk-monitor-secrets}"
umask 077
mkdir -p "$SECDIR"
chmod 700 "$SECDIR"

need() { [[ -s "$SECDIR/$1" ]]; }

VM_HOST="${MEMEDESK_VM:?set MEMEDESK_VM=ubuntu@host}"
SSH_KEY="${MEMEDESK_SSH_KEY:-$HOME/.ssh/oci_memedesk}"

if ! need TELEGRAM_BOT_TOKEN || ! need TELEGRAM_CHAT_ID; then
  echo "Fetching Telegram keys from the Oracle VM (values not printed)..."
  ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 "$VM_HOST" 'bash -s' << 'REMOTE'
set -e
umask 077
sudo bash -c '
  set -e
  umask 077
  awk -F= "/^TELEGRAM_BOT_TOKEN=/{print substr(\$0, index(\$0,\"=\")+1)}" /etc/memedesk/memedesk.env | tr -d "\r" > /tmp/md_tok
  awk -F= "/^TELEGRAM_CHAT_ID=/{print substr(\$0, index(\$0,\"=\")+1)}" /etc/memedesk/memedesk.env | tr -d "\r" > /tmp/md_chat
  chmod 600 /tmp/md_tok /tmp/md_chat
  chown ubuntu:ubuntu /tmp/md_tok /tmp/md_chat
'
REMOTE
  scp -i "$SSH_KEY" -o BatchMode=yes -q "$VM_HOST:/tmp/md_tok" "$SECDIR/TELEGRAM_BOT_TOKEN"
  scp -i "$SSH_KEY" -o BatchMode=yes -q "$VM_HOST:/tmp/md_chat" "$SECDIR/TELEGRAM_CHAT_ID"
  ssh -i "$SSH_KEY" -o BatchMode=yes "$VM_HOST" 'rm -f /tmp/md_tok /tmp/md_chat'
fi

if ! need SUPABASE_ANON_KEY; then
  python3 - "$SECDIR" << 'PY'
import sys
from pathlib import Path
sec = Path(sys.argv[1])
anon = None
for line in Path("/workspace/memedesk/.env.supabase").read_text().splitlines():
    if line.startswith("SUPABASE_ANON_KEY="):
        anon = line.split("=", 1)[1].strip()
        break
if not anon:
    raise SystemExit("SUPABASE_ANON_KEY missing from /workspace/memedesk/.env.supabase")
(sec / "SUPABASE_ANON_KEY").write_text(anon)
(sec / "SUPABASE_ANON_KEY").chmod(0o600)
PY
fi

if ! need MONITOR_TEST_SECRET; then
  python3 -c 'import secrets,pathlib,sys; p=pathlib.Path(sys.argv[1])/"MONITOR_TEST_SECRET"; p.write_text(secrets.token_urlsafe(24)); p.chmod(0o600)' "$SECDIR"
fi

python3 - "$SECDIR" << 'PY'
from pathlib import Path
import sys
sec = Path(sys.argv[1])
for name in ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","SUPABASE_ANON_KEY","MONITOR_TEST_SECRET"):
    p = sec / name
    t = p.read_text().strip()
    p.write_text(t)
    p.chmod(0o600)
    print(f"{name} len={len(t)} ok={bool(t)}")
PY

ENVFILE="$SECDIR/wrangler-secrets.env"
python3 - "$SECDIR" "$ENVFILE" << 'PY'
from pathlib import Path
import sys
sec, out = Path(sys.argv[1]), Path(sys.argv[2])
keys = ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","SUPABASE_ANON_KEY","MONITOR_TEST_SECRET")
lines = []
for k in keys:
    v = (sec / k).read_text().strip()
    if any(c in v for c in "\n\r"):
        raise SystemExit(f"{k} has unexpected newlines")
    lines.append(f"{k}={v}\n")
out.write_text("".join(lines))
out.chmod(0o600)
print("secrets_env_keys", 4)
PY

echo "Uploading secrets to Worker memedesk-monitor (values not printed)..."
npx --yes wrangler@4.163.0 secret bulk "$ENVFILE" --config "$ROOT/wrangler.jsonc" --name memedesk-monitor
echo "Done. Secret names: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SUPABASE_ANON_KEY, MONITOR_TEST_SECRET."
