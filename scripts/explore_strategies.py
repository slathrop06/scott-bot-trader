"""Strategy exploration — test many mechanical variants on TWO separate
time windows to guard against overfitting.

The rule: only variants that win on BOTH the in-sample and out-of-sample
windows are candidates for real signal. Single-window winners are noise.

No Claude calls. This tests structural bets only:
  - direction (long vs fade)
  - gap-size filter
  - stop/target ratio
  - sector subset
"""
from __future__ import annotations
import sys
import json
from datetime import datetime, timedelta, date
from pathlib import Path
from dataclasses import dataclass, asdict
import statistics

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf
import pandas as pd

from src import config  # noqa: E402


@dataclass
class Variant:
    name: str
    direction: str       # "long" or "short"
    min_abs_gap: float   # ignore gaps smaller than this
    max_abs_gap: float   # ignore gaps larger than this (avoid crazy moves)
    gap_side: str        # "up" (only gap-ups), "down" (only gap-downs), "both"
    take_profit_pct: float
    stop_loss_pct: float
    max_picks: int
    universe_subset: list[str] | None = None  # None = full universe
    description: str = ""


def fetch_history(symbols: list[str], days_back: int) -> dict[str, pd.DataFrame]:
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
        except KeyError:
            pass
    return out


def simulate_one_trade(
    open_p: float, high: float, low: float, close: float,
    direction: str, take_profit_pct: float, stop_loss_pct: float,
) -> float:
    """Return % P&L for one trade. Conservative: when both stop & target
    are touched intraday, assume stop fills first."""
    if direction == "long":
        stop = open_p * (1 - stop_loss_pct / 100)
        target = open_p * (1 + take_profit_pct / 100)
        hit_stop = low <= stop
        hit_target = high >= target
        if hit_stop and hit_target:
            return -stop_loss_pct
        if hit_target:
            return take_profit_pct
        if hit_stop:
            return -stop_loss_pct
        return (close - open_p) / open_p * 100
    else:  # short
        stop = open_p * (1 + stop_loss_pct / 100)   # stop ABOVE entry
        target = open_p * (1 - take_profit_pct / 100)  # target BELOW entry
        hit_stop = high >= stop
        hit_target = low <= target
        if hit_stop and hit_target:
            return -stop_loss_pct
        if hit_target:
            return take_profit_pct
        if hit_stop:
            return -stop_loss_pct
        return (open_p - close) / open_p * 100  # short profits when price falls


def run_variant(
    variant: Variant,
    history: dict[str, pd.DataFrame],
    trading_pairs: list[tuple[date, date]],
) -> dict:
    """Simulate a variant over the given trading days. Returns aggregate stats."""
    universe = variant.universe_subset or list(history.keys())
    equity = 1.0
    equity_curve = [equity]
    daily_pnls: list[float] = []
    win_days = lose_days = 0
    n_picks_total = 0
    n_days_active = 0

    for today, prev in trading_pairs:
        movers: list[dict] = []
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
            if abs_gap < variant.min_abs_gap or abs_gap > variant.max_abs_gap:
                continue
            if variant.gap_side == "up" and gap <= 0:
                continue
            if variant.gap_side == "down" and gap >= 0:
                continue
            movers.append({
                "symbol": sym,
                "gap_pct": gap,
                "today_ts": today_ts,
                "df": df,
            })

        # Rank by absolute gap; take top N
        movers.sort(key=lambda m: abs(m["gap_pct"]), reverse=True)
        picks = movers[: variant.max_picks]
        if not picks:
            equity_curve.append(equity)
            continue

        budget_pct = min(config.MAX_EQUITY_PER_TRADE_PCT / 100, 1.0 / len(picks))
        day_pnl = 0.0
        for p in picks:
            row = p["df"].loc[p["today_ts"]]
            pct = simulate_one_trade(
                open_p=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                direction=variant.direction,
                take_profit_pct=variant.take_profit_pct,
                stop_loss_pct=variant.stop_loss_pct,
            )
            day_pnl += pct * budget_pct

        equity *= (1 + day_pnl / 100)
        equity_curve.append(equity)
        daily_pnls.append(day_pnl)
        n_picks_total += len(picks)
        n_days_active += 1
        if day_pnl > 0:
            win_days += 1
        elif day_pnl < 0:
            lose_days += 1

    total_return = (equity - 1) * 100
    win_rate = win_days / max(n_days_active, 1) * 100
    # Max drawdown
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)
    # Sharpe
    sharpe = 0.0
    if len(daily_pnls) >= 2:
        mean = statistics.mean(daily_pnls)
        sd = statistics.stdev(daily_pnls)
        if sd > 0:
            sharpe = (mean / sd) * (252 ** 0.5)
    avg_win = (sum(p for p in daily_pnls if p > 0) / max(win_days, 1))
    avg_loss = (sum(p for p in daily_pnls if p < 0) / max(lose_days, 1))

    return {
        "return_pct": round(total_return, 3),
        "win_rate_pct": round(win_rate, 1),
        "n_active_days": n_days_active,
        "n_picks_total": n_picks_total,
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "max_dd_pct": round(max_dd, 3),
        "sharpe": round(sharpe, 2),
    }


