"""Entry point — runs ~9:35am ET. Picks stocks and submits bracket orders."""
from __future__ import annotations
import sys
import json
from pathlib import Path

# Allow running as a plain script (`python scripts/morning_run.py`)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import account, altdata, data, earnings, news, picker, safety, trader, tracker  # noqa: E402


def main() -> int:
    if tracker.already_completed_today("morning"):
        print("[morning] already completed today — skipping (backup cron no-op)")
        return 0
    tracker.log_run_attempt("morning", "started")
    print("[morning] preflight checks...")
    try:
        snapshot = safety.check_can_trade()
    except safety.SafetyError as e:
        print(f"[morning] BLOCKED: {e}")
        tracker.log_run_attempt("morning", "blocked", str(e))
        return 0  # not an error — bot just refused to trade

    print(f"[morning] equity=${snapshot['equity']:.2f}  daily={snapshot['daily_pct']:+.2f}%")

    print("[morning] fetching premarket movers...")
    movers = data.top_movers(n=15)
    if not movers:
        print("[morning] no premarket data — bailing")
        tracker.log_run_attempt("morning", "blocked", "no premarket data")
        return 0
    print(f"[morning] {len(movers)} movers fetched")

    print("[morning] checking for upcoming earnings (filter out within 1 day)...")
    safe_syms, blocked = earnings.filter_out_earnings_risk(
        [m["symbol"] for m in movers], min_days_buffer=1
    )
    if blocked:
        print(f"[morning] blocked due to earnings: {blocked}")
    movers = [m for m in movers if m["symbol"] in safe_syms]
    if not movers:
        print("[morning] all movers blocked by earnings filter — standing down")
        tracker.log_run_attempt("morning", "ok", f"all movers blocked by earnings: {blocked}")
        return 0
    print(f"[morning] {len(movers)} movers remain after earnings filter")

    print("[morning] fetching overnight news for movers...")
    symbols = [m["symbol"] for m in movers]
    news_by_symbol = news.overnight_news(symbols, lookback_hours=16)
    news_counts = {s: len(news_by_symbol.get(s, [])) for s in symbols}
    print(f"[morning] news article counts: {news_counts}")

    print("[morning] fetching alt-data signals (insider/WSB/stocktwits/analysts)...")
    altdata_by_symbol = altdata.smart_money_snapshot(symbols)
    # Quick summary for the log
    for s in symbols:
        sig = altdata.signal_strength(altdata_by_symbol[s])
        if sig['score'] != 0:
            print(f"[morning]   {s}: score={sig['score']:+d}  {'; '.join(sig['reasons'])}")

    print("[morning] asking Claude for catalyst + smart-money picks...")
    picks_response = picker.pick_stocks(movers, news_by_symbol, altdata_by_symbol)
    print(f"[morning] picks: {json.dumps(picks_response, indent=2)}")

    picks = picks_response.get("picks", [])
    if not picks:
        print("[morning] Claude returned no picks — standing down")
        tracker.log_morning_run(picks_response, [], snapshot)
        tracker.log_run_attempt("morning", "ok", "Claude returned no picks")
        return 0

    print(f"[morning] submitting {len(picks)} bracket orders...")
    orders = trader.execute_picks(picks, equity=snapshot["equity"])
    for o in orders:
        print(f"[morning]   {o}")

    tracker.log_morning_run(picks_response, orders, snapshot)
    tracker.save_account_snapshot(account.current_snapshot())
    tracker.log_run_attempt("morning", "ok", f"{len(orders)} orders submitted")
    print("[morning] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
