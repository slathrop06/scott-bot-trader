"""Swing-trading backtest — tests N-day holds on any universe.

Two structural questions at once:
  1. Does the timeframe matter? (1d vs 3d vs 5d vs 10d holds)
  2. Does the universe matter? (mega-cap liquid vs mid-cap less-followed)

For each (universe, hold_period, signal_direction) combo:
  - Each day, pick top 2 gappers (>1.5% absolute)
  - Enter at next day's open
  - Exit at close N trading days later
  - Apply 0.05% round-trip cost (mega-cap) or 0.15% (mid-cap) for spread/slippage

Robustness: split into IS and OOS windows. Only trust variants positive in BOTH.
"""
from __future__ import annotations
import sys
import json
import statistics
from datetime import datetime, timedelta, date
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf
import pandas as pd

from src import config  # noqa: E402


# Mega-cap universe = same as production
MEGA_UNIVERSE = config.TRADABLE_UNIVERSE

# Mid-cap universe — ~$5-50B market cap, decent daily volume, broad sector mix.
# Avoid microcaps (too thin for any meaningful position) and the meme-y ones.
MID_UNIVERSE = [
    # Mid-cap tech / SaaS
    "SNOW", "ZS", "DDOG", "NET", "MDB", "OKTA", "TWLO", "TEAM",
    "ESTC", "DT", "GTLB", "S",
    # Biotech / health
    "BIIB", "ILMN", "EXAS", "NTRA", "ALNY",
    # Consumer
    "ETSY", "RH", "DKS", "ULTA",
    # Industrial / energy
    "BLDR", "PWR", "DVN", "FANG", "MPC", "PSX",
    # Fintech / payments
    "AFRM", "SOFI", "UPST",
    # Diversified
    "ROKU", "PINS", "RBLX",
]


@dataclass
class SwingVariant:
    name: str
    universe_name: str
    universe: list[str]
    hold_days: int
    direction: str          # "long" or "short"
    min_abs_gap: float
    gap_side: str           # "up", "down", "both"
    max_picks: int
    roundtrip_cost_pct: float
    description: str = ""


def fetch_history(symbols: list[str], days_back: int = 220) -> dict[str, pd.DataFrame]:
    end = datetime.now()
    start = end - timedelta(days=int(days_back * 1.7) + 10)
    raw = yf.download(
        tickers=" ".join(symbols),
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        try:
            df = raw[s].dropna()
            if len(df) > 0:
                out[s] = df
        except (KeyError, AttributeError):
            pass
    return out


def trading_days_in_window(history: dict[str, pd.DataFrame], days_ago_start: int, days_ago_end: int) -> tuple[list[date], float]:
    """Return (list of trading days, SPY return % over window)."""
    spy = history.get("SPY")
    if spy is None or spy.empty:
        return [], 0.0
    today_dt = pd.Timestamp(date.today())
    window_start = today_dt - pd.Timedelta(days=days_ago_start)
    window_end = today_dt - pd.Timedelta(days=days_ago_end)
    if window_start > window_end:
        window_start, window_end = window_end, window_start
    days = [d for d in spy.index if window_start <= d <= window_end]
    if len(days) < 2:
        return [], 0.0
    spy_ret = (float(spy.loc[days[-1], "Close"]) / float(spy.loc[days[0], "Close"]) - 1) * 100
    return [d.date() for d in days], spy_ret


def simulate_swing_position(
    df: pd.DataFrame,
    entry_date: date,
    hold_days: int,
    direction: str,
    roundtrip_cost_pct: float,
) -> float | None:
    """Open at entry_date's OPEN, close at the CLOSE hold_days trading days later.
    Returns % P&L (cost already subtracted). None if not enough data."""
    entry_ts = pd.Timestamp(entry_date)
    if entry_ts not in df.index:
        return None
    idx = df.index.get_loc(entry_ts)
    if idx + hold_days >= len(df):
        return None  # not enough future data
    entry_price = float(df.iloc[idx]["Open"])
    exit_price = float(df.iloc[idx + hold_days]["Close"])
    if entry_price <= 0:
        return None
    raw = (exit_price - entry_price) / entry_price * 100
    if direction == "short":
        raw = -raw
    return raw - roundtrip_cost_pct  # subtract costs


def find_gappers(history: dict[str, pd.DataFrame], today: date, prev: date,
                 min_abs_gap: float, gap_side: str, max_picks: int,
                 universe: list[str]) -> list[str]:
    """Identify top gappers on `today` compared to `prev` close. Return symbols."""
    movers = []
    for sym in universe:
        df = history.get(sym)
        if df is None:
            continue
        today_ts = pd.Timestamp(today)
        prev_ts = pd.Timestamp(prev)
        if today_ts not in df.index or prev_ts not in df.index:
            continue
        prev_close = float(df.loc[prev_ts, "Close"])
        today_open = float(df.loc[today_ts, "Open"])
        if prev_close <= 0:
            continue
        gap = (today_open - prev_close) / prev_close * 100
        abs_gap = abs(gap)
        if abs_gap < min_abs_gap:
            continue
        if gap_side == "up" and gap <= 0:
            continue
        if gap_side == "down" and gap >= 0:
            continue
        movers.append((sym, abs_gap))
    movers.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in movers[:max_picks]]


