"""Pre-trade safety checks. Refuse to trade if any of these trip."""
from __future__ import annotations
from . import alpaca_client, config


class SafetyError(Exception):
    pass


def check_can_trade() -> dict:
    """Run all preflight checks. Returns account snapshot. Raises if blocked."""
    if config.TRADING_MODE != "paper":
        raise SafetyError(
            f"TRADING_MODE={config.TRADING_MODE} — only 'paper' is allowed right now."
        )

    if not alpaca_client.market_is_open():
        raise SafetyError("Market is closed.")

    acct = alpaca_client.account()
    equity = float(acct.equity)
    last_equity = float(acct.last_equity)
    daily_pct = (equity - last_equity) / last_equity * 100 if last_equity else 0

    if daily_pct <= -config.DAILY_LOSS_KILL_SWITCH_PCT:
        raise SafetyError(
            f"Daily loss kill-switch: down {daily_pct:.2f}% today "
            f"(limit -{config.DAILY_LOSS_KILL_SWITCH_PCT}%)."
        )

    # PDT: under $25k, max 3 day trades / 5 business days. Refuse if flagged.
    if getattr(acct, "pattern_day_trader", False):
        raise SafetyError("Account is flagged as PDT and equity < $25k. Refusing to trade.")

    if float(acct.buying_power) < config.MIN_BUYING_POWER_USD:
        raise SafetyError(
            f"Buying power ${float(acct.buying_power):.2f} below ${config.MIN_BUYING_POWER_USD} "
            f"minimum — account is too deployed to take new trades."
        )

    return {
        "equity": equity,
        "last_equity": last_equity,
        "buying_power": float(acct.buying_power),
        "daily_pct": daily_pct,
    }


def trade_budget(equity: float, num_picks: int) -> float:
    """How much $ per pick. Capped by MAX_EQUITY_PER_TRADE_PCT."""
    if num_picks <= 0:
        return 0
    per_pick_cap = equity * config.MAX_EQUITY_PER_TRADE_PCT / 100
    even_split = equity / num_picks
    return min(per_pick_cap, even_split)
