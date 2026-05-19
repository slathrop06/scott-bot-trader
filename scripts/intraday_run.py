"""Intraday picker — runs ~11:00am ET (mid slot) and ~1:30pm ET (late slot).

Same flow as morning_run but:
- Uses LOOSE picker mode (EITHER catalyst OR smart-money, picks at least 1)
- Skips symbols already held (don't double-up)
- Sizes orders from available CASH rather than total equity so stacking with
  the morning slot doesn't over-deploy
- Slot ("mid" / "late") inferred from current ET hour; each slot has its own
  idempotency guard so backup crons no-op cleanly.
"""
from __future__ import annotations
import sys
import json
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import account, altdata, data, earnings, intraday, news, picker, safety, trader, tracker  # noqa: E402


def _slot() -> str:
    now_et = datetime.now(ZoneInfo("America/New_York"))
    return "mid" if now_et.hour < 13 else "late"


def _run(slot: str) -> int:
    kind = f"intraday_{slot}"
    if tracker.already_completed_today(kind):
        print(f"[intraday/{slot}] already completed today — skipping (backup cron no-op)")
        return 0
    tracker.log_run_attempt(kind, "started")

    print(f"[intraday/{slot}] preflight checks...")
    try:
        snapshot = safety.check_can_trade()
    except safety.SafetyError as e:
        print(f"[intraday/{slot}] BLOCKED: {e}")
        tracker.log_run_attempt(kind, "blocked", str(e))
        return 0

    # Available cash drives intraday sizing — protects against over-deployment
    # when morning already opened positions.
    available_cash = snapshot.get("buying_power", snapshot["equity"])
    print(f"[intraday/{slot}] equity=${snapshot['equity']:.2f}  cash=${available_cash:.2f}")
    if available_cash < 500:
        print(f"[intraday/{slot}] not enough cash to size a pick ($500 minimum) — standing down")
        tracker.log_run_attempt(kind, "blocked", f"insufficient cash ${available_cash:.0f}")
        return 0

    print(f"[intraday/{slot}] fetching current movers...")
    movers = data.top_movers(n=15)
    if not movers:
        print(f"[intraday/{slot}] no market data — bailing")
        tracker.log_run_attempt(kind, "blocked", "no market data")
        return 0

    held = intraday.open_symbols()
    print(f"[intraday/{slot}] holding: {sorted(held) or '(none)'}")
    movers = intraday.filter_out_held(movers, held)
    if not movers:
        print(f"[intraday/{slot}] all movers already held — standing down")
        tracker.log_run_attempt(kind, "ok", "all candidates already held")
        return 0

    print(f"[intraday/{slot}] {len(movers)} candidates after held-filter")

    print(f"[intraday/{slot}] earnings filter (1d buffer)...")
    safe_syms, blocked = earnings.filter_out_earnings_risk(
        [m["symbol"] for m in movers], min_days_buffer=1
    )
    movers = [m for m in movers if m["symbol"] in safe_syms]
    if not movers:
        print(f"[intraday/{slot}] all candidates blocked by earnings filter")
        tracker.log_run_attempt(kind, "ok", f"all blocked by earnings: {blocked}")
        return 0

    symbols = [m["symbol"] for m in movers]
    print(f"[intraday/{slot}] fetching news...")
    news_by_symbol = news.overnight_news(symbols, lookback_hours=6)  # intraday: 6h lookback
    print(f"[intraday/{slot}] fetching alt-data...")
    altdata_by_symbol = altdata.smart_money_snapshot(symbols)

    print(f"[intraday/{slot}] asking Claude (loose mode)...")
    label = f"Current intraday movers as of {datetime.now(ZoneInfo('America/New_York')).strftime('%I:%M %p ET')} ({slot} slot)"
    picks_response = picker.pick_stocks(
        movers, news_by_symbol, altdata_by_symbol,
        mode="loose",
        context_label=label,
    )
    print(f"[intraday/{slot}] picks: {json.dumps(picks_response, indent=2)}")

    picks = picks_response.get("picks", [])
    if not picks:
        print(f"[intraday/{slot}] Claude returned no picks even in loose mode — standing down")
        tracker.log_morning_run(picks_response, [], snapshot)  # reuse — same shape
        tracker.log_run_attempt(kind, "ok", "loose mode returned no picks")
        return 0

    print(f"[intraday/{slot}] submitting {len(picks)} bracket orders (sized from cash)...")
    # Pass cash, not equity, so trade_budget caps off available cash
    orders = trader.execute_picks(picks, equity=available_cash)
    for o in orders:
        print(f"[intraday/{slot}]   {o}")

    tracker.log_morning_run(picks_response, orders, snapshot)
    tracker.save_account_snapshot(account.current_snapshot())
    tracker.log_run_attempt(kind, "ok", f"{len(orders)} orders submitted")
    print(f"[intraday/{slot}] done")
    return 0


def main() -> int:
    slot = _slot()
    try:
        return _run(slot)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[intraday/{slot}] UNHANDLED EXCEPTION: {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        note = f"{type(e).__name__}: {e}"[:240]
        try:
            tracker.log_run_attempt(f"intraday_{slot}", "error", note)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
