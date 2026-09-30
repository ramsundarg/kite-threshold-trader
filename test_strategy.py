import unittest
from datetime import time

from broker import DemoBroker, PaperBroker, marketable_limit
from strategy import Action, Position, Rule, decide
from trader import Trader

R = Rule("NSE:INFY", buy_below=1000, sell_above=1100, quantity=10)


class Decide(unittest.TestCase):
    def test_flat_buy_below_threshold(self):
        d = decide(R, Position(), 999)
        self.assertEqual((d.action, d.quantity), (Action.BUY, 10))

    def test_flat_between_thresholds_holds(self):
        self.assertIs(decide(R, Position(), 1050).action, Action.HOLD)

    def test_flat_above_sell_no_short_by_default(self):
        self.assertIs(decide(R, Position(), 1200).action, Action.HOLD)

    def test_flat_above_sell_shorts_when_allowed(self):
        r = Rule("NSE:INFY", 1000, 1100, 10, allow_short=True)
        self.assertIs(decide(r, Position(), 1200).action, Action.SELL)

    def test_long_sells_above_threshold(self):
        d = decide(R, Position(10, 1080), 1100)
        self.assertEqual((d.action, d.quantity), (Action.SELL, 10))

    def test_long_stop_loss_at_5pct(self):
        d = decide(R, Position(10, 1000), 950)       # exactly -5%
        self.assertEqual(d.action, Action.SELL)
        self.assertIn("stop-loss", d.reason)

    def test_long_just_inside_stop_holds(self):
        # 951 is below buy_below but we already hold: no pyramiding
        self.assertIs(decide(R, Position(10, 1000), 951).action, Action.HOLD)

    def test_long_take_profit_at_15pct(self):
        r = Rule("NSE:INFY", 1000, 2000, 10)          # sell_above far away
        d = decide(r, Position(10, 1000), 1150)
        self.assertEqual(d.action, Action.SELL)
        self.assertIn("take-profit", d.reason)

    def test_short_stop_loss_when_price_rises(self):
        r = Rule("NSE:INFY", 1000, 1100, 10, allow_short=True)
        d = decide(r, Position(-10, 1100), 1155)      # +5% against a short
        self.assertEqual((d.action, d.quantity), (Action.BUY, 10))
        self.assertIn("stop-loss", d.reason)

    def test_stop_loss_beats_signal(self):
        # below buy_below AND past stop: risk exit, never an add
        d = decide(R, Position(10, 1100), 900)
        self.assertEqual(d.action, Action.SELL)

    def test_bad_thresholds_rejected(self):
        with self.assertRaises(ValueError):
            Rule("NSE:INFY", 1100, 1000, 1)


class Broker(unittest.TestCase):
    def test_marketable_limit_rounds_to_tick(self):
        self.assertEqual(marketable_limit(Action.BUY, 1000.02, 0.5), 1005.05)
        self.assertEqual(marketable_limit(Action.SELL, 1000.02, 0.5), 995.0)

    def test_paper_round_trip_pnl(self):
        b = PaperBroker()
        b.place(R, Action.BUY, 10, 1000)
        b.place(R, Action.SELL, 10, 1150)
        self.assertEqual(b.position(R), Position(0, 0.0))
        self.assertAlmostEqual(b.realised[R.symbol], 1500)


class TraderLoop(unittest.TestCase):
    CFG = {"product": "MIS", "poll_seconds": 0, "market_open": "09:15",
           "entry_cutoff": "15:00", "squareoff_time": "15:15", "rules": [R]}

    def _trader(self, price):
        b = DemoBroker([R])
        b.ltp = lambda syms: {s: price for s in syms}
        return Trader(self.CFG, b), b

    def test_no_entry_after_cutoff(self):
        t, b = self._trader(990)
        t.step(now=time(15, 5))
        self.assertEqual(b.position(R).quantity, 0)

    def test_mis_squareoff(self):
        t, b = self._trader(990)
        t.step(now=time(10, 0))
        self.assertEqual(b.position(R).quantity, 10)
        t.step(now=time(15, 16))
        self.assertEqual(b.position(R).quantity, 0)


if __name__ == "__main__":
    unittest.main()
