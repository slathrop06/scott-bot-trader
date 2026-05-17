"""Earnings-date awareness — block trades within 1 trading day of earnings.

Earnings announcements cause large overnight gaps that turn day trading into
a coin flip. Even with a good catalyst read, the post-earnings move can
reverse violently. Strategy: don't hold any position into earnings.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
import yfinance as yf

from . import config


CACHE_PATH = config.DATA_DIR / "earnings_cache.json"
CACHE_TTL_HOURS = 24


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _save_cache(cache: dict) -> None:
    config.DATA_DIR.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, default=str))


def _is_fresh(entry: dict) -> bool:
    try:
        ts = datetime.fromisoformat(entry["cached_at"])
        return datetime.now(timezone.utc) - ts < timedelta(hours=CACHE_TTL_HOURS)
    except (KeyError, ValueError):
        return False


def next_earnings_date(symbol: str, cache: dict | None = None) -> date | None:
    """Return the next upcoming earnings date for symbol, or None."""
    cache = cache if cache is not None else _load_cache()
    entry = cache.get(symbol)
    if entry and _is_fresh(entry):
        return date.fromisoformat(entry["next"]) if entry.get("next") else None

    next_date: date | None = None
    try:
        t = yf.Ticker(symbol)
        ed = t.earnings_dates
        if ed is not None and not ed.empty:
            future = ed[ed.index > datetime.now(ed.index.tz)]
            if not future.empty:
                # earnings_dates is sorted descending — the last future entry is the soonest.
                next_date = future.index[-1].date()
    except Exception as e:
        print(f"[earnings] {symbol}: lookup failed: {e}")

    cache[symbol] = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "next": next_date.isoformat() if next_date else None,
    }
    return next_date


def days_until_earnings(symbol: str, cache: dict | None = None) -> int | None:
    nxt = next_earnings_date(symbol, cache=cache)
    if nxt is None:
        return None
    return (nxt - date.today()).days


def filter_out_earnings_risk(symbols: list[str], min_days_buffer: int = 1) -> tuple[list[str], dict[str, int]]:
    """Split symbols into (safe, blocked_with_days_until_earnings).
    A stock is blocked if earnings are within `min_days_buffer` trading days.
    """
    cache = _load_cache()
    safe: list[str] = []
    blocked: dict[str, int] = {}

    for s in symbols:
        days = days_until_earnings(s, cache=cache)
        if days is not None and 0 <= days <= min_days_buffer:
            blocked[s] = days
        else:
            safe.append(s)

    _save_cache(cache)
    return safe, blocked
