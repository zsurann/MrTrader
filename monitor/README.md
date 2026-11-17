# memedesk-monitor

External 24/7 watchdog for the PAPER MemeDesk on the Oracle Always Free VM.
It runs as a Cloudflare Worker Cron Trigger so it does not use Grok Bot minutes
and still fires if the whole VM is unreachable. The VM's own
`memedesk-watchdog.timer` already restarts the desk when the machine is up.

- Worker: `memedesk-monitor` (separate from `memedesk-dashboard`; the dashboard is untouched)
- Schedule: `*/5 * * * *`
- Check: latest `cycles.ts` / `desk_status` heartbeat via the Supabase anon key (SELECT-only RLS)
- Down: no new cycle for >15 minutes → Telegram `@sasowlbot`
  `⚠️ MemeDesk: nessun ciclo da X minuti — VM o desk giù`
  plus restart hint `Oracle Cloud → Compute → istanze (Milano) → memedesk-vm` (never Terminate)
  at most once per hour while down (state in KV `memedesk-monitor`)
- Recovery: one `✅ MemeDesk di nuovo online`
- No cloud-provider credentials, no trading-rule changes, free Workers plan only
  (this is cron #2 of 5 on the account; `turni-push` already uses one)

Secrets are Worker secrets, never committed: `SUPABASE_ANON_KEY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `MONITOR_TEST_SECRET`.
`SUPABASE_URL` is a plain var.

After `wrangler login` (or `CLOUDFLARE_API_TOKEN`):

    ./put-secrets.sh

Protected test path (Bearer token only, no query string):

    curl -sS https://memedesk-monitor.zsuran139.workers.dev/run?mode=status \
      -H "Authorization: Bearer $MONITOR_TEST_SECRET"
    # mode=test-alert  → one Telegram message labelled "test monitor"
    # mode=check       → same logic as the cron
