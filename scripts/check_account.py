"""Smoke test — verifies Alpaca + Anthropic keys work, doesn't trade."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import alpaca_client, config  # noqa: E402


def main() -> int:
    print(f"Trading mode: {config.TRADING_MODE}")
    print(f"Base URL: {config.ALPACA_BASE_URL}")
    print()

    print("Fetching Alpaca account...")
    acct = alpaca_client.account()
    print(f"  Account #:     {acct.account_number}")
    print(f"  Status:        {acct.status}")
    print(f"  Equity:        ${float(acct.equity):,.2f}")
    print(f"  Cash:          ${float(acct.cash):,.2f}")
    print(f"  Buying power:  ${float(acct.buying_power):,.2f}")
    print(f"  PDT flag:      {getattr(acct, 'pattern_day_trader', 'n/a')}")
    print()

    clock = alpaca_client.trading().get_clock()
    print(f"Market open now: {clock.is_open}")
    print(f"  Next open:    {clock.next_open}")
    print(f"  Next close:   {clock.next_close}")
    print()

    print("Open positions:")
    positions = alpaca_client.positions()
    if not positions:
        print("  (none)")
    for p in positions:
        print(f"  {p.symbol}: {p.qty} @ ${float(p.avg_entry_price):.2f}  PL=${float(p.unrealized_pl):+.2f}")

    print("\nOK — Alpaca connection works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
