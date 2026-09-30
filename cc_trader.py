"""Covered-call runner on Zerodha Kite.

    python cc_trader.py --demo       # offline: simulated stock + Black-Scholes option prices, ~1 year
    python cc_trader.py              # PAPER: live Kite data, prints the orders it would send
    python cc_trader.py --live       # LIVE: real orders. Run login.py first.

You must already hold at least one lot of the stock (bought as CNC / in holdings).
"""
import argparse
import json
import logging
import math
import os
import random
import time
from datetime import date, datetime, time as dtime, timedelta

from broker import TERMINAL_STATUSES, KiteBroker
from covered_call import CCRule, CCState, OptionContract, Order, ShortCall, decide_cc
from strategy import Action
from trader import HERE, IST, hhmm, make_kite

log = logging.getLogger("cc")
JOURNAL = os.path.join(HERE, ".cc_premiums.json")   # premium actually received per option we wrote


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    cfg["rules"] = [CCRule(**r) for r in cfg["rules"]]
    return cfg


def load_journal():
    try:
        with open(JOURNAL) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


class KiteCC:
    """Stock from holdings, short calls from NRML positions, orders cross the spread."""

    def __init__(self, kite, tag="covcall"):
        self.kite = kite
        self.tag = tag
        self._nfo, self._nfo_day = [], None

    def chain(self, rule, today):
        if self._nfo_day != today:                     # ~100k rows; refresh once a day
            self._nfo, self._nfo_day = self.kite.instruments("NFO"), today
        return [OptionContract(i["tradingsymbol"], i["expiry"], i["strike"], i["lot_size"])
                for i in self._nfo
                if i["name"] == rule.tradingsymbol and i["instrument_type"] == "CE" and i["segment"] == "NFO-OPT"]

    def state(self, rule, chain, journal):
        stock = KiteBroker(self.kite, "CNC").position(rule)
        by_symbol = {c.tradingsymbol: c for c in chain}
        shorts = [p for p in self.kite.positions()["net"]
                  if p["exchange"] == "NFO" and p["product"] == "NRML"
                  and p["quantity"] < 0 and p["tradingsymbol"] in by_symbol]
        if len(shorts) > 1:
            log.warning("%s: %d short call series open; managing %s only",
                        rule.underlying, len(shorts), shorts[0]["tradingsymbol"])

        keys = [rule.underlying] + [f"NFO:{p['tradingsymbol']}" for p in shorts[:1]]
        quotes = self.kite.quote(*keys)
        short = None
        if shorts:
            p = shorts[0]
            short = ShortCall(by_symbol[p["tradingsymbol"]], -p["quantity"],
                              journal.get(p["tradingsymbol"], p["average_price"]),
                              quotes[f"NFO:{p['tradingsymbol']}"]["last_price"])
        return CCState(quotes[rule.underlying]["last_price"], max(stock.quantity, 0), stock.average_price, short)

    def place(self, order: Order) -> str:
        key = f"{order.exchange}:{order.tradingsymbol}"
        depth = self.kite.quote(key)[key]["depth"]["buy" if order.side is Action.SELL else "sell"]
        if not depth or depth[0]["price"] <= 0:
            raise RuntimeError(f"{key}: no {'bid' if order.side is Action.SELL else 'offer'} to trade against")
        k = self.kite
        return k.place_order(
            variety=k.VARIETY_REGULAR, exchange=order.exchange, tradingsymbol=order.tradingsymbol,
            transaction_type=k.TRANSACTION_TYPE_SELL if order.side is Action.SELL else k.TRANSACTION_TYPE_BUY,
            quantity=order.quantity, product=order.product, order_type=k.ORDER_TYPE_LIMIT,
            price=depth[0]["price"], validity=k.VALIDITY_DAY, tag=self.tag,
        )

    def order_info(self, order_id) -> dict:
        return self.kite.order_history(order_id)[-1]

    def cancel(self, order_id):
        self.kite.cancel_order(self.kite.VARIETY_REGULAR, order_id)


class PaperCC(KiteCC):
    """Live Kite data, but orders are only logged."""

    def place(self, order):
        return "PAPER"

    def order_info(self, order_id):
        return {"status": "CANCELLED", "average_price": 0}

    def cancel(self, order_id):
        pass


