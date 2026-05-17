"""End-of-day liquidation. Runs at 3:55pm ET."""
from __future__ import annotations
from . import alpaca_client


def flatten_all() -> dict:
    """Close every open position. Same-day exit guarantee."""
    positions = alpaca_client.positions()
    if not positions:
        return {"closed": [], "note": "No open positions."}

    open_syms = [p.symbol for p in positions]
    results = alpaca_client.close_all_positions()
    return {
        "closed": open_syms,
        "raw_response_count": len(results) if results else 0,
    }
