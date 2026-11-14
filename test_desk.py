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
        calm_up = {"data": {"attributes": {"ohlcv_list": [[i, 1 + i * .004, 1 + i * .004 + .006, 1 + i * .004 - .001,
                                                          1 + (i + 1) * .004, 1] for i in range(120)]}}}
        crash = {"data": {"attributes": {"ohlcv_list": [[i, 1, 1.01, .9, .97, 1] for i in range(120)]}}}
        state = {"bars": calm_up, "px": "0.0101", "kw": {}}
        def fake(url, body=None):
            if "ohlcv" in url: return state["bars"]
            return [pair(priceUsd=state["px"], **state["kw"])]
        orig, desk.get = desk.get, fake
        go = lambda: desk.Risk().recheck(sig, NOW)
        try:
            r = go()
            self.assertAlmostEqual(r["price"], 0.0101)                     # fresh price replaces the stale one
            self.assertEqual(r["liq"], 300_000)
            self.assertLess(r["tm"]["stop"], 0.5)
            state["bars"] = crash
            self.assertIsNone(go())                                         # time machine: stop first too often
            state["bars"], state["px"] = calm_up, "0.0090"
            self.assertIsNone(go())                                         # -10% since signal: momentum broke
            state["px"], state["kw"] = "0.0101", {"priceChange": {"h1": 250}}
            self.assertIsNone(go())                                         # pumped past the +80% rule meanwhile
            state["kw"], state["bars"] = {}, None
            self.assertIsNone(go())                                         # no bars: fail closed
            desk.get = lambda url, body=None: None
            self.assertIsNone(go())
        finally:
            desk.get = orig

    def test_time_machine(self):
        up = [(1.01 ** i,) * 4 for i in range(60)]                       # +1%/min, never dips
        self.assertEqual(desk.time_machine(up, 0.06, 0.12, 240, 500, 1)["arm"], 1.0)
        down = [(0.99 ** i,) * 4 for i in range(60)]
        self.assertEqual(desk.time_machine(down, 0.06, 0.12, 240, 500, 1)["stop"], 1.0)
        both = [(1, 2, 0.5, 1)] * 10                                          # stop and arm in the same bar
        self.assertEqual(desk.time_machine(both, 0.06, 0.12, 5, 100, 1)["stop"], 1.0)
        r = desk.time_machine([(1, 1, 1, 1)] * 10, 0.06, 0.12, 5, 100, 1)   # flat: neither
        self.assertEqual(r, {"stop": 0.0, "arm": 0.0, "none": 1.0})

    def test_risk_limits(self):
        st = {"pos": {}, "cash": 10_000}
        self.assertIsNone(desk.Risk.limits(st, 10_000, NOW))
        self.assertIn("daily loss", desk.Risk.limits(st, 9_650, NOW))
        self.assertIsNone(desk.Risk.limits(st, 9_650, NOW + 86_400))          # new day, new baseline
        st["pos"] = {str(i): {} for i in range(5)}
        self.assertIn("open positions", desk.Risk.limits(st, 9_650, NOW + 86_400))
        st["pos"], st["day"]["buys"] = {}, 20
        self.assertIn("buys today", desk.Risk.limits(st, 9_650, NOW + 86_400))

    def test_time_exit(self):
        p = {"entry": 1.0, "stop": 0.94, "peak": 1.0, "opened": NOW}
        self.assertIsNone(desk.Risk.update(p, 1.05, NOW + 3 * 3600))
        self.assertEqual(desk.Risk.update(p, 1.05, NOW + 4 * 3600), "time_exit")
        q = {"entry": 1.0, "stop": 0.94, "peak": 1.0, "opened": NOW}
        desk.Risk.update(q, 1.2, NOW)                                         # trailing: no time exit
        self.assertIsNone(desk.Risk.update(q, 1.15, NOW + 5 * 3600))

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

    def test_scout_rotates_gecko_lists(self):
        asked = []
        def fake(url, body=None):
            asked.append(url)
            if "geckoterminal" in url:
                return {"data": [{"relationships": {"base_token": {"data": {"id": f"solana_G{len(asked)}"}}}}]}
            return [] if "/tokens/v1/" in url else None
        orig, desk.get, gap = desk.get, fake, desk.CFG["gecko_gap_s"]
        desk.CFG["gecko_gap_s"] = 0
        try:
            sc = desk.Scout(); sc.run(NOW); sc.run(NOW + 120)
        finally:
            desk.get, desk.CFG["gecko_gap_s"] = orig, gap
        g = [u for u in asked if "geckoterminal" in u]
        self.assertEqual(len(g), 6); self.assertEqual(len(set(g)), 6)        # 3 per scan, different ones
        self.assertEqual(len(sc.gecko_seen), 6)                              # earlier lists are remembered
        self.assertEqual(sum(u.count("G") for u in asked if "/tokens/v1/" in u and "G" in u.split("/")[-1]) >= 6, True)

    def test_get_does_not_retry_429(self):
        import urllib.error, urllib.request
        calls = []
        def boom(req, timeout=0):
            calls.append(1); raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", None, None)
        orig, urllib.request.urlopen = urllib.request.urlopen, boom
        try:
            self.assertIsNone(desk.get("https://x.test/a"))
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(len(calls), 1)

    def test_gecko_calls_are_paced(self):
        import urllib.request
        stamps = []
        class R:
            def __enter__(s): return s
            def __exit__(s, *a): pass
            def read(s, *a): return b"{}"
        def fake(req, timeout=0):
            stamps.append(time.time()); return R()
        orig, urllib.request.urlopen = urllib.request.urlopen, fake
        gap, desk.CFG["gecko_gap_s"] = desk.CFG["gecko_gap_s"], 0.2
        try:
            for _ in range(3): desk.get(desk.GT + "/x")
            desk.get("https://api.dexscreener.com/x")                         # other hosts: not paced
        finally:
            urllib.request.urlopen, desk.CFG["gecko_gap_s"] = orig, gap
        self.assertGreaterEqual(stamps[2] - stamps[0], 0.39)
        self.assertLess(stamps[3] - stamps[2], 0.1)

    def test_keys_and_redaction(self):
        p = os.path.join(tempfile.mkdtemp(), "keys.env")
        open(p, "w").write("# comment\nZZ_TEST_KEY = abc123\nBROKEN\n")
        desk.load_keys(p)
        self.assertEqual(os.environ.pop("ZZ_TEST_KEY"), "abc123")
        self.assertEqual(desk.redact("https://mainnet.helius-rpc.com/?api-key=SECRET&x=1"),
                         "https://mainnet.helius-rpc.com/?api-key=***&x=1")

    def test_bars_use_demo_key_when_set(self):
        seen = []
        orig, desk.get = desk.get, lambda url, body=None, headers=None: seen.append((url, headers)) or None
        key, desk.CG_KEY = desk.CG_KEY, "K"
        try:
            desk.bars_1m("POOL")
            desk.CG_KEY = None
            desk.bars_1m("POOL")
        finally:
            desk.get, desk.CG_KEY = orig, key
        self.assertTrue(seen[0][0].startswith(desk.CG) and seen[0][1] == {"x-cg-demo-api-key": "K"})
        self.assertTrue(seen[1][0].startswith(desk.GT))

    def test_handoff_hard(self):
        with self.assertRaises(ValueError):
            desk.handoff({"mint": "M"}, ["mint", "price"], "Analyst", "Risk")


if __name__ == "__main__":
    unittest.main()