def build_window(history: dict[str, pd.DataFrame], days_ago_start: int, days_ago_end: int) -> tuple[list[tuple[date, date]], float]:
    """Return (trading_pairs, spy_return_pct) for the given window."""
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
    pairs = [(days[i].date(), days[i-1].date()) for i in range(1, len(days))]
    spy_ret = (float(spy.loc[days[-1], "Close"]) / float(spy.loc[days[0], "Close"]) - 1) * 100
    return pairs, spy_ret


# Variant definitions — each tests one hypothesis.
ETF_ONLY = ["SPY", "QQQ", "IWM", "DIA"]
MEGA_TECH = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "TSLA"]

VARIANTS = [
    Variant("0_baseline_long", "long", 1.0, 100, "up", 2.0, 1.0, 2,
            description="Original strategy: long gap-ups, +2/-1"),
    Variant("1_fade_gap_ups", "short", 1.0, 100, "up", 2.0, 1.0, 2,
            description="Short gap-ups (mean reversion)"),
    Variant("2_fade_gap_downs", "long", 1.0, 100, "down", 2.0, 1.0, 2,
            description="Long gap-downs (mean reversion)"),
    Variant("3_fade_big_only", "short", 3.0, 100, "up", 2.0, 1.0, 2,
            description="Short only BIG gap-ups (>3%)"),
    Variant("4_tight_target", "long", 1.0, 100, "up", 0.5, 1.0, 2,
            description="Long gap-ups with TIGHT target +0.5 / stop -1"),
    Variant("5_fade_tight_target", "short", 1.0, 100, "up", 0.5, 1.0, 2,
            description="Short gap-ups with tight target +0.5 / stop -1"),
    Variant("6_wide_stop_tight_target", "short", 1.0, 100, "up", 0.5, 2.0, 2,
            description="Short gap-ups +0.5 / -2 (let it breathe, take quick profits)"),
    Variant("7_etf_long_continuation", "long", 0.5, 5, "up", 1.0, 0.5, 2, universe_subset=ETF_ONLY,
            description="ETFs only, long continuation, modest gap"),
    Variant("8_etf_fade", "short", 0.5, 5, "up", 0.5, 1.0, 2, universe_subset=ETF_ONLY,
            description="ETFs only, fade gap-ups"),
    Variant("9_megatech_fade", "short", 1.5, 100, "up", 1.0, 1.0, 2, universe_subset=MEGA_TECH,
            description="Mega-cap tech only, fade gap-ups, symmetric 1:1"),
    Variant("10_long_small_gaps", "long", 0.3, 1.5, "up", 1.5, 0.5, 2,
            description="Long SMALL gap-ups (continuation territory)"),
    Variant("11_fade_both_sides", "short", 1.0, 100, "both", 1.0, 1.0, 2,
            description="Fade both gap-ups (short) — gap-downs excluded for now"),
]


