"""Put the same rules on Zerodha's servers as GTT orders, so no PC has to stay on.

GTT (Good Till Triggered) orders are watched by Zerodha for up to a year and only
work for delivery (CNC) equity.

    python gtt_setup.py entry   NSE:INFY --buy-below 1400 --qty 5
    python gtt_setup.py protect NSE:INFY --sell-above 1550      # after the buy fills
    python gtt_setup.py list
    python gtt_setup.py delete 123456

'protect' places an OCO (one-cancels-other) GTT on the shares you hold:
    lower leg: stop-loss at average_price * (1 - 5%)
    upper leg: take-profit at min(sell_above, average_price * (1 + 15%))
"""
import argparse

from broker import marketable_limit
from strategy import Action
from trader import make_kite


def tick(x, step=0.05):
    return round(round(x / step) * step, 2)


def last_price(kite, symbol, given):
    return given if given else kite.ltp([symbol])[symbol]["last_price"]


def entry(kite, a):
    exch, sym = a.symbol.split(":")
    trigger = tick(a.buy_below)
    gid = kite.place_gtt(
        trigger_type=kite.GTT_TYPE_SINGLE, tradingsymbol=sym, exchange=exch,
        trigger_values=[trigger], last_price=last_price(kite, a.symbol, a.last_price),
        orders=[{"transaction_type": kite.TRANSACTION_TYPE_BUY, "quantity": a.qty,
                 "order_type": kite.ORDER_TYPE_LIMIT, "product": kite.PRODUCT_CNC,
                 "price": marketable_limit(Action.BUY, trigger, a.buffer)}],
    )
    print(f"GTT {gid}: BUY {a.qty} {a.symbol} when price falls to {trigger}")


def protect(kite, a):
    exch, sym = a.symbol.split(":")
    h = next((h for h in kite.holdings() if (h["exchange"], h["tradingsymbol"]) == (exch, sym)), None)
    qty = (h["quantity"] + h.get("t1_quantity", 0)) if h else 0
    if not qty:
        raise SystemExit(f"No holding in {a.symbol} yet (a buy made today shows up after settlement).")
    avg = h["average_price"]
    sl = tick(avg * (1 - a.stop_loss_pct / 100))
    tp = tick(min(avg * (1 + a.take_profit_pct / 100), a.sell_above or float("inf")))
    sell = lambda px: {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
                       "order_type": kite.ORDER_TYPE_LIMIT, "product": kite.PRODUCT_CNC,
                       "price": marketable_limit(Action.SELL, px, a.buffer)}
    gid = kite.place_gtt(
        trigger_type=kite.GTT_TYPE_OCO, tradingsymbol=sym, exchange=exch,
        trigger_values=[sl, tp], last_price=last_price(kite, a.symbol, a.last_price),
        orders=[sell(sl), sell(tp)],
    )
    print(f"GTT {gid}: {qty} {a.symbol} @ avg {avg:.2f} -> stop-loss {sl}, take-profit {tp}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("entry"); e.add_argument("symbol"); e.add_argument("--buy-below", type=float, required=True)
    e.add_argument("--qty", type=int, required=True)
    p = sub.add_parser("protect"); p.add_argument("symbol"); p.add_argument("--sell-above", type=float)
    p.add_argument("--stop-loss-pct", type=float, default=5.0); p.add_argument("--take-profit-pct", type=float, default=15.0)
    for s in (e, p):
        s.add_argument("--last-price", type=float, help="current price, if your API plan has no quotes")
        s.add_argument("--buffer", type=float, default=0.5, help="limit price %% beyond the trigger")
    sub.add_parser("list")
    d = sub.add_parser("delete"); d.add_argument("gtt_id", type=int)
    a = ap.parse_args()

    kite = make_kite()
    if a.cmd == "entry":
        entry(kite, a)
    elif a.cmd == "protect":
        protect(kite, a)
    elif a.cmd == "list":
        for g in kite.get_gtts():
            c = g["condition"]
            print(g["id"], g["status"], g["type"], f'{c["exchange"]}:{c["tradingsymbol"]}', c["trigger_values"])
    else:
        kite.delete_gtt(a.gtt_id)
        print("deleted", a.gtt_id)


if __name__ == "__main__":
    main()
