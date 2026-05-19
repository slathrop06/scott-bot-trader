"""Intraday-specific helpers: don't double up on positions we already hold,
and size new entries from available cash rather than total equity so we don't
over-deploy when stacking with morning picks."""
from __future__ import annotations

from . import alpaca_client


def open_symbols() -> set[str]:
    return {p.symbol for p in alpaca_client.positions()}


def filter_out_held(movers: list[dict], held: set[str]) -> list[dict]:
    return [m for m in movers if m["symbol"] not in held]
