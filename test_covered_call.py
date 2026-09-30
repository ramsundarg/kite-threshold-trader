import unittest
from datetime import date, datetime, timedelta

from cc_trader import CCTrader, DemoCC, KiteCC, bs_call, last_tuesday
from covered_call import CCRule, CCState, OptionContract, ShortCall, decide_cc, select_call
from strategy import Action

TODAY = date(2026, 1, 5)
R = CCRule("NSE:INFY", lots=1, otm_pct=5, min_days_to_expiry=20, max_days_to_expiry=60,
           roll_days_before_expiry=5, take_profit_pct=80, stock_stop_loss_pct=5)
NEAR, FAR, TOO_CLOSE = date(2026, 2, 24), date(2026, 3, 31), date(2026, 1, 20)


def chain(lot=400):
    return [OptionContract(f"INFY{e:%y%b}{k}CE".upper(), e, float(k), lot)
            for e in (TOO_CLOSE, NEAR, FAR) for k in range(1400, 1700, 20)]


def short(strike=1580, expiry=NEAR, qty=400, avg=20.0, ltp=15.0):
    return ShortCall(OptionContract("INFYX", expiry, strike, 400), qty, avg, ltp)


class SelectCall(unittest.TestCase):
    def test_nearest_eligible_expiry_and_lowest_otm_strike(self):
        c = select_call(chain(), 1500, R, TODAY)
        self.assertEqual((c.expiry, c.strike), (NEAR, 1580))   # 1575 floor -> 1580; Jan too close

    def test_none_when_no_expiry_in_window(self):
        self.assertIsNone(select_call(chain(), 1500, R, date(2026, 3, 20)))


class DecideCC(unittest.TestCase):
    def test_writes_one_lot_when_holding_shares(self):
        o = decide_cc(R, CCState(1500, 400, 1450), TODAY, chain())
        self.assertEqual((o.exchange, o.side, o.quantity, o.product), ("NFO", Action.SELL, 400, "NRML"))

    def test_lots_capped_by_shares(self):
        r = CCRule("NSE:INFY", lots=3)
        o = decide_cc(r, CCState(1500, 900, 1450), TODAY, chain())
        self.assertEqual(o.quantity, 800)                      # 900 shares cover 2 lots of 400

    def test_nothing_below_one_lot(self):
        self.assertIsNone(decide_cc(R, CCState(1500, 399, 1450), TODAY, chain()))

    def test_hold_while_call_working(self):
        self.assertIsNone(decide_cc(R, CCState(1500, 400, 1450, short()), TODAY, chain()))

    def test_take_profit_on_decay(self):
        o = decide_cc(R, CCState(1500, 400, 1450, short(avg=20, ltp=4)), TODAY, chain())
        self.assertEqual((o.side, o.quantity), (Action.BUY, 400))
        self.assertIn("take-profit", o.reason)

    def test_roll_near_expiry(self):
        o = decide_cc(R, CCState(1500, 400, 1450, short()), NEAR - timedelta(days=5), chain())
        self.assertEqual(o.side, Action.BUY)
        self.assertIn("roll", o.reason)

    def test_buys_back_uncovered_calls(self):
        o = decide_cc(R, CCState(1500, 0, 0, short()), TODAY, chain())
        self.assertEqual((o.side, o.quantity), (Action.BUY, 400))
        self.assertIn("uncovered", o.reason)

    def test_stop_loss_closes_call_before_shares(self):
        st = CCState(1420, 400, 1500, short())                 # -5.3%
        first = decide_cc(R, st, TODAY, chain())
        self.assertEqual((first.exchange, first.side), ("NFO", Action.BUY))
        second = decide_cc(R, CCState(1420, 400, 1500), TODAY, chain())
        self.assertEqual((second.exchange, second.tradingsymbol, second.side, second.product),
                         ("NSE", "INFY", Action.SELL, "CNC"))
        self.assertTrue(second.final)

    def test_stop_loss_sells_only_covered_shares(self):
        o = decide_cc(R, CCState(1420, 1000, 1500), TODAY, chain())
        self.assertEqual(o.quantity, 400)                      # lots=1, not the whole 1000

    def test_stop_disabled(self):
        r = CCRule("NSE:INFY", stock_stop_loss_pct=None)
        o = decide_cc(r, CCState(1000, 400, 1500), TODAY, chain())
        self.assertEqual(o.side, Action.SELL)
        self.assertEqual(o.exchange, "NFO")                    # keeps writing calls

    def test_bad_rule_rejected(self):
        with self.assertRaises(ValueError):
            CCRule("NSE:INFY", roll_days_before_expiry=25, min_days_to_expiry=20)


