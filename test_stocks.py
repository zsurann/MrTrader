import json, os, shutil, tempfile, unittest
import stocks

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures", "stocks")


class T(unittest.TestCase):
    """End to end on the skill pack's own reel day: TSLA opening-range breakout, Monday 2026-10-19."""
    def setUp(self):
        self.work = tempfile.mkdtemp()
        stocks.WORK, stocks.T = self.work, os.path.join(self.work, "trader")
        stocks.EXTRA = {"scan orb": ["--offline", os.path.join(FX, "wall5m-2026-10-19.json.gz"), "--min-price", "0"],
                        "tm run": ["--offline", os.path.join(FX, "TSLA-60d5m.json.gz")],
                        "alerts quote": ["--offline", os.path.join(FX, "alerts")],
                        "news": ["--offline", os.path.join(FX, "news")]}
        self.notes = []
        stocks.notify = self.notes.append

    def tearDown(self):
        shutil.rmtree(self.work)

    def trades(self):
        p = os.path.join(self.work, "logs", "stocks.jsonl")
        return [r for r in map(json.loads, open(p)) if r["kind"] == "trade"]

    def test_reel_day_buy_then_flat_by_close(self):
        st = stocks.load()
        stocks.cycle(st, "2026-10-19 10:15")
        buys = self.trades()
        tsla = [b for b in buys if b["symbol"] == "TSLA"]
        self.assertEqual(len(tsla), 1)
        b = tsla[0]
        self.assertEqual((b["qty"], b["stop"]), (8, 358.32))           # $50 max loss / 5.86 risk = 8 shares
        self.assertGreater(b["avg_R"], 0)                              # time machine gate passed
        self.assertIn("TSLA", st["pos"]); self.assertLessEqual(st["trades"], 3)
        self.assertTrue(any("COMPRA TSLA" in n for n in self.notes))
        pf = json.load(open(os.path.join(stocks.T, "portfolio.json")))
        self.assertTrue(any(p["ticker"] == "TSLA" for p in pf["positions"]))
        stocks.cycle(st, "2026-10-19 10:20")                           # no new scan within 10 min, no exit at 366
        self.assertIn("TSLA", st["pos"])
        stocks.cycle(st, "2026-10-19 15:55")                           # flat by 15:55: never held overnight
        self.assertEqual(st["pos"], {})
        self.assertEqual(self.trades()[-1]["reason"], "flat_by")

    def test_closed_market_does_nothing(self):
        st = stocks.load()
        stocks.cycle(st, "2026-10-18 10:15")                           # Sunday
        self.assertIsNone(st["date"])

    def test_hhmm(self):
        self.assertEqual(stocks.hhmm("10:15 AM"), 615)
        self.assertEqual(stocks.hhmm("3:55 PM"), 955)
        self.assertEqual(stocks.hhmm("15:55"), 955)


if __name__ == "__main__":
    unittest.main()