def run_swing_variant(v: SwingVariant, history: dict[str, pd.DataFrame], days: list[date]) -> dict:
    """Walk the days, opening positions, and aggregate."""
    if len(days) < v.hold_days + 2:
        return {"return_pct": 0.0, "n_trades": 0, "note": "window too short for hold period"}

    # For each entry day, identify gappers from previous day's close to that day's open,
    # enter at that day's open, exit at close N trading days later.
    # We allow overlapping positions (entry every day, hold N days = up to N positions at once).
    # Equal-weight cap each entry by 1/N of equity so they fit.
    equity = 1.0
    equity_curve = [equity]
    trade_pnls: list[float] = []
    daily_pnls: list[float] = []

    # Track open positions: list of (entry_day_idx, symbol, weight)
    open_positions: list[tuple[int, str, float]] = []

    # Per-day weight cap so we don't over-allocate when many overlapping.
    # If we enter every day with hold=5d, we have up to 5*max_picks open at once.
    # Cap individual position size at MAX_EQUITY_PER_TRADE_PCT/100.
    max_position_pct = config.MAX_EQUITY_PER_TRADE_PCT / 100

    for i, today in enumerate(days):
        if i == 0:
            equity_curve.append(equity)
            continue
        prev = days[i - 1]

        # Close any positions whose hold has elapsed
        still_open = []
        day_pnl_pct = 0.0
        for entry_idx, sym, weight in open_positions:
            if entry_idx + v.hold_days <= i:
                # This position would have exited today. Realize P&L (close at today's close at the right idx).
                df = history.get(sym)
                if df is None:
                    continue
                # Use the calculated swing P&L from entry_day to entry_day+hold_days
                entry_date = days[entry_idx]
                pnl = simulate_swing_position(df, entry_date, v.hold_days, v.direction, v.roundtrip_cost_pct)
                if pnl is not None:
                    trade_pnls.append(pnl)
                    day_pnl_pct += pnl * weight
            else:
                still_open.append((entry_idx, sym, weight))
        open_positions = still_open

        # Open new positions for today
        gappers = find_gappers(history, today, prev, v.min_abs_gap, v.gap_side, v.max_picks, v.universe)
        if gappers:
            # Weight each new position
            per_position_weight = min(max_position_pct, 1.0 / (len(gappers) * v.hold_days))
            for sym in gappers:
                # Verify entry feasible (open price exists)
                df = history.get(sym)
                if df is None:
                    continue
                today_ts = pd.Timestamp(today)
                if today_ts not in df.index:
                    continue
                open_positions.append((i, sym, per_position_weight))

        equity *= (1 + day_pnl_pct / 100)
        equity_curve.append(equity)
        daily_pnls.append(day_pnl_pct)

    # Force-close any still-open positions at the last available close
    for entry_idx, sym, weight in open_positions:
        df = history.get(sym)
        if df is None:
            continue
        entry_date = days[entry_idx]
        # Try hold_days; if not available, simulate the partial hold to the last day
        last_idx_avail = min(len(days) - 1 - entry_idx, v.hold_days)
        if last_idx_avail <= 0:
            continue
        entry_ts = pd.Timestamp(entry_date)
        if entry_ts not in df.index:
            continue
        i_in_df = df.index.get_loc(entry_ts)
        if i_in_df + last_idx_avail >= len(df):
            continue
        entry_price = float(df.iloc[i_in_df]["Open"])
        exit_price = float(df.iloc[i_in_df + last_idx_avail]["Close"])
        if entry_price <= 0:
            continue
        raw = (exit_price - entry_price) / entry_price * 100
        if v.direction == "short":
            raw = -raw
        pnl = raw - v.roundtrip_cost_pct
        trade_pnls.append(pnl)
        # Apply to final equity
        equity *= (1 + (pnl * weight) / 100)

    total_return = (equity - 1) * 100
    n_trades = len(trade_pnls)
    win_trades = sum(1 for p in trade_pnls if p > 0)
    win_rate = win_trades / max(n_trades, 1) * 100
    avg_win = sum(p for p in trade_pnls if p > 0) / max(win_trades, 1)
    avg_loss = sum(p for p in trade_pnls if p < 0) / max(n_trades - win_trades, 1)
    # Max drawdown from equity curve
    peak = equity_curve[0]
    max_dd = 0.0
    for x in equity_curve:
        peak = max(peak, x)
        max_dd = min(max_dd, (x - peak) / peak * 100)
    sharpe = 0.0
    if len(daily_pnls) >= 2:
        mean = statistics.mean(daily_pnls)
        sd = statistics.stdev(daily_pnls)
        if sd > 0:
            sharpe = (mean / sd) * (252 ** 0.5)

    return {
        "return_pct": round(total_return, 3),
        "n_trades": n_trades,
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "max_dd_pct": round(max_dd, 3),
        "sharpe": round(sharpe, 2),
    }


