# Map: US stock day-trading paper desk  `wayfinder:map`
(local-markdown tracker)

## Destination
A second desk, separate from MrTrader, that runs the day-trader-skill pipeline (scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> paper fill -> alerts) by itself on the GCP VM during NYSE hours, paper money only, visible on the phone dashboard.

## Notes
Tools: `.claude/skills/trader-*/*.py` (stdlib, file handoffs in `trader/`). Safety rule kept: no broker, no real orders; paper fills are simulated locally. User is a beginner: guide step by step, Italian.

## Decisions so far
- Yahoo reachable from the VM (2026-11-15): quote TSLA OK, Time Machine read 59 full sessions of 5m bars. Not checked: the scanner's bulk spark requests (thousands of symbols) under rate limits.

## Tickets (frontier)
- [ ] **Account rules** `wayfinder:grilling` — paper budget, max loss per trade, max trades/day, setups (orb only, or daily setups too).
- [ ] **Paper fill and exit model** `wayfinder:grilling` — fill at next quote + slippage? exits by stop/target/flat-by from alerts.py check every minute. 

## Not yet specified
- Time Machine gate threshold for stocks (its verdict, or target-share cutoff).
- News gate: skip a symbol with earnings/halt news today?
- Dashboard: same page as MrTrader (second tab) or separate.

## Out of scope
- Live orders through Alpaca / any broker (paper first, same as MrTrader).
- trader-pelosi, trader-analyst as trade triggers (research tools, not intraday signals).
