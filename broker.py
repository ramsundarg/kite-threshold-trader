"""Broker adapters.

KiteBroker   - real orders through pykiteconnect.
PaperBroker  - real Kite prices, simulated instant fills (no orders sent).
DemoBroker   - random-walk prices and simulated fills; needs no credentials.
"""
import math
import random

from strategy import Action, Position, Rule

TERMINAL_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED"}


def marketable_limit(action: Action, ltp: float, buffer_pct: float, tick: float = 0.05) -> float:
    """A LIMIT price a little through the LTP so it fills like a market order,
    but can never fill at a runaway price. Rounded onto the exchange tick."""
    if action is Action.BUY:
        return round(math.ceil(ltp * (1 + buffer_pct / 100) / tick) * tick, 2)
    return round(math.floor(ltp * (1 - buffer_pct / 100) / tick) * tick, 2)


class KiteBroker:
    def __init__(self, kite, product: str, limit_buffer_pct: float = 0.5, tag: str = "thresh"):
        self.kite = kite
        self.product = product            # kite.PRODUCT_MIS (intraday) or kite.PRODUCT_CNC (delivery)
        self.limit_buffer_pct = limit_buffer_pct
        self.tag = tag                    # visible in the Kite order book, max 20 chars

    def ltp(self, symbols):
        data = self.kite.ltp(*symbols)
        return {s: q["last_price"] for s, q in data.items()}

    def position(self, rule: Rule) -> Position:
        """Net quantity + average price for this symbol and product.

        MIS: positions()["net"] is the whole story.
        CNC: shares bought on earlier days sit in holdings() (quantity + t1_quantity);
             shares bought today appear only in positions()["day"] until settlement.
        """
        legs = []  # (qty, avg_price)
        for p in self.kite.positions()["net" if self.product == "MIS" else "day"]:
            if (p["exchange"], p["tradingsymbol"], p["product"]) == (rule.exchange, rule.tradingsymbol, self.product):
                legs.append((p["quantity"], p["average_price"]))

        if self.product == "CNC":
            for h in self.kite.holdings():
                if (h["exchange"], h["tradingsymbol"]) == (rule.exchange, rule.tradingsymbol):
                    legs.append((h["quantity"] + h.get("t1_quantity", 0), h["average_price"]))

        qty = sum(q for q, _ in legs)
        if qty == 0:
            return Position()
        # Cost basis of the open quantity; good enough for the SL/TP check.
        gross = sum(abs(q) for q, _ in legs if q)
        avg = sum(abs(q) * px for q, px in legs if q) / gross
        return Position(qty, avg)

    def place(self, rule: Rule, action: Action, qty: int, ltp: float) -> str:
        k = self.kite
        return k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=rule.exchange,
            tradingsymbol=rule.tradingsymbol,
            transaction_type=k.TRANSACTION_TYPE_BUY if action is Action.BUY else k.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=self.product,
            order_type=k.ORDER_TYPE_LIMIT,
            price=marketable_limit(action, ltp, self.limit_buffer_pct),
            validity=k.VALIDITY_DAY,
            tag=self.tag,
        )

    def order_status(self, order_id: str) -> str:
        return self.kite.order_history(order_id)[-1]["status"]


class PaperBroker:
    """Simulated fills at LTP. Pass a price source (KiteBroker) for live paper trading."""

    def __init__(self, price_source=None):
        self.price_source = price_source
        self.positions = {}   # symbol -> Position
        self.realised = {}    # symbol -> realised P&L
        self._n = 0

    def ltp(self, symbols):
        if self.price_source is None:
            raise RuntimeError("PaperBroker needs a price_source")
        return self.price_source.ltp(symbols)

    def position(self, rule: Rule) -> Position:
        return self.positions.get(rule.symbol, Position())

    def place(self, rule: Rule, action: Action, qty: int, ltp: float) -> str:
        pos = self.position(rule)
        signed = qty if action is Action.BUY else -qty
        new_qty = pos.quantity + signed

        if pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):
            # opening / adding: blend the average price
            avg = (abs(pos.quantity) * pos.average_price + qty * ltp) / abs(new_qty)
        else:
            # reducing / closing: book realised P&L
            closed = min(qty, abs(pos.quantity))
            direction = 1 if pos.quantity > 0 else -1
            self.realised[rule.symbol] = self.realised.get(rule.symbol, 0.0) + direction * closed * (ltp - pos.average_price)
            avg = pos.average_price if new_qty else 0.0

        self.positions[rule.symbol] = Position(new_qty, avg)
        self._n += 1
        return f"PAPER-{self._n}"

    def order_status(self, order_id: str) -> str:
        return "COMPLETE"


class DemoBroker(PaperBroker):
    """Offline: each symbol's price does a random walk starting at its threshold midpoint."""

    def __init__(self, rules, seed=None, vol_pct=1.0):
        super().__init__()
        self.rng = random.Random(seed)
        self.vol = vol_pct / 100
        self.prices = {r.symbol: (r.buy_below + r.sell_above) / 2 for r in rules}

    def ltp(self, symbols):
        for s in symbols:
            self.prices[s] = round(self.prices[s] * (1 + self.rng.gauss(0, self.vol)), 2)
        return {s: self.prices[s] for s in symbols}
