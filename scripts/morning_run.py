"""Entry point — runs ~9:35am ET. Picks stocks and submits bracket orders."""
from __future__ import annotations
import sys
import json
from pathlib import Path

# Allow running as a plain script (`python scripts/morning_run.py`)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import data, picker, safety, trader, tracker  # noqa: E402


def main() -> int:
    print("[morning] preflight checks...")
    try:
        snapshot = safety.check_can_trade()
    except safety.SafetyError as e:
        print(f"[morning] BLOCKED: {e}")
        return 0  # not an error — bot just refused to trade

    print(f"[morning] equity=${snapshot['equity']:.2f}  daily={snapshot['daily_pct']:+.2f}%")

    print("[morning] fetching premarket movers...")
    movers = data.top_movers(n=15)
    if not movers:
        print("[morning] no premarket data — bailing")
        return 0
    print(f"[morning] {len(movers)} movers fetched")

    print("[morning] asking Claude for picks...")
    picks_response = picker.pick_stocks(movers)
    print(f"[morning] picks: {json.dumps(picks_response, indent=2)}")

    picks = picks_response.get("picks", [])
    if not picks:
        print("[morning] Claude returned no picks — standing down")
        tracker.log_morning_run(picks_response, [], snapshot)
        return 0

    print(f"[morning] submitting {len(picks)} bracket orders...")
    orders = trader.execute_picks(picks, equity=snapshot["equity"])
    for o in orders:
        print(f"[morning]   {o}")

    tracker.log_morning_run(picks_response, orders, snapshot)
    print("[morning] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
