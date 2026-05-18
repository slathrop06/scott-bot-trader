"""One-off: backfill today's filled orders into trades.json as trade entries.

Used when the dashboard rebuild lands AFTER trades have already happened in a
day. Pulls the same data eod_flatten would have logged, but doesn't move any
positions. Safe to re-run — appends trade rows; the dashboard tolerates dupes
by entry_order_id if you ever wire up deduping.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import account, tracker  # noqa: E402


def main() -> int:
    rt = account.todays_round_trips()
    trades = rt["trades"]
    print(f"[backfill] found {len(trades)} round-trips, {len(rt['open_positions'])} still open")
    for t in trades:
        print(
            f"[backfill]   {t['symbol']} {t['qty']}sh  "
            f"entry ${t['entry_price']} → exit ${t['exit_price']}  "
            f"pnl ${t['pnl_usd']:+.2f} ({t['pnl_pct']:+.2f}%)  [{t['exit_reason']}]"
        )
    tracker.log_trades(trades)
    tracker.save_account_snapshot(account.current_snapshot())
    print("[backfill] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
