"""Alternative-data signal aggregator.

Pulls signals that retail bots usually ignore because they require parsing
unstructured data — the kind of thing an LLM is uniquely good at synthesizing:

  1. Insider transactions (SEC Form 4, via Finnhub) — strongest documented edge.
     Cluster buys (3+ insiders buying in same week) have ~12% annualized alpha
     in academic literature (Cohen/Malloy/Pomorski 2012).
  2. WSB mentions (ApeWisdom) — retail momentum / unusual interest spikes.
  3. Stocktwits sentiment — bullish/bearish message ratios.
  4. Analyst recommendations (Finnhub) — slow-moving but show institutional view.

Congressional trades intentionally excluded — all free sources have been
shut down or moved to paid (Quiver Quant ~$15/mo). Add by setting
QUIVER_API_KEY in .env and uncommenting the relevant code below.

All functions are best-effort: any signal that fails just returns empty,
so missing data never blocks the picker.
"""
from __future__ import annotations
from datetime import date, timedelta
import requests

from . import config, congress


USER_AGENT = "scott-bot-trader/1.0 (research)"
HTTP_TIMEOUT = 15


# ============================================================
# Insider transactions (Finnhub free tier)
# ============================================================

def insider_transactions(symbols: list[str], lookback_days: int = 30) -> dict[str, list[dict]]:
    """Per-symbol recent Form 4 filings. Returns {symbol: [{name, change, filingDate, type}]}.
    `change` > 0 = buy (bullish), < 0 = sell (bearish/neutral)."""
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    if not config.FINNHUB_API_KEY:
        return out

    end = date.today()
    start = end - timedelta(days=lookback_days)
    for s in symbols:
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/stock/insider-transactions",
                params={"symbol": s, "from": str(start), "to": str(end), "token": config.FINNHUB_API_KEY},
                timeout=HTTP_TIMEOUT,
            )
            if not r.ok:
                continue
            for tx in (r.json().get("data") or []):
                out[s].append({
                    "name": tx.get("name", ""),
                    "change": tx.get("change", 0),
                    "filingDate": tx.get("filingDate", ""),
                    "transactionDate": tx.get("transactionDate", ""),
                    "transactionPrice": tx.get("transactionPrice", 0),
                    "isDerivative": tx.get("isDerivative", False),
                })
        except requests.RequestException:
            continue
    return out


def summarize_insider(transactions: list[dict]) -> dict:
    """Reduce raw insider records to a useful summary."""
    # Exclude derivative transactions (options exercises) from buy/sell counts —
    # those are usually compensation-related, not conviction signals.
    real = [tx for tx in transactions if not tx.get("isDerivative")]
    buys = [tx for tx in real if tx["change"] > 0]
    sells = [tx for tx in real if tx["change"] < 0]
    unique_buyers = {tx["name"] for tx in buys}
    unique_sellers = {tx["name"] for tx in sells}
    return {
        "n_buys": len(buys),
        "n_sells": len(sells),
        "unique_buyers": len(unique_buyers),
        "unique_sellers": len(unique_sellers),
        "cluster_buy": len(unique_buyers) >= 3,  # 3+ different insiders bought
        "total_shares_bought": sum(tx["change"] for tx in buys),
        "total_shares_sold": -sum(tx["change"] for tx in sells),
        "buyer_names": list(unique_buyers)[:5],
    }


# ============================================================
# WSB chatter (ApeWisdom, free)
# ============================================================

def wsb_mentions(top_n: int = 50) -> dict[str, dict]:
    """Pull top mentioned tickers from WSB in the last 24h. Returns {ticker: {...}}."""
    try:
        r = requests.get(
            "https://apewisdom.io/api/v1.0/filter/wallstreetbets",
            headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT,
        )
        if not r.ok:
            return {}
        d = r.json()
        out = {}
        for row in (d.get("results") or [])[:top_n]:
            t = row.get("ticker")
            if not t:
                continue
            out[t] = {
                "mentions": row.get("mentions", 0),
                "upvotes": row.get("upvotes", 0),
                "rank": row.get("rank", 999),
                "rank_24h_ago": row.get("rank_24h_ago", 999),
                "mentions_24h_ago": row.get("mentions_24h_ago", 0),
            }
        return out
    except requests.RequestException:
        return {}


def wsb_signal_for(ticker: str, wsb_data: dict) -> dict:
    info = wsb_data.get(ticker)
    if not info:
        return {"mentioned": False}
    # Mention growth — useful indicator of unusual interest spike
    prev = info.get("mentions_24h_ago", 0) or 1
    growth_ratio = info["mentions"] / prev if prev > 0 else 1.0
    return {
        "mentioned": True,
        "mentions": info["mentions"],
        "rank": info["rank"],
        "growth_ratio": round(growth_ratio, 2),
        "unusual_spike": growth_ratio >= 2.0,  # 2x mentions in 24h = real spike
    }


# ============================================================
# Stocktwits sentiment (free, requires user-agent header)
# ============================================================

