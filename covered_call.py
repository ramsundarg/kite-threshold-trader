"""Covered-call decision logic: no broker calls, so it can be unit-tested offline.

You hold shares of an F&O stock; the strategy keeps one short OTM call per lot
of shares you hold:

  1. Stock stop-loss: stock down >= stock_stop_loss_pct from your average price
       -> buy back the call first, then (next cycle) sell the covered shares
          (lots * lot_size, never your whole holding) and stop managing the stock.
       The call is always closed before the shares, so it is never naked.
  2. Short calls larger than the shares cover (you sold shares elsewhere)
       -> buy back the excess.
  3. <= roll_days_before_expiry days left -> buy back (next cycle writes the next month).
  4. Premium captured >= take_profit_pct -> buy back early (next cycle writes a new one).
  5. No short call and at least one lot of shares
       -> sell the nearest-expiry call at least min_days_to_expiry out,
          with the lowest strike >= spot * (1 + otm_pct%).

decide_cc() returns one order per cycle; the runner waits for it to fill and
re-evaluates, so multi-step exits (call first, then shares) stay in order.
"""
from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

from strategy import Action


@dataclass(frozen=True)
class CCRule:
    underlying: str                          # "NSE:INFY" - the cash-market stock you hold
    lots: int = 1                            # max lots to write (capped by shares held)
    otm_pct: float = 5.0                     # strike at least this % above spot
    min_days_to_expiry: int = 20             # skip expiries closer than this when writing
    max_days_to_expiry: int = 60
    roll_days_before_expiry: int = 5         # buy back this many days before expiry
    take_profit_pct: float = 80.0            # buy back once this % of the premium is captured
    stock_stop_loss_pct: Optional[float] = 5.0   # None disables the stock stop

    def __post_init__(self):
        if self.lots <= 0:
            raise ValueError(f"{self.underlying}: lots must be positive")
        if self.min_days_to_expiry > self.max_days_to_expiry:
            raise ValueError(f"{self.underlying}: min_days_to_expiry > max_days_to_expiry")
        if self.roll_days_before_expiry >= self.min_days_to_expiry:
            raise ValueError(f"{self.underlying}: roll_days_before_expiry must be < min_days_to_expiry")

    @property
    def exchange(self) -> str:
        return self.underlying.split(":", 1)[0]

    @property
    def tradingsymbol(self) -> str:
        return self.underlying.split(":", 1)[1]


@dataclass(frozen=True)
class OptionContract:
    tradingsymbol: str                       # e.g. "INFY26OCT1600CE"
    expiry: date
    strike: float
    lot_size: int


@dataclass(frozen=True)
class ShortCall:
    contract: OptionContract
    quantity: int                            # shares short (positive), a multiple of lot_size
    average_price: float                     # premium received per share
    ltp: float                               # current premium


@dataclass(frozen=True)
class CCState:
    spot: float
    shares: int                              # shares held (holdings + today's CNC buys)
    stock_avg: float
    short: Optional[ShortCall] = None


@dataclass(frozen=True)
class Order:
    exchange: str                            # "NSE" for the stock, "NFO" for the option
    tradingsymbol: str
    side: Action
    quantity: int
    product: str                             # "CNC" stock, "NRML" option
    reason: str
    final: bool = False                      # once filled, stop managing this underlying


def select_call(chain: Sequence[OptionContract], spot: float, rule: CCRule, today: date) -> Optional[OptionContract]:
    """Nearest eligible expiry, then the lowest strike at least otm_pct above spot."""
    eligible = [c for c in chain if rule.min_days_to_expiry <= (c.expiry - today).days <= rule.max_days_to_expiry]
    if not eligible:
        return None
    expiry = min(c.expiry for c in eligible)
    floor = spot * (1 + rule.otm_pct / 100)
    strikes = [c for c in eligible if c.expiry == expiry and c.strike >= floor]
    return min(strikes, key=lambda c: c.strike) if strikes else None


def _buy_back(short: ShortCall, qty: int, reason: str) -> Order:
    return Order("NFO", short.contract.tradingsymbol, Action.BUY, qty, "NRML", reason)


def decide_cc(rule: CCRule, st: CCState, today: date, chain: Sequence[OptionContract]) -> Optional[Order]:
    short = st.short
    lot = short.contract.lot_size if short else min((c.lot_size for c in chain), default=0)
    if not short and (not lot or st.shares < lot):
        return None                          # less than one lot: nothing to cover, nothing to manage

    # 1. stock stop-loss: close the call, then the covered shares, then stop
    if st.shares and st.stock_avg > 0 and rule.stock_stop_loss_pct is not None:
        pnl = (st.spot - st.stock_avg) / st.stock_avg * 100
        if pnl <= -rule.stock_stop_loss_pct:
            if short:
                return _buy_back(short, short.quantity, f"stock stop-loss {pnl:+.2f}%: closing call before selling shares")
            qty = min(st.shares, rule.lots * lot)
            return Order(rule.exchange, rule.tradingsymbol, Action.SELL, qty, "CNC",
                         f"stock stop-loss {pnl:+.2f}% (limit -{rule.stock_stop_loss_pct}%)", final=True)

    if short:
        covered = min(rule.lots, st.shares // lot) * lot
        # 2. never stay short more calls than the shares cover
        if short.quantity > covered:
            return _buy_back(short, short.quantity - covered,
                             f"only {st.shares} shares cover {covered}; buying back uncovered calls")
        # 3. roll before expiry
        dte = (short.contract.expiry - today).days
        if dte <= rule.roll_days_before_expiry:
            return _buy_back(short, short.quantity, f"roll: {dte} days to expiry")
        # 4. take profit on premium decay
        if short.average_price > 0:
            captured = (short.average_price - short.ltp) / short.average_price * 100
            if captured >= rule.take_profit_pct:
                return _buy_back(short, short.quantity,
                                 f"take-profit: captured {captured:.0f}% of {short.average_price:.2f} premium")
        return None

    # 5. write a new call
    c = select_call(chain, st.spot, rule, today)
    if c is None:
        return None
    lots = min(rule.lots, st.shares // c.lot_size)
    if lots == 0:
        return None
    otm = (c.strike / st.spot - 1) * 100
    return Order("NFO", c.tradingsymbol, Action.SELL, lots * c.lot_size, "NRML",
                 f"write {lots} lot(s) {c.strike:g} CE exp {c.expiry} ({otm:.1f}% OTM, spot {st.spot:.2f})")