def main() -> int:
    print("Fetching 200 days of history...")
    history = fetch_history(config.TRADABLE_UNIVERSE, 200)
    print(f"Got history for {len(history)} symbols")

    # Two non-overlapping windows
    is_pairs, is_spy = build_window(history, days_ago_start=60, days_ago_end=0)
    oos_pairs, oos_spy = build_window(history, days_ago_start=180, days_ago_end=60)

    print(f"\nIn-sample window:    {len(is_pairs)} days, SPY {is_spy:+.2f}%")
    print(f"Out-of-sample window: {len(oos_pairs)} days, SPY {oos_spy:+.2f}%")
    print()

    results = []
    print(f"{'Variant':<32} {'IS Ret':>9} {'OOS Ret':>9} {'IS-SPY':>9} {'OOS-SPY':>9} {'IS WR':>7} {'OOS WR':>7} {'IS Days':>8} {'OOS Days':>9}")
    print("-" * 110)
    for v in VARIANTS:
        is_res = run_variant(v, history, is_pairs)
        oos_res = run_variant(v, history, oos_pairs)
        is_alpha = is_res["return_pct"] - is_spy
        oos_alpha = oos_res["return_pct"] - oos_spy
        robust = "★" if (is_alpha > 0 and oos_alpha > 0) else " "
        print(f"{robust} {v.name:<30} {is_res['return_pct']:>8.2f}% {oos_res['return_pct']:>8.2f}% "
              f"{is_alpha:>8.2f}% {oos_alpha:>8.2f}% "
              f"{is_res['win_rate_pct']:>6.1f}% {oos_res['win_rate_pct']:>6.1f}% "
              f"{is_res['n_active_days']:>8d} {oos_res['n_active_days']:>9d}")
        results.append({
            "variant": asdict(v),
            "in_sample": is_res,
            "out_of_sample": oos_res,
            "is_alpha": round(is_alpha, 3),
            "oos_alpha": round(oos_alpha, 3),
            "robust": is_alpha > 0 and oos_alpha > 0,
        })

    robust_winners = [r for r in results if r["robust"]]
    print()
    print("=" * 80)
    print(f"ROBUST WINNERS (beat SPY on BOTH windows): {len(robust_winners)} of {len(VARIANTS)}")
    print("=" * 80)
    for w in robust_winners:
        v = w["variant"]
        print(f"\n  {v['name']}: {v['description']}")
        print(f"    IS alpha: {w['is_alpha']:+.2f}%  ({w['in_sample']['return_pct']:+.2f}% strategy vs {is_spy:+.2f}% SPY)")
        print(f"    OOS alpha: {w['oos_alpha']:+.2f}%  ({w['out_of_sample']['return_pct']:+.2f}% strategy vs {oos_spy:+.2f}% SPY)")
        print(f"    Win rate: {w['in_sample']['win_rate_pct']}% IS / {w['out_of_sample']['win_rate_pct']}% OOS")
        print(f"    Sharpe:   {w['in_sample']['sharpe']} IS / {w['out_of_sample']['sharpe']} OOS")

    if not robust_winners:
        print("\nNo variant beat SPY on both windows. Honest conclusion:")
        print("  This universe + bracket-order framework does not have an edge.")
        print("  Don't deploy. Consider: longer holding period, different universe,")
        print("  or a totally different signal (not premarket gaps).")

    out_path = config.DATA_DIR / "strategy_explorer.json"
    out_path.write_text(json.dumps({
        "ran_at": datetime.now().isoformat(),
        "in_sample_days": len(is_pairs),
        "out_of_sample_days": len(oos_pairs),
        "in_sample_spy_pct": round(is_spy, 3),
        "out_of_sample_spy_pct": round(oos_spy, 3),
        "results": results,
    }, indent=2))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
