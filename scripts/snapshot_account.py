"""Snapshot current account balance + positions to data/account.json.

Called hourly during market hours by snapshot.yml. Lets the dashboard show
a current portfolio without anyone hitting the Alpaca console.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import account, tracker  # noqa: E402


def main() -> int:
    snap = account.current_snapshot()
    tracker.save_account_snapshot(snap)
    print(
        f"[snapshot] equity=${snap['equity']:.2f}  "
        f"cash=${snap['cash']:.2f}  "
        f"all_time={snap['all_time_return_pct']:+.2f}%  "
        f"positions={len(snap['positions'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
