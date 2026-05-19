"""Thin wrapper over alpaca-py — just the calls we actually use."""
from __future__ import annotations
import time
import uuid
from decimal import Decimal
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
    ClosePositionRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

from . import config


_trading: TradingClient | None = None
_data: StockHistoricalDataClient | None = None


def trading() -> TradingClient:
    global _trading
    if _trading is None:
        _trading = TradingClient(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            paper="paper" in config.ALPACA_BASE_URL,
        )
    return _trading


def data() -> StockHistoricalDataClient:
    global _data
    if _data is None:
        _data = StockHistoricalDataClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
        )
    return _data


def account():
    return trading().get_account()


def positions():
    return trading().get_all_positions()


def market_is_open() -> bool:
    return trading().get_clock().is_open


def latest_price(symbol: str) -> float:
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = data().get_stock_latest_quote(req)[symbol]
    # Midpoint of bid/ask. Falls back to ask if bid missing.
    bid = float(quote.bid_price or 0)
    ask = float(quote.ask_price or 0)
    if bid and ask:
        return (bid + ask) / 2
    return ask or bid


def submit_bracket(
    symbol: str,
    notional_usd: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> dict:
    """Submit a bracket order: market entry + stop + take-profit.

    Uses fractional shares (notional). Returns the order dict.
    NOTE: Alpaca bracket orders require whole-share qty, not notional — so we
    compute qty from latest price and round down.
    """
    price = latest_price(symbol)
    if price <= 0:
        raise RuntimeError(f"No price for {symbol}")

    qty = int(notional_usd // price)
    if qty < 1:
        raise RuntimeError(
            f"{symbol} @ ${price:.2f} too expensive for ${notional_usd:.2f} budget"
        )

    stop_price = round(price * (1 - stop_loss_pct / 100), 2)
    take_price = round(price * (1 + take_profit_pct / 100), 2)

    # client_order_id makes the submission idempotent on the Alpaca side: if
    # the SDK retries after a transient network blip and the order was already
    # accepted server-side, Alpaca rejects the duplicate instead of doubling
    # the position. (We suspect the 2x mystery positions on 2026-05-19 were
    # exactly this case during the crashed 14:06 morning run.)
    coid = f"sbt-{symbol}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        client_order_id=coid,
        take_profit=TakeProfitRequest(limit_price=take_price),
        stop_loss=StopLossRequest(stop_price=stop_price),
    )
    order = trading().submit_order(req)
    return {
        "symbol": symbol,
        "qty": qty,
        "entry_price_est": price,
        "stop_price": stop_price,
        "take_price": take_price,
        "order_id": str(order.id),
    }


def close_all_positions() -> list:
    """Liquidate everything. Used at 3:55pm to enforce same-day exit."""
    return trading().close_all_positions(cancel_orders=True)
