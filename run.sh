#!/bin/bash
# Supervisor: keeps the paper desk, the Supabase realtime syncer AND the smart-wallet builder running. PAPER ONLY.
# All three are independent processes: if Supabase/sync or the smart-wallet builder dies, trading is never blocked.
cd "$(cd "$(dirname "$0")" && pwd)"
if [ -f RUNNING_ON_VM ] || [ -f RUNNING_ON_GCP ]; then
  echo "Refusing to start: this copy is not the live desk (see RUNNING_ON_VM)." >&2
  exit 1
fi
echo $$ > supervisor.pid
ts() { date '+%F %T %Z'; }

(   # syncer loop (mirror to Supabase for the live dashboard)
  while true; do
    python3 -u syncer.py >> sync.out 2>&1 &
    echo $! > syncer.pid
    wait $!; rc=$?
    echo "$(ts) syncer exited (rc=$rc), restarting in 10s" >> sync.out
    sleep 10
  done
) &

(   # smart-wallet builder + shadow exit rule (free on-chain data, background, log-only)
  # Lower CPU/IO priority so the desk cycle stays responsive on the small Always Free VM.
  while true; do
    nice -n 10 ionice -c2 -n 7 python3 -u smartwallets.py >> smart.out 2>&1 &
    echo $! > smart.pid
    wait $!; rc=$?
    echo "$(ts) smartwallets exited (rc=$rc), restarting in 30s" >> smart.out
    sleep 30
  done
) &

trap 'kill $(cat desk.pid syncer.pid smart.pid 2>/dev/null) 2>/dev/null; pkill -P $$; exit 0' TERM INT
while true; do
  python3 -u desk.py >> desk.out 2>&1 &
  echo $! > desk.pid
  wait $!; rc=$?          # capture BEFORE any other command ($? was being clobbered by $(date))
  echo "$(ts) desk exited (rc=$rc), restarting in 20s" >> desk.out
  sleep 20
done
