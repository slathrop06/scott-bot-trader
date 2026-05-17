"""Translate Claude's picks into bracket orders."""
from __future__ import annotations
from . import alpaca_client, config, safety


def execute_picks(picks: list[dict], equity: float) -> list[dict]:
    """For each pick, size and submit a bracket order. Returns list of results."""
    if not picks:
        return []

    budget = safety.trade_budget(equity, len(picks))
    results: list[dict] = []

    for p in picks:
        sym = p["symbol"]
        try:
            order = alpaca_client.submit_bracket(
                symbol=sym,
                notional_usd=budget,
                stop_loss_pct=config.STOP_LOSS_PCT,
                take_profit_pct=config.TAKE_PROFIT_PCT,
            )
            results.append({
                "status": "submitted",
                "confidence": p.get("confidence"),
                "rationale": p.get("rationale"),
                **order,
            })
        except Exception as e:
            results.append({
                "status": "failed",
                "symbol": sym,
                "error": str(e),
            })

    return results
