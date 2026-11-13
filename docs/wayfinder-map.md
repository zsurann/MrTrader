# Map: Solana memecoin desk  `wayfinder:map`
(local-markdown tracker — GitHub tracker unreachable this session)

## Destination
Paper desk running overnight on real on-chain data, 6 agents with hard handoffs, every kill logged. Go/no-go for live decided from paper results.

## Notes
Stdlib Python only. Paper first. Ponytail: smallest complete change.

## Decisions so far
- Data sources: DexScreener (pairs, price, vol, txns), RugCheck report (mint/freeze, LP lock, holders, creator, rug risks), trench.bot (bundle %). Missing safety data = kill.
- Analyst weights: momentum 20, buyers 15, buy/sell 20, holder growth 15, liq/mcap 15, smart wallets 15. Pass ≥70.
- Risk: stop −6% set before fill; trail arms at +12%, trails 6% under peak. 1% slippage each side.
- Executor: 8% of equity per trade, asserted. One entry per mint ever.

## Tickets (frontier)
- [ ] **Unique-buyer source** `wayfinder:research` — DexScreener gives buy-txn count only. Helius/Birdeye (keyed) for true unique buyers?
- [ ] **Smart wallet list** `wayfinder:grilling` — which wallets count as smart; how to source/refresh `smart_wallets.txt`.
- [ ] **Trail distance** `wayfinder:grilling` — 6% under peak assumed; user only specified arm point.
- [ ] **Validate RugCheck/trench field names on live data** `wayfinder:task` — first overnight run; check `logs/events.jsonl` for http_error and kill reasons dominated by "unavailable".

## Not yet specified
- Live execution (Jupiter swaps, wallet key handling, priority fees) — after paper results.
- Gap risk: stops checked every 20s, memecoins can gap through.

## Out of scope
- Non-Solana chains.
