"""Account snapshots + round-trip extraction from Alpaca.

The dashboard needs to show portfolio balance and per-trade entry/exit prices
without anyone hitting the Alpaca console. This module pulls both from the
trading API and shapes them for trades.json / account.json.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

from . import alpaca_client


# Alpaca paper accounts start at $100k. Used to compute "all-time return".
PAPER_STARTING_EQUITY = 100_000.0


def current_snapshot() -> dict:
    acct = alpaca_client.account()
    positions = alpaca_client.positions()
    equity = float(acct.equity)
    return {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "equity": equity,
        "last_equity": float(acct.last_equity),
        "cash": float(acct.cash),
        "buying_power": float(acct.buying_power),
        "all_time_return_pct": round((equity - PAPER_STARTING_EQUITY) / PAPER_STARTING_EQUITY * 100, 4),
        "positions": [
            {
                "symbol": p.symbol,
                "qty": int(float(p.qty)),
                "avg_entry_price": round(float(p.avg_entry_price), 4),
                "current_price": round(float(p.current_price), 4) if p.current_price else None,
                "market_value": round(float(p.market_value), 2),
                "unrealized_pl": round(float(p.unrealized_pl), 2),
                "unrealized_pl_pct": round(float(p.unrealized_plpc) * 100, 4),
            }
            for p in positions
        ],
    }


def _side(o) -> str:
    s = o.side
    return s if isinstance(s, str) else getattr(s, "value", str(s))


def todays_round_trips(now: datetime | None = None) -> list[dict]:
    """Today's filled orders paired into entry/exit round-trips.

    Pairs each filled BUY with the next filled SELL of the same symbol in
    time order. Unmatched BUYs are open positions; unmatched SELLs are
    short-cover scenarios we don't trade. Exit reason is inferred:
      - bracket child order (parent had legs) → 'bracket' (stop or take)
      - standalone sell                       → 'flatten'
    """
    now = now or datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    req = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        after=today_start,
        limit=500,
        nested=True,  # parents include their legs (stop/take); else .legs is None
    )
    parents = alpaca_client.trading().get_orders(filter=req)

    bracket_child_ids: set[str] = set()
    orders: list = []
    for p in parents:
        orders.append(p)
        for leg in (getattr(p, "legs", None) or []):
            bracket_child_ids.add(str(leg.id))
            orders.append(leg)

    fills: list[dict] = []
    for o in orders:
        if o.status != "filled" and getattr(o.status, "value", "") != "filled":
            continue
        price = getattr(o, "filled_avg_price", None)
        if price in (None, ""):
            continue
        fills.append({
            "ts": o.filled_at,
            "symbol": o.symbol,
            "side": _side(o),
            "price": float(price),
            "qty": float(o.filled_qty),
            "order_id": str(o.id),
            "is_bracket_child": str(o.id) in bracket_child_ids,
        })
    fills.sort(key=lambda f: f["ts"])

    pending_buys: dict[str, list[dict]] = {}
    trades: list[dict] = []
    for f in fills:
        if f["side"] == "buy":
            pending_buys.setdefault(f["symbol"], []).append(f)
            continue
        buys = pending_buys.get(f["symbol"])
        if not buys:
            continue
        buy = buys.pop(0)
        qty = f["qty"]
        entry = buy["price"]
        exit_price = f["price"]
        pnl = (exit_price - entry) * qty
        pnl_pct = ((exit_price - entry) / entry * 100) if entry else 0.0
        trades.append({
            "symbol": f["symbol"],
            "qty": int(qty),
            "entry_price": round(entry, 4),
            "entry_ts": buy["ts"].isoformat().replace("+00:00", "Z") if buy["ts"] else None,
            "entry_order_id": buy["order_id"],
            "exit_price": round(exit_price, 4),
            "exit_ts": f["ts"].isoformat().replace("+00:00", "Z") if f["ts"] else None,
            "exit_order_id": f["order_id"],
            "exit_reason": "bracket" if f["is_bracket_child"] else "flatten",
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
        })

    # Surface any unmatched buys (still open) for transparency.
    open_legs = []
    for sym, buys in pending_buys.items():
        for buy in buys:
            open_legs.append({
                "symbol": sym,
                "qty": int(buy["qty"]),
                "entry_price": round(buy["price"], 4),
                "entry_ts": buy["ts"].isoformat().replace("+00:00", "Z") if buy["ts"] else None,
                "entry_order_id": buy["order_id"],
            })
    return {"trades": trades, "open_positions": open_legs}
