# Map: Solana memecoin desk  `wayfinder:map`
(local-markdown tracker — GitHub tracker unreachable this session)

## Destination
Paper desk running overnight on real on-chain data, 6 agents with hard handoffs, every kill logged. Go/no-go for live decided from paper results.

## Notes
Stdlib Python only. Paper first. Ponytail: smallest complete change.

## Decisions so far
- Data sources: DexScreener (pairs, price, vol, txns), RugCheck report (mint/freeze, LP lock, holders, creator, rug risks), ~~trench.bot~~. Missing safety data = kill.
- Bundle: trench.bot never answered (5/5 timeouts). Replaced by RugCheck insider-held % > 10% = kill. User choice.
- Live run 1: DexScreener OK; added GeckoTerminal new/trending pools to Scout (only 1 candidate before).
- Analyst weights: momentum 20, buyers 15, buy/sell 20, holder growth 15, liq/mcap 15, smart wallets 15. Pass ≥70.
- Risk: stop −6% set before fill; trail arms at +12%, trails 6% under peak. 1% slippage each side.
- Executor: 8% of equity per trade, asserted. One entry per mint ever.

- Hosting: Google Cloud e2-micro (free tier), systemd service, desk auto-resumes after reboot. Phone access via Tailscale serve (private, no public port). User choice.

- Deployed on GCP, running overnight from 2026-11-13 (paper). Scout widened to Gecko busiest/trending pools.

- Smart wallets: user delegated choice; GMGN unreachable from agent and addresses must not be invented -> desk learns them: top non-insider holders of coins it saw that went 2x+ within 72h, 2+ wins = smart (smart_wallets_auto.txt). Manual paste in dashboard still possible; desk checks on-chain holdings via Solana RPC (getTokenAccountsByOwner). User choice.

- First ~6h (2026-11-13): 1 trade, WANTED score 71.9, stopped -8.15% after 4 min (stop slipped past -6%). Top kills: 1h vol 109, liq 82 (mostly dead $0 pairs), score 68, top10 48, rug history 26. Fixes: dead pairs skip RugCheck; price check every 10s. User decision: 1h vol min 50k -> 25k, add smart wallets.

- Night 1 review (2026-11-14): 7 trades, 2W/5L, -2.5%. Two -15% losses were filled and stopped in the same minute = stale signal price; three -8% = -6% stop + 2% costs. Findings + fixes: (1) stops were unwatched during slow scans -> monitor runs between every slow call; (2) Risk rechecks a fresh price/pair right before entry, kills if price fell >3% or rules now fail; (3) paper costs now fee 0.5% + price impact usd/(liq/2), size capped so impact <= 1.5% (thin pool => smaller than 8%); (4) GeckoTerminal paced to ~9 calls/min (keyless limit is 10-30/min, likely source of the 429s).
- Unique-buyer source: GeckoTerminal pool `transactions.h1.buyers` (unique wallets, free, no key) replaces the buy-txn proxy; 300/h = full marks.
- Outcome tracking: every scored coin gets one row in logs/outcome.jsonl (score, parts, 1h return, hi, lo); dashboard shows avg 1h return per score band. Tuning weights/stop waits for this data.

- Day-trader-skill applied (2026-11-14, user chose all 4 that fit memecoins; scanner/news/analyst/pelosi/broker/entry are US-stock tools, out of scope): (1) Time Machine from trader-timemachine: before entry, 10,000 replays of the next 240 min from the coin's own GeckoTerminal 1m bars (ratio-to-previous-close bars, stop counted first when both in one bar); skip if stop-first > 50%. (2) Risk limits from trader-risk: no new buys after -3% equity in the UTC day, max 5 open, max 20 buys/day; selling never blocked. (3) Phone pushes from trader-alerts via ntfy.sh on every buy/sell, random topic shown in dashboard. (4) Time exit from trader-exit: sell after 4h if trail never armed.

## Tickets (frontier)
- [ ] **Stop width and momentum chasing** `wayfinder:grilling` — -6% sits inside these coins' 1-minute noise, and the score pays most for coins already +40% in 1h. Decide with the outcome table (needs ~100 scored coins). Blocked by: Review results after 2-3 days.
- [ ] **Review results after 2-3 days** `wayfinder:grilling` — with smart wallets + vol 25k: trade count, win rate, P&L.

- [ ] **Trail distance** `wayfinder:grilling` — 6% under peak assumed; user only specified arm point.
- [x] **Validate field names on live data** `wayfinder:task` — DexScreener, RugCheck OK on live run 2; GeckoTerminal OK but 429 on bursts (throttled). RugCheck `insider` field confirmed live (run 3). Full pipeline reached Analyst (FOOD scored 55.4).

## Not yet specified
- Real bundle detection: Cabal-Hunter (free 250 scans/month, no key), Mobula, MadeOnSol free tier — vendor claims, unverified. Insider % from RugCheck stays until one is tested.
- Jupiter quote API for exact price impact (needed for live anyway; free lite-api host, v1 marked superseded by Swap V2).
- Live execution (Jupiter swaps, wallet key handling, priority fees) — after paper results.
- Gap risk: stops checked every 20s, memecoins can gap through.

## Out of scope
- Non-Solana chains.