def variants_for(universe_name: str, universe: list[str], cost_pct: float) -> list[SwingVariant]:
    out = []
    for hold in [1, 3, 5, 10]:
        for direction, side, name in [
            ("long", "up", "longContUp"),
            ("short", "up", "fadeUp"),
            ("long", "down", "longContDown"),
        ]:
            out.append(SwingVariant(
                name=f"{universe_name}_{name}_hold{hold}d",
                universe_name=universe_name,
                universe=universe,
                hold_days=hold,
                direction=direction,
                min_abs_gap=1.5,
                gap_side=side,
                max_picks=2,
                roundtrip_cost_pct=cost_pct,
                description=f"{universe_name} {name} gap-pick, hold {hold} days, cost {cost_pct}%",
            ))
    return out


def main() -> int:
    all_syms = list(set(MEGA_UNIVERSE + MID_UNIVERSE + ["SPY"]))
    print(f"Fetching 220d history for {len(all_syms)} symbols...")
    history = fetch_history(all_syms)
    print(f"Got history for {len(history)} symbols\n")

    is_days, is_spy = trading_days_in_window(history, days_ago_start=60, days_ago_end=0)
    oos_days, oos_spy = trading_days_in_window(history, days_ago_start=180, days_ago_end=60)
    print(f"In-sample:    {len(is_days)} trading days, SPY {is_spy:+.2f}%")
    print(f"Out-of-sample: {len(oos_days)} trading days, SPY {oos_spy:+.2f}%\n")

    variants = (
        variants_for("mega", MEGA_UNIVERSE, 0.05) +
        variants_for("mid", MID_UNIVERSE, 0.15)
    )

    print(f"{'Variant':<40} {'IS Ret':>8} {'OOS Ret':>9} {'IS Alpha':>9} {'OOS Alpha':>10} {'IS WR':>7} {'OOS WR':>7} {'IS N':>6} {'OOS N':>7}")
    print("-" * 115)

    results = []
    for v in variants:
        is_res = run_swing_variant(v, history, is_days)
        oos_res = run_swing_variant(v, history, oos_days)
        is_alpha = is_res["return_pct"] - is_spy
        oos_alpha = oos_res["return_pct"] - oos_spy
        robust = is_alpha > 0 and oos_alpha > 0
        flag = "★" if robust else " "
        print(f"{flag} {v.name:<38} {is_res['return_pct']:>7.2f}% {oos_res['return_pct']:>8.2f}% "
              f"{is_alpha:>+8.2f}% {oos_alpha:>+9.2f}% "
              f"{is_res['win_rate_pct']:>6.1f}% {oos_res['win_rate_pct']:>6.1f}% "
              f"{is_res['n_trades']:>6d} {oos_res['n_trades']:>7d}")
        results.append({
            "name": v.name,
            "universe": v.universe_name,
            "hold_days": v.hold_days,
            "direction": v.direction,
            "gap_side": v.gap_side,
            "cost_pct": v.roundtrip_cost_pct,
            "description": v.description,
            "in_sample": is_res,
            "out_of_sample": oos_res,
            "is_alpha": round(is_alpha, 3),
            "oos_alpha": round(oos_alpha, 3),
            "robust": robust,
        })

    print()
    print("=" * 80)
    robust_winners = [r for r in results if r["robust"]]
    print(f"ROBUST WINNERS (beat SPY on BOTH windows): {len(robust_winners)} of {len(results)}")
    print("=" * 80)
    for w in sorted(robust_winners, key=lambda x: x["is_alpha"] + x["oos_alpha"], reverse=True):
        print(f"\n  {w['name']}: {w['description']}")
        print(f"    IS:   {w['in_sample']['return_pct']:+.2f}% (alpha {w['is_alpha']:+.2f}%)  win {w['in_sample']['win_rate_pct']}%  sharpe {w['in_sample']['sharpe']}  N={w['in_sample']['n_trades']}")
        print(f"    OOS:  {w['out_of_sample']['return_pct']:+.2f}% (alpha {w['oos_alpha']:+.2f}%)  win {w['out_of_sample']['win_rate_pct']}%  sharpe {w['out_of_sample']['sharpe']}  N={w['out_of_sample']['n_trades']}")

    if not robust_winners:
        print("\nNo variant beat SPY on both windows.")
        # Find best by combined alpha anyway
        best = max(results, key=lambda x: x["is_alpha"] + x["oos_alpha"])
        print(f"\nBest by combined alpha (NOT a robust winner — likely overfit):")
        print(f"  {best['name']}: {best['description']}")
        print(f"    IS alpha {best['is_alpha']:+.2f}%, OOS alpha {best['oos_alpha']:+.2f}%")

    out_path = config.DATA_DIR / "swing_backtest.json"
    out_path.write_text(json.dumps({
        "ran_at": datetime.now().isoformat(),
        "is_window_days": len(is_days),
        "oos_window_days": len(oos_days),
        "is_spy_pct": round(is_spy, 3),
        "oos_spy_pct": round(oos_spy, 3),
        "mega_universe_size": len(MEGA_UNIVERSE),
        "mid_universe_size": len(MID_UNIVERSE),
        "results": results,
    }, indent=2))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
