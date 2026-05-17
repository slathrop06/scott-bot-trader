"""Premarket movers — pulls overnight % change for our universe via yfinance."""
from __future__ import annotations
import yfinance as yf

from . import config


def premarket_snapshot() -> list[dict]:
    """Return [{symbol, prev_close, premarket_price, pct_change, volume}, ...]
    sorted by |pct_change| descending.

    Uses 1-day daily history for prev_close and fast_info for the latest tick
    (which includes pre/post-market quotes during extended hours).
    """
    rows: list[dict] = []
    tickers = yf.Tickers(" ".join(config.TRADABLE_UNIVERSE))

    for sym in config.TRADABLE_UNIVERSE:
        try:
            t = tickers.tickers[sym]
            info = t.fast_info
            # NOTE: fast_info.get() is broken — returns None even when attr exists.
            # Use attribute access via getattr instead.
            prev_close = float(getattr(info, "previous_close", 0) or 0)
            last = float(getattr(info, "last_price", 0) or 0)
            if prev_close <= 0 or last <= 0:
                continue
            pct = (last - prev_close) / prev_close * 100
            rows.append({
                "symbol": sym,
                "prev_close": round(prev_close, 2),
                "premarket_price": round(last, 2),
                "pct_change": round(pct, 2),
                "volume": int(getattr(info, "last_volume", 0) or 0),
            })
        except Exception as e:
            print(f"[data] skip {sym}: {e}")
            continue

    rows.sort(key=lambda r: abs(r["pct_change"]), reverse=True)
    return rows


def top_movers(n: int = 10) -> list[dict]:
    return premarket_snapshot()[:n]
