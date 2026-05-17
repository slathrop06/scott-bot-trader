"""Claude-powered stock picker. Reads premarket data, returns picks as JSON."""
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

Each morning you receive premarket data for a fixed universe of liquid US stocks.
Your job: pick 1 to {max_picks} long-only trades to enter at market open, hold intraday,
and exit by 3:55pm ET (same-day exit is enforced by the bot).

Strategy guardrails:
- LONG ONLY. No shorts, no options, no leverage.
- Each pick gets a bracket order: take-profit at +2%, stop-loss at -1%.
- The weekly target is +1.5% on total equity. Pick conservatively — losing days are fine.
- If nothing looks compelling, return an empty list. Don't force trades.
- Prefer stocks showing premarket momentum (>1% move on real volume) over flat names.
- Avoid picks where the catalyst is unclear or driven by a single ambiguous headline.

Output STRICT JSON only (no prose, no markdown fences):
{{
  "picks": [
    {{"symbol": "AAPL", "confidence": "high|medium|low", "rationale": "one short sentence"}}
  ],
  "market_take": "one sentence on overall tone today"
}}
"""


def pick_stocks(premarket: list[dict]) -> dict:
    """Ask Claude to choose 0-N picks from premarket data."""
    user_msg = (
        f"Premarket snapshot (sorted by |% change|):\n"
        f"{json.dumps(premarket, indent=2)}\n\n"
        f"Pick up to {config.MAX_PICKS_PER_DAY} long trades for today, or none."
    )

    resp = client().messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        system=SYSTEM.format(max_picks=config.MAX_PICKS_PER_DAY),
        messages=[{"role": "user", "content": user_msg}],
    )

    text = resp.content[0].text.strip()
    # Defensive: strip code fences if model adds them despite instructions.
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    parsed = json.loads(text)
    # Enforce max picks cap (defensive: model might exceed it).
    parsed["picks"] = parsed.get("picks", [])[: config.MAX_PICKS_PER_DAY]
    # Enforce universe membership.
    universe = set(config.TRADABLE_UNIVERSE)
    parsed["picks"] = [p for p in parsed["picks"] if p["symbol"] in universe]
    return parsed