class CCTrader:
    def __init__(self, cfg, broker):
        self.cfg = cfg
        self.broker = broker
        self.rules = cfg["rules"]
        self.pending = {}          # underlying -> (order_id, Order, sent_at)
        self.stopped = set()       # underlyings exited by the stock stop-loss
        self.cooldown = {}         # underlying -> datetime before which we don't retry
        self.journal = load_journal() if isinstance(broker, KiteCC) and not isinstance(broker, PaperCC) else {}

    def _save_journal(self):
        if isinstance(self.broker, KiteCC):
            with open(JOURNAL, "w") as f:
                json.dump(self.journal, f, indent=1)

    def _check_pending(self, rule, now) -> bool:
        """True when nothing is pending any more for this underlying."""
        oid, order, sent = self.pending[rule.underlying]
        info = self.broker.order_info(oid)
        if info["status"] in TERMINAL_STATUSES:
            del self.pending[rule.underlying]
            if info["status"] == "COMPLETE":
                log.info("%s %-14s filled %s %d %s @ %.2f", now.date(), rule.underlying, order.side.value,
                         order.quantity, order.tradingsymbol, info["average_price"])
                if order.exchange == "NFO" and order.side is Action.SELL:
                    self.journal[order.tradingsymbol] = info["average_price"]
                    self._save_journal()
                if order.final:
                    self.stopped.add(rule.underlying)
                    log.info("%s %-14s stop-loss done; no longer managed", now.date(), rule.underlying)
            elif info["status"] == "REJECTED":
                self.cooldown[rule.underlying] = now + timedelta(minutes=15)
                log.warning("%s %-14s order REJECTED (%s); retrying in 15 min", now.date(), rule.underlying,
                            info.get("status_message") or "no reason given")
            return True
        if (now - sent).total_seconds() >= self.cfg["order_timeout_seconds"]:
            log.info("%s %-14s order %s unfilled after %ss; cancelling (re-decided next cycle)",
                     now.date(), rule.underlying, oid, self.cfg["order_timeout_seconds"])
            self.broker.cancel(oid)
        return False

    def step(self, now: datetime) -> bool:
        if now.time() < hhmm(self.cfg["market_open"]):
            return True
        if now.time() >= hhmm(self.cfg["market_close"]):
            return False
        for rule in self.rules:
            if rule.underlying in self.stopped:
                continue
            if rule.underlying in self.pending and not self._check_pending(rule, now):
                continue
            if now < self.cooldown.get(rule.underlying, now):
                continue
            try:
                chain = self.broker.chain(rule, now.date())
                st = self.broker.state(rule, chain, self.journal)
                order = decide_cc(rule, st, now.date(), chain)
                if order:
                    log.info("%s %-14s %-4s %d %s | %s", now.date(), rule.underlying, order.side.value,
                             order.quantity, order.tradingsymbol, order.reason)
                    self.pending[rule.underlying] = (self.broker.place(order), order, now)
            except Exception:
                log.exception("%s: cycle failed", rule.underlying)
        return True

    def run(self):
        while self.step(datetime.now(IST).replace(tzinfo=None)):
            time.sleep(self.cfg["poll_seconds"])
        log.info("market closed")


# ---------------------------------------------------------------- offline demo

def bs_call(spot, strike, years, rate, vol):
    if years <= 0:
        return max(spot - strike, 0.0)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return spot * n(d1) - strike * math.exp(-rate * years) * n(d2)


