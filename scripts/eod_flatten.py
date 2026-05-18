"""Entry point — runs ~3:55pm ET. Closes everything, logs P&L."""
from __future__ import annotations
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import account, alpaca_client, flatten, tracker  # noqa: E402


def main() -> int:
    if tracker.already_completed_today("eod"):
        print("[eod] already completed today — skipping (backup cron no-op)")
        return 0
    tracker.log_run_attempt("eod", "started")

    # Don't bail off-hours: close_all_positions queues market sells for the
    # next open, which is exactly what we want when the primary cron skipped
    # the 3:55 ET window and we're flattening after the bell.
    if not alpaca_client.market_is_open():
        print("[eod] market closed — flatten will queue orders for next open")

    print("[eod] flattening positions...")
    result = flatten.flatten_all()
    print(f"[eod] {result}")

    print("[eod] logging P&L...")
    summary = tracker.log_eod(result)
    print(f"[eod] equity=${summary['equity']:.2f}  daily_pnl=${summary['daily_pnl_usd']:+.2f}  ({summary['daily_pct']:+.2f}%)")

    print("[eod] fetching today's filled orders for trade history...")
    rt = account.todays_round_trips()
    trades = rt["trades"]
    tracker.log_trades(trades)
    if trades:
        for t in trades:
            print(
                f"[eod]   {t['symbol']} {t['qty']}sh  "
                f"entry ${t['entry_price']} → exit ${t['exit_price']}  "
                f"pnl ${t['pnl_usd']:+.2f} ({t['pnl_pct']:+.2f}%)  [{t['exit_reason']}]"
            )
    if rt["open_positions"]:
        print(f"[eod] {len(rt['open_positions'])} positions still open (exits pending fill)")

    print("[eod] saving account snapshot...")
    tracker.save_account_snapshot(account.current_snapshot())

    weekly = tracker.weekly_progress()
    print(f"[eod] WTD: {json.dumps(weekly, indent=2)}")
    tracker.log_run_attempt("eod", "ok", f"closed {len(result.get('closed', []))} positions, {len(trades)} round-trips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
