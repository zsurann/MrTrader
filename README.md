# MrTrader — Solana memecoin paper desk

Scout → Filter → Analyst → Risk → Executor, Logger records everything. Paper only.

```bash
python3 desk.py          # loop forever (overnight)
python3 desk.py --once   # one cycle
python3 -m unittest test_desk
```

Dashboard (GUI): `python3 dashboard.py`, then open http://localhost:8050 — Start/Stop buttons, equity, positions, trades, kills.

Overnight without GUI: `nohup python3 desk.py > desk.out 2>&1 &`

Outputs: `logs/kill.jsonl` (every kill + reason + facts), `logs/trade.jsonl`, `logs/events.jsonl`, `state.json` (paper book, resumes on restart).
Optional `smart_wallets.txt`: one address per line. Without it the smart-wallet factor scores 0 (max score 85).
Thresholds in `CFG` at top of `desk.py`. Open questions: `docs/wayfinder-map.md`.