class Demo(unittest.TestCase):
    CFG = {"poll_seconds": 0, "order_timeout_seconds": 30, "market_open": "09:20",
           "market_close": "15:25", "rules": [R]}

    def test_last_tuesday(self):
        self.assertEqual(last_tuesday(2026, 0), date(2026, 1, 27))
        self.assertEqual(last_tuesday(2026, 12), date(2027, 1, 26))

    def test_bs_call_at_expiry_is_intrinsic(self):
        self.assertEqual(bs_call(110, 100, 0, 0.06, 0.2), 10)

    def test_stopped_underlying_is_not_managed_again(self):
        b = DemoCC([R], start=TODAY)
        t = CCTrader(self.CFG, b)
        now = datetime(2026, 1, 5, 10, 0)
        t.step(now)                                            # writes a call
        b.spot[R.underlying] = 1400                            # -6.7%
        for _ in range(4):
            t.step(now)
        self.assertEqual(b.shares[R.underlying], 0)
        self.assertNotIn(R.underlying, b.shorts)
        self.assertIn(R.underlying, t.stopped)

    def test_rejection_cools_down(self):
        b = DemoCC([R], start=TODAY)
        sent = []
        b.place = lambda order: sent.append(order) or "OID"     # broker rejects, nothing fills
        b.order_info = lambda oid: {"status": "REJECTED", "status_message": "Insufficient margin"}
        t = CCTrader(self.CFG, b)
        now = datetime(2026, 1, 5, 10, 0)
        t.step(now)                                            # sends the write
        t.step(now)                                            # sees REJECTED
        t.step(now + timedelta(minutes=5))
        self.assertEqual(len(sent), 1)                         # no resend inside the cooldown
        t.step(now + timedelta(minutes=16))
        self.assertEqual(len(sent), 2)

    def test_paper_journal_not_touched_in_demo(self):
        self.assertEqual(CCTrader(self.CFG, DemoCC([R], start=TODAY)).journal, {})


class FakeKite:
    VARIETY_REGULAR, ORDER_TYPE_LIMIT, VALIDITY_DAY = "regular", "LIMIT", "DAY"
    TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "BUY", "SELL"

    def __init__(self):
        self.placed = []

    def instruments(self, exchange):
        return [{"tradingsymbol": "INFY26FEB1580CE", "name": "INFY", "instrument_type": "CE",
                 "segment": "NFO-OPT", "expiry": NEAR, "strike": 1580.0, "lot_size": 400},
                {"tradingsymbol": "INFY26FEB1580PE", "name": "INFY", "instrument_type": "PE",
                 "segment": "NFO-OPT", "expiry": NEAR, "strike": 1580.0, "lot_size": 400},
                {"tradingsymbol": "TCS26FEB4000CE", "name": "TCS", "instrument_type": "CE",
                 "segment": "NFO-OPT", "expiry": NEAR, "strike": 4000.0, "lot_size": 175}]

    def holdings(self):
        return [{"exchange": "NSE", "tradingsymbol": "INFY", "quantity": 400, "t1_quantity": 0, "average_price": 1450.0}]

    def positions(self):
        return {"net": [{"exchange": "NFO", "tradingsymbol": "INFY26FEB1580CE", "product": "NRML",
                         "quantity": -400, "average_price": 21.0}], "day": []}

    def quote(self, *keys):
        return {k: {"last_price": 1500.0 if k == "NSE:INFY" else 12.0,
                    "depth": {"buy": [{"price": 11.9}], "sell": [{"price": 12.1}]}} for k in keys}

    def place_order(self, **kw):
        self.placed.append(kw)
        return "OID1"


class KiteWiring(unittest.TestCase):
    def test_state_and_order(self):
        k = FakeKite()
        b = KiteCC(k)
        ch = b.chain(R, TODAY)
        self.assertEqual([c.tradingsymbol for c in ch], ["INFY26FEB1580CE"])   # CE of INFY only
        st = b.state(R, ch, journal={"INFY26FEB1580CE": 20.0})
        self.assertEqual((st.spot, st.shares, st.stock_avg), (1500.0, 400, 1450.0))
        self.assertEqual((st.short.quantity, st.short.average_price, st.short.ltp), (400, 20.0, 12.0))

        o = decide_cc(R, st, NEAR - timedelta(days=3), ch)                     # roll -> buy back
        b.place(o)
        self.assertEqual({x: k.placed[0][x] for x in ("exchange", "tradingsymbol", "transaction_type",
                                                    "quantity", "product", "price")},
                         {"exchange": "NFO", "tradingsymbol": "INFY26FEB1580CE", "transaction_type": "BUY",
                          "quantity": 400, "product": "NRML", "price": 12.1})   # pays the offer


if __name__ == "__main__":
    unittest.main()
