"""Claude-powered stock picker.

Takes premarket data + overnight news for each candidate. Claude must identify
a specific catalyst behind each pick — no catalyst, no pick.
"""
from __future__ import annotations
import json
from anthropic import Anthropic

from . import config


_client: Anthropic | None = None


def client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


SYSTEM = """You are a day-trading analyst for a small paper-trading bot.

Each morning you receive premarket data PLUS overnight news headlines for a fixed
universe of liquid US stocks. Your job: pick 0 to {max_picks} long-only trades to
enter at market open, hold intraday, and exit by 3:55pm ET.

Strategy guardrails:
- LONG ONLY. No shorts, no options, no leverage.
- Each pick gets a bracket order: take-profit +2%, stop-loss -1% (2:1 reward:risk).
- Weekly target is +1.5% on total equity. Conservative beats reckless.
- The "edge" is your ability to READ THE NEWS. Reject picks where you cannot
  point to a specific, real catalyst in the provided headlines.
- A move without a catalyst is noise — likely to mean-revert. Skip it.
- A catalyst-driven move (earnings beat, FDA approval, M&A, analyst upgrade,
  guidance raise, major contract) is what you want.
- If no candidates have real catalysts, return EMPTY picks. Don't force trades.
- Be wary of stocks with negative catalysts (downgrades, lawsuits, missed earnings) —
  these often gap down further intraday. Don't buy the dip on bad news.
- If the news for a candidate contradicts the price move (e.g. bad news but stock up),
  treat with extra skepticism — could be a short squeeze that reverses fast.

Output STRICT JSON only (no prose, no markdown fences):
{{
  "picks": [
    {{
      "symbol": "AAPL",
      "confidence": "high|medium|low",
      "catalyst": "Specific headline or event driving the move — quote or paraphrase the news",
      "rationale": "Why this catalyst supports a long intraday — one short sentence"
    }}
  ],
  "market_take": "One sentence on overall tone today",
  "skipped_movers": ["TSLA: -5% with no clear news, likely noise", ...]
}}
"""


def _format_candidates(premarket: list[dict], news_by_symbol: dict[str, list[dict]]) -> str:
    lines = []
    for m in premarket:
        sym = m["symbol"]
        lines.append(f"\n=== {sym} ===")
        lines.append(
            f"  Premarket: ${m['prev_close']} → ${m['premarket_price']} "
            f"({m['pct_change']:+.2f}%), vol={m['volume']:,}"
        )
        articles = news_by_symbol.get(sym, [])
        if not articles:
            lines.append("  News (last 16h): (none)")
        else:
            lines.append(f"  News (last 16h):")
            for a in articles:
                lines.append(f"    - [{a['source']}] {a['headline']}")
                if a['summary']:
                    lines.append(f"      → {a['summary']}")
    return "\n".join(lines)


def pick_stocks(premarket: list[dict], news_by_symbol: dict[str, list[dict]]) -> dict:
    """Ask Claude to choose 0-N picks given premarket data + news."""
    user_msg = (
        f"Today's premarket movers with overnight news:\n"
        f"{_format_candidates(premarket, news_by_symbol)}\n\n"
        f"Pick up to {config.MAX_PICKS_PER_DAY} long trades for today. "
        f"Each pick MUST have a real catalyst from the news above. "
        f"If no candidate has a clear catalyst, return picks=[]."
    )

    resp = client().messages.create(
        model="claude-opus-4-7",
        max_tokens=2048,
        system=SYSTEM.format(max_picks=config.MAX_PICKS_PER_DAY),
        messages=[{"role": "user", "content": user_msg}],
    )

    text = resp.content[0].text.strip()
    # Strip code fences if model adds them despite instructions.
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    parsed = json.loads(text)
    parsed["picks"] = parsed.get("picks", [])[: config.MAX_PICKS_PER_DAY]
    universe = set(config.TRADABLE_UNIVERSE)
    parsed["picks"] = [p for p in parsed["picks"] if p["symbol"] in universe]
    # Defensive: ensure catalyst field present, drop picks without one.
    parsed["picks"] = [p for p in parsed["picks"] if (p.get("catalyst") or "").strip()]
    return parsed
