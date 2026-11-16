# Map: US stock day-trading paper desk  `wayfinder:map`
(local-markdown tracker)

## Destination
A second desk, separate from MrTrader, that runs the day-trader-skill pipeline (scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> paper fill -> alerts) by itself on the GCP VM during NYSE hours, paper money only, visible on the phone dashboard.

## Notes
Tools: `.claude/skills/trader-*/*.py` (stdlib, file handoffs in `trader/`). Safety rule kept: no broker, no real orders; paper fills are simulated locally. User is a beginner: guide step by step, Italian.

## Decisions so far
- Yahoo reachable from the VM (2026-11-15): quote TSLA OK, Time Machine read 59 full sessions of 5m bars. Not checked: the scanner's bulk spark requests (thousands of symbols) under rate limits.

- Account rules (user, 2026-11-15): $10,000 paper, max loss $50/trade (0.5%), daily loss $150, max 3 trades/day, setup = opening-range breakout only (15-min range, stop at range midpoint, target 2R, flat by 15:55 ET).
- Built stocks.py (decisions taken by agent, user is a beginner): every 10 min 9:50-15:30 ET run `scan.py orb`, take hits that fired in the last 15 min, closest to the range first; news logged (no gate); `exit.py plan`; Time Machine gate avg_R > 0 (expected R incl. flat-by closes); quote -> `size.py` -> `check.py` PASS; paper fill at delayed Yahoo quote via `ticket.py fill`; exits every minute on quote: stop, target, 15:55. Phone pushes on the MrTrader ntfy topic. Dashboard card. systemd unit via deploy/stocks-service.sh. End-to-end test on the skill pack's reel day (TSLA 2026-10-19).

- Efficiency 1 (2026-11-15): the scan runs in a background thread; exits keep checking every minute; the lock is held one candidate at a time. Scan logs its duration.
- Full-universe scan speed (2026-11-15): `scan.py daily` over 5,932 stocks took 2m28s from the VM, no 429 seen. With the background thread and a 10-min cadence that is fine: universe left untrimmed (a liquid-only list would gain ~2 min freshness, not worth a second list to maintain).

- Exploration mode (2026-11-16, day 1: 2 trades, 2 small stop losses, most hits skipped by the Time Machine): max 10 trades/day, Time Machine gate avg_R > -0.05, daily loss $300; config.json re-applied on every start.

## Tickets (frontier)
- [ ] **First week review** `wayfinder:grilling` — trades, P&L, Time Machine skips; is avg_R > 0 too loose/tight; does a full-universe scan finish fast enough under Yahoo limits.

## Not yet specified
- News gate: skip a symbol with earnings/halt news today? (logged only for now)


## Out of scope
- Live orders through Alpaca / any broker (paper first, same as MrTrader).
- trader-pelosi, trader-analyst as trade triggers (research tools, not intraday signals).
