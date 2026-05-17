"""Alpaca news fetching — overnight catalysts for premarket movers."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from . import config


_client: NewsClient | None = None


def client() -> NewsClient:
    global _client
    if _client is None:
        _client = NewsClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    return _client


def overnight_news(
    symbols: list[str],
    lookback_hours: int = 16,
    max_per_symbol: int = 3,
) -> dict[str, list[dict]]:
    """Pull recent news for each symbol. Returns {symbol: [{headline, summary, source, url, ts}, ...]}.

    Lookback defaults to 16h — covers after-hours + overnight + premarket.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=lookback_hours)

    req = NewsRequest(
        symbols=",".join(symbols),
        start=start,
        end=end,
        limit=50,
        sort="desc",
        exclude_contentless=True,
    )

    try:
        resp = client().get_news(req)
        articles = resp.data.get("news", [])
    except Exception as e:
        print(f"[news] fetch failed: {e}")
        return {s: [] for s in symbols}

    by_symbol: dict[str, list[dict]] = {s: [] for s in symbols}
    for n in articles:
        for s in (n.symbols or []):
            if s in by_symbol and len(by_symbol[s]) < max_per_symbol:
                by_symbol[s].append({
                    "headline": n.headline,
                    "summary": (n.summary or "")[:240],
                    "source": n.source,
                    "url": n.url,
                    "ts": n.created_at.isoformat() if n.created_at else "",
                })
    return by_symbol
