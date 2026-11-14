import json, os, tempfile, time, unittest
import desk

NOW = 1_800_000_000


def pair(**kw):
    p = {"chainId": "solana", "baseToken": {"address": "M1", "symbol": "TST"}, "priceUsd": "0.01",
         "pairCreatedAt": (NOW - 5 * 3600) * 1000, "marketCap": 2_000_000, "liquidity": {"usd": 300_000},
         "volume": {"h1": 400_000}, "priceChange": {"h1": 35, "m5": 8}, "txns": {"h1": {"buys": 1500, "sells": 600}}}
    p.update(kw)
    return p


RC = {"mintAuthority": None, "freezeAuthority": None, "creator": "DEV", "totalHolders": 3000,
      "markets": [{"lp": {"lpLockedPct": 100}}], "knownAccounts": {"POOL": {"type": "AMM"}}, "risks": [],
      "topHolders": [{"address": "POOL", "pct": 40}] + [{"address": f"H{i}", "owner": f"W{i}", "pct": 2, "insider": i == 0} for i in range(10)]}


class T(unittest.TestCase):
    def setUp(self):
        desk.LOGS = tempfile.mkdtemp()

    def cand(self, **kw):
        return desk.Scout.select([pair(**kw)], NOW)

    def test_scout_window(self):
        self.assertEqual(len(self.cand()), 1)
        self.assertEqual(self.cand(pairCreatedAt=(NOW - 600) * 1000), [])           # 10 min old
        self.assertEqual(self.cand(pairCreatedAt=(NOW - 80 * 3600) * 1000), [])     # 80h old
        self.assertEqual(self.cand(marketCap=100_000), [])
        self.assertEqual(self.cand(marketCap=25_000_000), [])

    def test_filter_clean_passes_pool_excluded(self):
        r, fx = desk.Filter.check(self.cand()[0], RC)
        self.assertEqual(r, [])
        self.assertAlmostEqual(fx["top10_pct"], 20)

    def test_filter_kills(self):
        c = self.cand()[0]
        cases = [({**RC, "mintAuthority": "X"}, "mint"), ({**RC, "freezeAuthority": "X"}, "freeze"),
                 ({**RC, "markets": [{"lp": {"lpLockedPct": 20}}]}, "LP"),
                 ({**RC, "topHolders": [{"address": "A", "pct": 30, "insider": False}]}, "top10"),
                 ({**RC, "topHolders": [{"address": "A", "owner": "DEV", "pct": 5, "insider": False}]}, "dev"),
                 ({**RC, "risks": [{"name": "Creator history of rugged tokens"}]}, "rug"),
                 ({**RC, "topHolders": [{"address": f"I{i}", "pct": 2.2, "insider": True} for i in range(5)]}, "insiders"),
                 ({**RC, "topHolders": [{"address": "A", "pct": 1}]}, "insider data"), (None, "rugcheck")]
        for rc, word in cases:
            r, _ = desk.Filter.check(c, rc)
            self.assertTrue(any(word in x for x in r), (word, r))
        for kw, word in [({"liquidity": {"usd": 30_000}}, "liq"), ({"volume": {"h1": 10_000}}, "1h vol"),
                         ({"priceChange": {"h1": 95}}, "already")]:
            r, _ = desk.Filter.check(self.cand(**kw)[0], RC)
            self.assertTrue(any(word in x for x in r), (word, r))

    def test_analyst_threshold(self):
        c = {**self.cand()[0], "holders": 3000, "owners": ["W1", "W2", "W3"]}
        a = desk.Analyst()
        self.assertGreaterEqual(desk.Analyst.score(c, {"W1", "W2", "W3"})["smart"], 1)
        self.assertEqual(desk.Analyst.score(c, set())["smart"], 0)
        self.assertGreaterEqual(sum(a.W[k] * v for k, v in desk.Analyst.score(c, {"W1", "W2", "W3"}).items()), 70)
        weak = {**self.cand(priceChange={"h1": 2, "m5": 0}, txns={"h1": {"buys": 100, "sells": 120}})[0],
                "holders": 50, "owners": []}
        orig, desk.get = desk.get, lambda url, body=None: None           # GeckoTerminal down: falls back to txn count
        try:
            a.seen["M1"] = {"p": 0.01, "t": NOW, "w": []}
            self.assertIsNone(a.run(weak))
            self.assertLess(a.seen["M1"]["s"], 70)                       # first score kept for outcome tracking
        finally:
            desk.get = orig

    def test_risk_stop_and_trail(self):
        st = {"cash": 10_000, "pos": {}, "closed": []}
        o = desk.Risk().plan({"mint": "M1", "price": 1.0, "score": 80, "liq": 1_000_000}, 10_000, 10_000)
        self.assertEqual(o["usd"], 800)
        self.assertAlmostEqual(o["entry"], 1.0 * (1 + 0.005 + 800 / 500_000))   # fee + price impact
        self.assertAlmostEqual(o["stop"], o["entry"] * 0.94)
        ex = desk.Executor(st); ex.buy(o)
        p = st["pos"]["M1"]
        self.assertIsNone(desk.Risk.update(p, o["entry"] * 1.05))
        self.assertIsNone(desk.Risk.update(p, o["entry"] * 1.20))   # arms trail
        self.assertAlmostEqual(p["stop"], o["entry"] * 1.20 * 0.94)
        self.assertEqual(desk.Risk.update(p, o["entry"] * 1.12), "trail_stop")
        p2 = {**o, "peak": o["entry"]}
        self.assertEqual(desk.Risk.update(p2, o["entry"] * 0.93), "stop_loss")
        ex.sell("M1", o["entry"], "test")                              # flat price: lose only costs, both sides
        self.assertLess(st["cash"], 10_000)
        self.assertGreater(st["cash"], 10_000 - 800 * 0.02)

    def test_risk_sizes_down_in_thin_pool(self):
        o = desk.Risk().plan({"mint": "M1", "price": 1.0, "score": 80, "liq": 40_000}, 10_000, 10_000)
        self.assertAlmostEqual(o["usd"], 300)                           # 1.5% impact cap, not the 8% max
        self.assertAlmostEqual(o["entry"], 1.02)                        # 0.5% fee + 1.5% impact

    def test_risk_recheck(self):
        sig = {"mint": "M1", "symbol": "TST", "price": 0.01, "score": 80}
        orig = desk.get
        try:
            desk.get = lambda url, body=None: [pair(priceUsd="0.0101")]
            with_now = lambda: desk.Risk().recheck(sig, NOW)
            self.assertAlmostEqual(with_now()["price"], 0.0101)         # fresh price replaces the stale one
            self.assertEqual(with_now()["liq"], 300_000)
            desk.get = lambda url, body=None: [pair(priceUsd="0.0090")]
            self.assertIsNone(with_now())                               # -10% since signal: momentum broke
            desk.get = lambda url, body=None: [pair(priceChange={"h1": 250})]
            self.assertIsNone(with_now())                               # pumped past the +80% rule meanwhile
            desk.get = lambda url, body=None: None
            self.assertIsNone(with_now())
        finally:
            desk.get = orig

    def test_unique_buyers(self):
        c = {**self.cand()[0], "holders": 3000}
        self.assertEqual(desk.Analyst.score(c, set(), 150)["buyers"], 0.5)
        self.assertEqual(desk.Analyst.score(c, set(), None)["buyers"], 1.0)   # fallback: 1500 buy txns
        orig, desk.get = desk.get, lambda url, body=None: {"data": {"attributes": {"transactions": {"h1": {"buyers": 282}}}}}
        try:
            self.assertEqual(desk.unique_buyers("POOL"), 282)
            desk.get = lambda url, body=None: None
            self.assertIsNone(desk.unique_buyers("POOL"))
        finally:
            desk.get = orig

    def test_outcome_logged_after_1h(self):
        d = tempfile.mkdtemp(); orig_dir, desk.DIR = desk.DIR, d
        st = {"seen": {"A": {"p": 1.0, "t": NOW - 3700, "w": [], "s": 72.0, "parts": {"momentum": 1}, "sym": "AAA"},
                       "B": {"p": 1.0, "t": NOW - 60, "w": [], "s": 40.0, "parts": {}, "sym": "BBB"},
                       "C": {"p": 1.0, "t": NOW - 3700, "w": []}}}       # never scored: no outcome row
        orig_get, desk.get = desk.get, lambda url, body=None: [
            {"baseToken": {"address": m}, "priceUsd": "1.2", "liquidity": {"usd": 1}} for m in "ABC"]
        try:
            desk.learn_wallets(st, NOW); desk.learn_wallets(st, NOW)     # second pass must not duplicate
            rows = [json.loads(l) for l in open(os.path.join(desk.LOGS, "outcome.jsonl"))]
            self.assertEqual([(r["symbol"], r["score"], r["ret"]) for r in rows], [("AAA", 72.0, 0.2)])
        finally:
            desk.get, desk.DIR = orig_get, orig_dir

    def test_executor_rejects_oversize(self):
        st = {"cash": 10_000, "pos": {}, "closed": []}
        with self.assertRaises(AssertionError):
            desk.Executor(st).buy({"mint": "M", "usd": 2000, "entry": 1, "stop": 0.94, "liq": 1e6})

    def test_gecko_mints(self):
        resp = {"data": [{"relationships": {"base_token": {"data": {"id": "solana_ABC"}}}}, {"relationships": {}}]}
        self.assertEqual(desk.gecko_mints(resp), ["ABC"])
        self.assertEqual(desk.gecko_mints(None), [])

    def test_dashboard_reason_kind(self):
        import dashboard
        self.assertEqual(dashboard.reason_kind("1h vol $20,982 < $50,000"), "1h vol < $50,000")
        self.assertEqual(dashboard.reason_kind("score 55.4 < 70"), "score < 70")
        self.assertEqual(dashboard.reason_kind("mint authority not revoked"), "mint authority not revoked")
        self.assertEqual(dashboard.reason_kind("LP burned/locked 0% < 90%"), "LP burned/locked < 90%")

    def test_smart_wallets(self):
        self.assertTrue(desk.is_wallet("7Sbo9Re2ofWBmLSjG7QzGpkn AGuLnLZCrDYSUa8meNcH".replace(" ", "")))
        self.assertFalse(desk.is_wallet("ciao"))
        self.assertFalse(desk.is_wallet("0OIl" * 10))
        held = {"result": {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 5.0}}}}}}]}}
        empty = {"result": {"value": []}}
        orig, desk.get = desk.get, lambda url, body=None: held if body["params"][0] == "A" else empty
        try:
            self.assertEqual(desk.smart_holders("MINT", ["A", "B"]), {"A"})
        finally:
            desk.get = orig

    def test_filter_skips_rugcheck_on_dead_pair(self):
        calls = []
        orig, desk.get = desk.get, lambda url, body=None: calls.append(url)
        try:
            self.assertIsNone(desk.Filter().run(self.cand(liquidity={"usd": 0})[0], NOW))
        finally:
            desk.get = orig
        self.assertEqual(calls, [])

    def test_learn_wallets(self):
        d = tempfile.mkdtemp(); orig_dir, desk.DIR = desk.DIR, d
        W1, W2, W3 = "7Sbo9Re2ofWBmLSjG7QzGpknAGuLnLZCrDYSUa8meNcH", "DLRbZCdNGeXywLcUi2tMhKkdkwxiPsU6xhGk4Fw8mAbZ", "BTRwihySd7U8W6JKuyVWgm9ovndqnsrEPorvuzJyg4gm"
        st = {"seen": {"A": {"p": 1.0, "t": NOW, "w": [W1, W2]}, "B": {"p": 1.0, "t": NOW, "w": [W1, W3]},
                       "C": {"p": 1.0, "t": NOW, "w": [W2]}, "OLD": {"p": 1.0, "t": NOW - 80 * 3600, "w": [W3]}}}
        px = {"A": "2.5", "B": "2.0", "C": "1.5"}
        orig_get, desk.get = desk.get, lambda url, body=None: [
            {"baseToken": {"address": m}, "priceUsd": px[m], "liquidity": {"usd": 1}} for m in px]
        try:
            self.assertEqual(desk.learn_wallets(st, NOW), [W1])          # W1 held 2 winners
            self.assertEqual(set(st["seen"]), {"C"})                        # winners consumed, old pruned
            self.assertEqual(desk.load_smart_wallets(), {W1})
            self.assertEqual(desk.load_smart_wallets(auto=False), set())
        finally:
            desk.get, desk.DIR = orig_get, orig_dir

    def test_handoff_hard(self):
        with self.assertRaises(ValueError):
            desk.handoff({"mint": "M"}, ["mint", "price"], "Analyst", "Risk")


if __name__ == "__main__":
    unittest.main()
