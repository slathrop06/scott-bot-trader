"""Claude-powered stock picker — smart-money aware.

Takes premarket data + overnight news + alt-data signals (insider trades,
WSB chatter, Stocktwits sentiment, analyst recs). Claude must articulate
WHICH signals support each pick — it can't pick on price alone.
"""
from __future__ import annotations
import json
from anthropic import Anthropic

from . import config, altdata


_client: Anthropic | None = None


def client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


SYSTEM_LOOSE = """You are a day-trading analyst for a small paper-trading bot.
Today's mandate: FIND ACTION. The strict prompt was too conservative — it
returned zero picks on tradable days. This mode picks the BEST available
setup even when no candidate is a 10/10.

You receive price + news + alt-data signals for liquid US stocks. The
alt-data is the edge — price + news is in everyone's training set, but the
combination of insider activity + social chatter + analyst views + price
action is something only a thoughtful synthesis can exploit.

Strategy guardrails (unchanged):
- LONG ONLY. No shorts, no options, no leverage.
- Each pick gets a bracket order: take-profit +2%, stop-loss -1%.
- Weekly target is +1.5% on total equity.
- Pick 1 to {max_picks} trades. Stand down (empty) ONLY if every candidate
  has a clear disqualifier (e.g., earnings same day, alt-data is heavily
  negative, price move contradicts every signal).

What makes a VALID pick — EITHER condition is sufficient:
  A. STRONG CATALYST alone:
     - Specific news event (analyst upgrade, product launch, earnings beat,
       contract win) with premarket/intraday gap up
     - OR relative strength (up while market down) with no contradicting news
     - OR post-earnings drift (yesterday's positive print, gap continues)
  B. STRONG SMART-MONEY alone (one or more):
     - House cluster buy (2+ congresspeople in last 21d)
     - Insider cluster buy (3+ insiders in last 30d)
     - Analyst conviction >70% buy/strong-buy
     - WSB chatter >2x normal AND rank in top 20
     - Stocktwits sentiment >70% bullish on >10 messages

REJECT picks where:
  - Earnings within 1 trading day (overnight gap risk)
  - Heavy insider OR congressional selling (3:1+ ratio)
  - Stocktwits sentiment <30% bullish on >10 messages
  - Stop level is within typical intraday noise (penny stocks)

PRIORITIZE picks where MULTIPLE conditions stack (both catalyst + smart
money is still preferred, just not required).

Output STRICT JSON only (no prose, no markdown fences):
{{
  "picks": [
    {{
      "symbol": "AAPL",
      "confidence": "high|medium|low",
      "catalyst": "Specific news/price-action driver — be concrete",
      "smart_money_signals": "Which alt-data signals support this (cite numbers)",
      "rationale": "Why this is the best setup available — one sentence"
    }}
  ],
  "market_take": "One sentence on overall tone",
  "skipped_movers": ["TSLA: heavy insider selling — skip", ...]
}}
"""


SYSTEM = """You are a day-trading analyst for a small paper-trading bot.

You receive premarket data, overnight news, AND alternative-data signals
for a fixed universe of liquid US stocks. The alt-data is the edge —
price + news is in everyone's training set, but the combination of
insider activity + social chatter + analyst views + price action is
something only a thoughtful synthesis can exploit.

Strategy guardrails:
- LONG ONLY. No shorts, no options, no leverage.
- Each pick gets a bracket order: take-profit +2%, stop-loss -1%.
- Weekly target is +1.5% on total equity.
- Pick 0 to {max_picks} trades. Empty is a fine answer.

What constitutes a VALID pick — it must have BOTH:
  1. A real price catalyst (premarket gap with clear news reason), AND
  2. Smart-money confirmation from at least ONE of:
     - **House cluster buy** (2+ different congresspeople bought in last 21d) — strongest signal
     - **House single significant buy** ($15k+ position by one congressperson)
     - Insider cluster buying (3+ insiders bought in the last 30d)
     - Strong analyst conviction (>70% buy/strong-buy)
     - Unusual WSB chatter spike (>2x normal mentions)
     - Stocktwits sentiment >75% bullish on >10 messages

REJECT picks where:
  - News headline is vague/generic (Chamath commentary, market overview)
  - Heavy insider OR congressional selling
  - Stocktwits sentiment is <30% bullish (crowd is bearish)
  - The price move contradicts the alt-data picture

WEIGHT carefully: congressional and insider data have a LAG (30-45 days for
House PTRs, 2 days for Form 4). A 30-day-old senator buy is still meaningful
as background conviction but the trade catalyst should be present-day news.

Be highly skeptical. The default answer is NO PICK. Force yourself to
articulate a thesis for each pick that ties price, news, AND alt-data
together. If you can't, skip it.

Output STRICT JSON only (no prose, no markdown fences):
{{
  "picks": [
    {{
      "symbol": "AAPL",
      "confidence": "high|medium|low",
      "catalyst": "Specific news event driving the price move",
      "smart_money_signals": "Which alt-data signals confirm this (cite the numbers)",
      "rationale": "How catalyst + smart money + price come together — one sentence"
    }}
  ],
  "market_take": "One sentence on overall tone today",
  "skipped_movers": ["TSLA: insider selling 3:1 vs buying, despite +4% premarket — skip", ...]
}}
"""


