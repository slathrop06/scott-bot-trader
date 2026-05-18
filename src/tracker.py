"""P&L logging + weekly goal tracking."""
from __future__ import annotations
import json
from datetime import datetime, date, timedelta
from . import alpaca_client, config


def _load_log() -> list[dict]:
    if not config.TRADES_LOG.exists():
        return []
    try:
        return json.loads(config.TRADES_LOG.read_text())
    except json.JSONDecodeError:
        return []


def _save_log(entries: list[dict]) -> None:
    config.DATA_DIR.mkdir(exist_ok=True)
    config.TRADES_LOG.write_text(json.dumps(entries, indent=2, default=str))


def log_run_attempt(kind: str, status: str, note: str = "") -> None:
    """Always called at the start/end of every cron run, even on bail/error.
    Lets the dashboard answer 'did the bot actually run today?'"""
    entries = _load_log()
    entries.append({
        "type": f"{kind}_attempt",
        "date": date.today().isoformat(),
        "ts": datetime.utcnow().isoformat() + "Z",
        "status": status,  # "started", "ok", "blocked", "error"
        "note": note,
    })
    _save_log(entries)


def already_completed_today(kind: str) -> bool:
    # Did a `kind` run finish today? Used by the backup cron to no-op when
    # the primary already ran (success or graceful bail). A crashed "started"
    # entry doesn't count — the retry should still fire.
    today = date.today().isoformat()
    for e in _load_log():
        if (
            e.get("type") == f"{kind}_attempt"
            and e.get("date") == today
            and e.get("status") in ("ok", "blocked")
        ):
            return True
    return False


def log_morning_run(picks_result: dict, orders: list[dict], account_snapshot: dict) -> None:
    entries = _load_log()
    entries.append({
        "type": "morning",
        "date": date.today().isoformat(),
        "ts": datetime.utcnow().isoformat() + "Z",
        "equity_start": account_snapshot["equity"],
        "picks_response": picks_result,
        "orders": orders,
    })
    _save_log(entries)


def log_eod(flatten_result: dict) -> dict:
    acct = alpaca_client.account()
    equity = float(acct.equity)
    last_equity = float(acct.last_equity)
    daily_pnl = equity - last_equity
    daily_pct = (daily_pnl / last_equity * 100) if last_equity else 0

    entries = _load_log()
    entries.append({
        "type": "eod",
        "date": date.today().isoformat(),
        "ts": datetime.utcnow().isoformat() + "Z",
        "equity_end": equity,
        "daily_pnl_usd": round(daily_pnl, 2),
        "daily_pct": round(daily_pct, 4),
        "flatten": flatten_result,
    })
    _save_log(entries)

    return {"equity": equity, "daily_pnl_usd": daily_pnl, "daily_pct": daily_pct}


def weekly_progress() -> dict:
    """Compute week-to-date P&L vs. the 1.5% target. Week = Mon-Sun."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    entries = _load_log()
    eod_this_week = [
        e for e in entries
        if e.get("type") == "eod" and e["date"] >= monday.isoformat()
    ]

    if not eod_this_week:
        return {
            "week_start": monday.isoformat(),
            "trading_days_logged": 0,
            "wtd_pct": 0.0,
            "target_pct": config.WEEKLY_TARGET_PCT,
            "on_pace": None,
        }

    # Compound the daily % returns.
    wtd_mult = 1.0
    for e in eod_this_week:
        wtd_mult *= 1 + (e["daily_pct"] / 100)
    wtd_pct = (wtd_mult - 1) * 100

    return {
        "week_start": monday.isoformat(),
        "trading_days_logged": len(eod_this_week),
        "wtd_pct": round(wtd_pct, 4),
        "target_pct": config.WEEKLY_TARGET_PCT,
        "on_pace": wtd_pct >= (config.WEEKLY_TARGET_PCT * len(eod_this_week) / 5),
    }
