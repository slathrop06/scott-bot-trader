"""Entry point — runs ~3:55pm ET. Closes everything, logs P&L."""
from __future__ import annotations
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import alpaca_client, flatten, tracker  # noqa: E402


def main() -> int:
    tracker.log_run_attempt("eod", "started")

    if not alpaca_client.market_is_open():
        print("[eod] market closed — nothing to flatten")
        tracker.log_run_attempt("eod", "blocked", "market closed")
        return 0

    print("[eod] flattening positions...")
    result = flatten.flatten_all()
    print(f"[eod] {result}")

    print("[eod] logging P&L...")
    summary = tracker.log_eod(result)
    print(f"[eod] equity=${summary['equity']:.2f}  daily_pnl=${summary['daily_pnl_usd']:+.2f}  ({summary['daily_pct']:+.2f}%)")

    weekly = tracker.weekly_progress()
    print(f"[eod] WTD: {json.dumps(weekly, indent=2)}")
    tracker.log_run_attempt("eod", "ok", f"closed {len(result.get('closed', []))} positions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