def _format_candidates(
    premarket: list[dict],
    news_by_symbol: dict[str, list[dict]],
    altdata_by_symbol: dict[str, dict],
) -> str:
    lines = []
    for m in premarket:
        sym = m["symbol"]
        lines.append(f"\n=== {sym} ===")
        lines.append(
            f"  PRICE: ${m['prev_close']} → ${m['premarket_price']} "
            f"({m['pct_change']:+.2f}%), vol={m['volume']:,}"
        )

        # News
        articles = news_by_symbol.get(sym, [])
        if articles:
            lines.append("  NEWS (overnight):")
            for a in articles:
                lines.append(f"    - [{a['source']}] {a['headline']}")
                if a['summary']:
                    lines.append(f"      → {a['summary'][:180]}")
        else:
            lines.append("  NEWS: (none in last 16h)")

        # Alt-data
        alt = altdata_by_symbol.get(sym, {})
        signal = altdata.signal_strength(alt)
        lines.append(f"  ALT-DATA SIGNAL SCORE: {signal['score']}/10")
        if signal['reasons']:
            for r in signal['reasons']:
                lines.append(f"    · {r}")

        ins = alt.get('insider', {})
        if ins.get('n_buys', 0) > 0 or ins.get('n_sells', 0) > 0:
            lines.append(
                f"  INSIDER (30d): {ins.get('n_buys', 0)} buys ({ins.get('unique_buyers', 0)} insiders), "
                f"{ins.get('n_sells', 0)} sells ({ins.get('unique_sellers', 0)} insiders); "
                f"cluster_buy={ins.get('cluster_buy', False)}"
            )
        cong = alt.get('congress', {})
        if cong.get('n_trades', 0) > 0:
            line = f"  HOUSE CONGRESS (21d): {cong.get('n_buys', 0)} buys, {cong.get('n_sells', 0)} sells"
            if cong.get('buyer_names'):
                line += f"; bought by: {', '.join(cong['buyer_names'])}"
            if cong.get('seller_names'):
                line += f"; sold by: {', '.join(cong['seller_names'])}"
            if cong.get('cluster_buy'):
                line += " [CLUSTER BUY]"
            lines.append(line)
        wsb = alt.get('wsb', {})
        if wsb.get('mentioned'):
            lines.append(
                f"  WSB: rank #{wsb['rank']}, {wsb['mentions']} mentions "
                f"({wsb['growth_ratio']}x vs 24h ago)"
            )
        st = alt.get('stocktwits', {})
        if st.get('n_messages', 0) > 0:
            ratio_str = f"{int(st['ratio']*100)}% bullish" if st.get('ratio') is not None else "—"
            lines.append(f"  STOCKTWITS: {st['n_messages']} msgs, {ratio_str}")
        an = alt.get('analysts')
        if an:
            lines.append(
                f"  ANALYSTS ({an['period']}): {an['strongBuy']}SB / {an['buy']}B / "
                f"{an['hold']}H / {an['sell']}S / {an['strongSell']}SS  "
                f"({an['bullish_pct']}% bullish)"
            )
    return "\n".join(lines)


def pick_stocks(
    premarket: list[dict],
    news_by_symbol: dict[str, list[dict]],
    altdata_by_symbol: dict[str, dict] | None = None,
    mode: str = "loose",
    context_label: str = "Today's premarket movers",
) -> dict:
    """Ask Claude to choose 0-N picks. mode='loose' (default) or 'strict'.

    Loose: EITHER catalyst OR smart-money — picks the best setup available.
    Strict: BOTH required, default answer is no pick (historical default).
    """
    altdata_by_symbol = altdata_by_symbol or {m["symbol"]: {} for m in premarket}
    if mode == "strict":
        system_prompt = SYSTEM
        rule_reminder = "Each pick must have BOTH a real catalyst AND smart-money confirmation. If no candidate qualifies, picks=[]."
    else:
        system_prompt = SYSTEM_LOOSE
        rule_reminder = (
            "Pick the best 1-3 setups available — EITHER a strong catalyst OR strong "
            "smart-money is sufficient. Only return empty if every candidate has a "
            "clear disqualifier."
        )

    user_msg = (
        f"{context_label} with news AND alt-data signals:\n"
        f"{_format_candidates(premarket, news_by_symbol, altdata_by_symbol)}\n\n"
        f"Pick up to {config.MAX_PICKS_PER_DAY} long trades. {rule_reminder}"
    )

    resp = client().messages.create(
        model="claude-opus-4-7",
        max_tokens=2048,
        system=system_prompt.format(max_picks=config.MAX_PICKS_PER_DAY),
        messages=[{"role": "user", "content": user_msg}],
    )

    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    parsed = json.loads(text)
    parsed["picks"] = parsed.get("picks", [])[: config.MAX_PICKS_PER_DAY]
    universe = set(config.TRADABLE_UNIVERSE)
    parsed["picks"] = [p for p in parsed["picks"] if p["symbol"] in universe]
    # In loose mode, EITHER catalyst OR smart-money is enough; strict requires both.
    if mode == "strict":
        parsed["picks"] = [p for p in parsed["picks"] if (p.get("catalyst") or "").strip()]
        parsed["picks"] = [p for p in parsed["picks"] if (p.get("smart_money_signals") or "").strip()]
    else:
        parsed["picks"] = [
            p for p in parsed["picks"]
            if (p.get("catalyst") or "").strip() or (p.get("smart_money_signals") or "").strip()
        ]
    parsed["mode"] = mode
    return parsed