def last_tuesday(y, m0):
    """Last Tuesday of month m0 (0-based, may exceed 11) of year y."""
    n = m0 + 1
    d = date(y + n // 12, n % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 1) % 7)


class DemoCC:
    """One simulated stock per rule; fills at the model price; physical settlement at expiry."""

    def __init__(self, rules, start, seed=7, spot=1500.0, vol=0.25, lot=400):
        self.rng, self.vol, self.rate, self.lot = random.Random(seed), vol, 0.065, lot
        self.today = start
        self.spot = {r.underlying: spot for r in rules}
        self.shares = {r.underlying: lot * r.lots for r in rules}
        self.avg = dict(self.spot)
        self.shorts = {}            # underlying -> [contract, qty, premium]
        self.cash = 0.0
        self.contracts = {}         # option tradingsymbol -> (underlying, contract)
        self.start = {u: self.shares[u] * self.spot[u] for u in self.spot}
        self._n = 0

    def chain(self, rule, today):
        s = self.spot[rule.underlying]
        out = []
        for i in range(3):
            exp = last_tuesday(today.year, today.month - 1 + i)
            if exp < today:
                continue
            for k in range(int(s * 0.8 / 20) * 20, int(s * 1.3), 20):
                c = OptionContract(f"{rule.tradingsymbol}{exp:%y%b}{k}CE".upper(), exp, float(k), self.lot)
                self.contracts[c.tradingsymbol] = (rule.underlying, c)
                out.append(c)
        return out

    def _premium(self, u, c):
        return round(bs_call(self.spot[u], c.strike, (c.expiry - self.today).days / 365, self.rate, self.vol), 2)

    def state(self, rule, chain, journal):
        u = rule.underlying
        sh = self.shorts.get(u)
        short = ShortCall(sh[0], sh[1], sh[2], self._premium(u, sh[0])) if sh else None
        return CCState(self.spot[u], self.shares[u], self.avg[u], short)

    def place(self, order):
        self._n += 1
        if order.exchange != "NFO":
            u = f"{order.exchange}:{order.tradingsymbol}"
            px = self.spot[u]
            self.shares[u] -= order.quantity
            self.cash += px * order.quantity
        else:
            u, c = self.contracts[order.tradingsymbol]
            px = self._premium(u, c)
            if order.side is Action.SELL:
                self.shorts[u] = [c, order.quantity, px]
                self.cash += px * order.quantity
            else:
                self.cash -= px * order.quantity
                self.shorts[u][1] -= order.quantity
                if self.shorts[u][1] == 0:
                    del self.shorts[u]
        self.last_fill = px
        return f"DEMO-{self._n}"

    def order_info(self, order_id):
        return {"status": "COMPLETE", "average_price": self.last_fill}

    def cancel(self, order_id):
        pass

    def next_day(self):
        self.today += timedelta(days=3 if self.today.weekday() == 4 else 1)
        for u in self.spot:
            self.spot[u] = round(self.spot[u] * math.exp(self.rng.gauss(0, self.vol / math.sqrt(252))), 2)
            sh = self.shorts.get(u)
            if sh and sh[0].expiry < self.today:
                c, qty, _ = sh
                if self.spot[u] > c.strike:           # assigned: shares delivered at the strike
                    log.info("%s %-14s %s expired ITM: %d shares called away at %g", self.today, u, c.tradingsymbol, qty, c.strike)
                    self.shares[u] -= qty
                    self.cash += c.strike * qty
                del self.shorts[u]

    def report(self):
        for u, start in self.start.items():
            sh = self.shorts.get(u)
            liability = self._premium(u, sh[0]) * sh[1] if sh else 0.0
            value = self.cash + self.shares[u] * self.spot[u] - liability
            hold = start / self.avg[u] * self.spot[u]
            log.info("%-14s covered call %+.0f (%+.1f%%)  vs buy-and-hold %+.0f (%+.1f%%)  spot %.2f",
                     u, value - start, (value / start - 1) * 100, hold - start, (hold / start - 1) * 100, self.spot[u])


def run_demo(cfg, days, seed):
    broker = DemoCC(cfg["rules"], start=date(2026, 1, 5), seed=seed)
    trader = CCTrader(cfg, broker)
    for _ in range(days):
        now = datetime.combine(broker.today, dtime(10, 0))
        for _ in range(3):          # a few cycles a day so two-step exits/rolls finish same day
            trader.step(now)
        broker.next_day()
    broker.report()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "covered_call.json"))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true")
    mode.add_argument("--live", action="store_true")
    ap.add_argument("--days", type=int, default=250, help="demo length in trading days")
    ap.add_argument("--seed", type=int, default=7, help="demo random price path")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    path = args.config if os.path.exists(args.config) else os.path.join(HERE, "covered_call.example.json")
    cfg = load_config(path)

    if args.demo:
        run_demo(cfg, args.days, args.seed)
        return
    kite = make_kite()
    if args.live:
        log.warning("LIVE mode: real orders will be placed")
        broker = KiteCC(kite)
    else:
        log.info("PAPER mode: live data, orders are only logged. Use --live for real orders.")
        broker = PaperCC(kite)
    CCTrader(cfg, broker).run()


if __name__ == "__main__":
    main()
