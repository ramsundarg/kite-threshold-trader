"""Threshold trader on Zerodha Kite.

    python trader.py --demo           # offline, random-walk prices, no credentials
    python trader.py                  # PAPER: live Kite prices, simulated fills (default)
    python trader.py --live           # LIVE: real orders. Run login.py first.
"""
import argparse
import json
import logging
import os
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from broker import TERMINAL_STATUSES, DemoBroker, KiteBroker, PaperBroker
from strategy import Action, Position, Rule, decide

IST = ZoneInfo("Asia/Kolkata")
MARKET_CLOSE = dtime(15, 30)
HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("trader")


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    cfg["rules"] = [Rule(**r) for r in cfg["rules"]]
    if cfg["product"] not in ("MIS", "CNC"):
        raise ValueError("product must be MIS or CNC")
    if cfg["product"] == "CNC" and any(r.allow_short for r in cfg["rules"]):
        raise ValueError("allow_short needs product MIS (no delivery shorts in equity)")
    return cfg


def hhmm(s):
    return dtime.fromisoformat(s)


def make_kite():
    from kiteconnect import KiteConnect

    with open(os.path.join(HERE, ".kite_session.json")) as f:
        session = json.load(f)
    if session["date"] != datetime.now(IST).date().isoformat():
        raise SystemExit("Access token is from a previous day; run login.py again.")
    kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
    kite.set_access_token(session["access_token"])
    return kite


class Trader:
    def __init__(self, cfg, broker, respect_hours=True):
        self.cfg = cfg
        self.broker = broker
        self.rules = {r.symbol: r for r in cfg["rules"]}
        self.pending = {}          # symbol -> order_id awaiting a terminal status
        self.respect_hours = respect_hours

    def _pending_done(self, symbol):
        oid = self.pending.get(symbol)
        if oid is None:
            return True
        status = self.broker.order_status(oid)
        if status in TERMINAL_STATUSES:
            log.info("%s order %s -> %s", symbol, oid, status)
            del self.pending[symbol]
            return True
        return False

    def _send(self, rule, action, qty, ltp, reason):
        log.info("%-12s %-4s %d @ ~%.2f  | %s", rule.symbol, action.value, qty, ltp, reason)
        try:
            self.pending[rule.symbol] = self.broker.place(rule, action, qty, ltp)
        except Exception:
            log.exception("%s order failed", rule.symbol)

    def step(self, now=None):
        """One pass over all symbols. Returns False once the session is over."""
        now = now or datetime.now(IST).time()
        if self.respect_hours and now < hhmm(self.cfg["market_open"]):
            return True
        if self.respect_hours and now >= MARKET_CLOSE:
            return False
        squareoff = self.respect_hours and self.cfg["product"] == "MIS" and now >= hhmm(self.cfg["squareoff_time"])
        no_new_entries = self.respect_hours and now >= hhmm(self.cfg["entry_cutoff"])

        prices = self.broker.ltp(self.rules)
        for symbol, rule in self.rules.items():
            if not self._pending_done(symbol):
                continue           # never stack orders on an unfilled one
            ltp = prices[symbol]
            pos = self.broker.position(rule)

            if squareoff:
                if pos.quantity:
                    close = Action.SELL if pos.quantity > 0 else Action.BUY
                    self._send(rule, close, abs(pos.quantity), ltp, "end-of-day MIS square-off")
                continue

            d = decide(rule, pos, ltp)
            if d.action is Action.HOLD:
                continue
            if pos.quantity == 0 and no_new_entries:
                continue           # exits are always allowed, entries only before the cutoff
            self._send(rule, d.action, d.quantity, ltp, d.reason)

        return not (squareoff and not self.pending and all(self.broker.position(r).quantity == 0 for r in self.rules.values()))

    def run(self, max_steps=None):
        n = 0
        while self.step():
            n += 1
            if max_steps and n >= max_steps:
                break
            time.sleep(self.cfg["poll_seconds"])
        log.info("session over")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="offline random-walk prices")
    mode.add_argument("--live", action="store_true", help="send REAL orders")
    ap.add_argument("--steps", type=int, default=None, help="stop after N polls")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    cfg_path = args.config if os.path.exists(args.config) else os.path.join(HERE, "config.example.json")
    cfg = load_config(cfg_path)

    if args.demo:
        cfg["poll_seconds"] = 0
        broker = DemoBroker(cfg["rules"], seed=42)
        trader = Trader(cfg, broker, respect_hours=False)
        trader.run(max_steps=args.steps or 500)
        for s, pnl in broker.realised.items():
            log.info("%-12s realised P&L %+.2f   open %s", s, pnl, broker.positions.get(s, Position()))
        return

    kite_broker = KiteBroker(make_kite(), cfg["product"], cfg["limit_buffer_pct"])
    if args.live:
        log.warning("LIVE mode: real orders will be placed (%s)", cfg["product"])
        broker = kite_broker
    else:
        log.info("PAPER mode: live prices, simulated fills. Use --live for real orders.")
        broker = PaperBroker(price_source=kite_broker)
    Trader(cfg, broker).run(max_steps=args.steps)


if __name__ == "__main__":
    main()
