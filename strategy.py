"""Pure decision logic: no broker calls, so it can be unit-tested offline.

Priority on every price update:
  1. If a position is open, risk exits first:
       loss >= stop_loss_pct   -> square off
       gain >= take_profit_pct -> square off
  2. Then the threshold signal:
       price <= buy_below  -> BUY  (open a long, or cover a short)
       price >= sell_above -> SELL (close a long, or open a short if allow_short)
"""
from dataclasses import dataclass
from enum import Enum


class Action(Enum):
    HOLD = "HOLD"
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Rule:
    symbol: str                   # "EXCHANGE:TRADINGSYMBOL", e.g. "NSE:INFY"
    buy_below: float
    sell_above: float
    quantity: int
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 15.0
    allow_short: bool = False     # if flat and price >= sell_above, open a short (MIS only)

    def __post_init__(self):
        if self.buy_below >= self.sell_above:
            raise ValueError(f"{self.symbol}: buy_below must be < sell_above")
        if self.quantity <= 0:
            raise ValueError(f"{self.symbol}: quantity must be positive")

    @property
    def exchange(self) -> str:
        return self.symbol.split(":", 1)[0]

    @property
    def tradingsymbol(self) -> str:
        return self.symbol.split(":", 1)[1]


@dataclass(frozen=True)
class Position:
    quantity: int = 0             # >0 long, <0 short, 0 flat
    average_price: float = 0.0


@dataclass(frozen=True)
class Decision:
    action: Action
    quantity: int = 0
    reason: str = ""


HOLD = Decision(Action.HOLD)


def pnl_pct(pos: Position, ltp: float) -> float:
    """Unrealised P&L of the position in percent, sign-adjusted for shorts."""
    if pos.quantity == 0 or pos.average_price <= 0:
        return 0.0
    move = (ltp - pos.average_price) / pos.average_price * 100.0
    return move if pos.quantity > 0 else -move


def decide(rule: Rule, pos: Position, ltp: float) -> Decision:
    if pos.quantity != 0:
        qty = abs(pos.quantity)
        close = Action.SELL if pos.quantity > 0 else Action.BUY
        pnl = pnl_pct(pos, ltp)

        if pnl <= -rule.stop_loss_pct:
            return Decision(close, qty, f"stop-loss {pnl:+.2f}% (limit -{rule.stop_loss_pct}%)")
        if pnl >= rule.take_profit_pct:
            return Decision(close, qty, f"take-profit {pnl:+.2f}% (target +{rule.take_profit_pct}%)")

        if pos.quantity > 0 and ltp >= rule.sell_above:
            return Decision(Action.SELL, qty, f"signal: {ltp} >= sell_above {rule.sell_above}, closing long ({pnl:+.2f}%)")
        if pos.quantity < 0 and ltp <= rule.buy_below:
            return Decision(Action.BUY, qty, f"signal: {ltp} <= buy_below {rule.buy_below}, covering short ({pnl:+.2f}%)")
        return HOLD

    if ltp <= rule.buy_below:
        return Decision(Action.BUY, rule.quantity, f"signal: {ltp} <= buy_below {rule.buy_below}")
    if ltp >= rule.sell_above and rule.allow_short:
        return Decision(Action.SELL, rule.quantity, f"signal: {ltp} >= sell_above {rule.sell_above}, opening short")
    return HOLD