def stocktwits_sentiment(symbol: str, max_messages: int = 30) -> dict:
    """Return {n_messages, bullish, bearish, ratio} for a ticker."""
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
            headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT,
            params={"limit": max_messages},
        )
        if not r.ok:
            return {"n_messages": 0, "bullish": 0, "bearish": 0, "ratio": None}
        msgs = (r.json().get("messages") or [])[:max_messages]
        bullish = bearish = 0
        for m in msgs:
            ent = m.get("entities") or {}
            sentiment = (ent.get("sentiment") or {}).get("basic") if isinstance(ent.get("sentiment"), dict) else None
            if sentiment == "Bullish":
                bullish += 1
            elif sentiment == "Bearish":
                bearish += 1
        total_tagged = bullish + bearish
        ratio = bullish / total_tagged if total_tagged > 0 else None
        return {
            "n_messages": len(msgs),
            "bullish": bullish,
            "bearish": bearish,
            "ratio": round(ratio, 2) if ratio is not None else None,
        }
    except requests.RequestException:
        return {"n_messages": 0, "bullish": 0, "bearish": 0, "ratio": None}


# ============================================================
# Analyst recommendations (Finnhub free tier)
# ============================================================

def analyst_recommendations(symbol: str) -> dict | None:
    """Latest analyst breakdown. Returns {strongBuy, buy, hold, sell, strongSell, period}."""
    if not config.FINNHUB_API_KEY:
        return None
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/recommendation",
            params={"symbol": symbol, "token": config.FINNHUB_API_KEY},
            timeout=HTTP_TIMEOUT,
        )
        if not r.ok:
            return None
        rows = r.json()
        if not rows:
            return None
        latest = rows[0]
        total = sum(latest.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
        bullish = latest.get("strongBuy", 0) + latest.get("buy", 0)
        bullish_pct = (bullish / total * 100) if total else 0
        return {
            "period": latest.get("period", ""),
            "strongBuy": latest.get("strongBuy", 0),
            "buy": latest.get("buy", 0),
            "hold": latest.get("hold", 0),
            "sell": latest.get("sell", 0),
            "strongSell": latest.get("strongSell", 0),
            "bullish_pct": round(bullish_pct, 1),
        }
    except requests.RequestException:
        return None


# ============================================================
# Top-level: build per-symbol smart-money snapshot
# ============================================================

def smart_money_snapshot(symbols: list[str]) -> dict[str, dict]:
    """For each symbol, return the alt-data picture: insider, congressional,
    WSB, Stocktwits, analysts.

    Single entry point used by the picker. Failures degrade gracefully — missing
    data just becomes empty/None, the picker still works."""
    print(f"[altdata] fetching insider txns for {len(symbols)} symbols...")
    insider = insider_transactions(symbols, lookback_days=30)
    print(f"[altdata] fetching House congressional trades (last 21d)...")
    cong = congress.recent_congressional_trades(symbols, days=21)
    print(f"[altdata] fetching WSB mentions (top 50)...")
    wsb = wsb_mentions(top_n=50)

    out: dict[str, dict] = {}
    for s in symbols:
        print(f"[altdata]   {s}: stocktwits + analyst...")
        out[s] = {
            "insider": summarize_insider(insider.get(s, [])),
            "congress": congress.summarize_congress(cong.get(s, [])),
            "wsb": wsb_signal_for(s, wsb),
            "stocktwits": stocktwits_sentiment(s, max_messages=30),
            "analysts": analyst_recommendations(s),
        }
    return out


def signal_strength(snapshot: dict) -> dict:
    """Compute a simple overall signal score (0-10) from the snapshot — used
    by the picker prompt to highlight high-conviction candidates."""
    score = 0
    reasons = []

    ins = snapshot.get("insider", {})
    if ins.get("cluster_buy"):
        score += 4
        reasons.append(f"insider cluster buy ({ins['unique_buyers']} buyers)")
    elif ins.get("n_buys", 0) > ins.get("n_sells", 0):
        score += 1
        reasons.append("net insider buying")
    elif ins.get("n_sells", 0) > ins.get("n_buys", 0) * 2:
        score -= 2
        reasons.append(f"heavy insider selling ({ins['n_sells']} sells)")

    # Congressional signal — highest-conviction in our stack because it's the
    # data source most retail bots can't easily access.
    cong = snapshot.get("congress", {})
    if cong.get("cluster_buy"):
        score += 3
        names = ", ".join(cong.get("buyer_names", [])[:2])
        reasons.append(f"House cluster buy ({cong['n_buys']} buys by {names})")
    elif cong.get("n_buys", 0) > 0 and cong.get("n_sells", 0) == 0:
        score += 2
        reasons.append(f"House net buying ({cong['n_buys']} buys by {', '.join(cong['buyer_names'][:1])})")
    elif cong.get("n_sells", 0) > cong.get("n_buys", 0):
        score -= 1
        reasons.append(f"House net selling ({cong['n_sells']} sells)")

    wsb = snapshot.get("wsb", {})
    if wsb.get("unusual_spike"):
        score += 2
        reasons.append(f"WSB chatter {wsb['growth_ratio']}x normal")
    elif wsb.get("mentioned") and wsb.get("rank", 999) <= 10:
        score += 1
        reasons.append(f"top-{wsb['rank']} WSB ticker")

    st = snapshot.get("stocktwits", {})
    if st.get("n_messages", 0) >= 10 and st.get("ratio") is not None:
        if st["ratio"] >= 0.75:
            score += 1
            reasons.append(f"stocktwits {int(st['ratio']*100)}% bullish")
        elif st["ratio"] <= 0.25:
            score -= 1
            reasons.append(f"stocktwits {int(st['ratio']*100)}% bullish (bearish)")

    an = snapshot.get("analysts")
    if an and an.get("bullish_pct", 0) >= 70:
        score += 1
        reasons.append(f"{an['bullish_pct']:.0f}% analyst buy")

    return {"score": score, "reasons": reasons}
