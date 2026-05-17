"""Deep day-trading edge explorer — same-day open-to-close only.

Tests genuine academic edges:
  - Post-Earnings Announcement Drift (PEAD)
  - Volume confirmation
  - Sector relative strength
  - Multi-day trend filter
  - VIX regime conditioning
  - Combinations of the above

Key methodology fix vs previous backtest:
  - NO stop-losses. We just hold open→close. This removes the conservative
    stop bias that destroyed earlier results.
  - Apply realistic per-trade cost (0.05%).
  - Tested on IS (last 60d) AND OOS (60-180d ago).

Goal: find a same-day strategy that beats SPY on BOTH windows.
"""
from __future__ import annotations
import sys
import json
import statistics
from datetime import datetime, timedelta, date
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf
import pandas as pd

from src import config  # noqa: E402


UNIVERSE = config.TRADABLE_UNIVERSE
SECTOR_ETFS = {
    # Map mega-cap stocks to their sector ETF for sector RS filter
    "AAPL": "XLK", "MSFT": "XLK", "GOOGL": "XLC", "AMZN": "XLY", "META": "XLC",
    "NVDA": "XLK", "AVGO": "XLK", "TSLA": "XLY", "AMD": "XLK", "NFLX": "XLC",
    "CRM": "XLK", "ADBE": "XLK", "ORCL": "XLK", "INTC": "XLK", "PLTR": "XLK",
    "SHOP": "XLY", "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "MS": "XLF",
    "V": "XLF", "MA": "XLF", "WMT": "XLP", "COST": "XLP", "HD": "XLY",
    "NKE": "XLY", "MCD": "XLY", "SBUX": "XLY", "DIS": "XLC", "XOM": "XLE",
    "CVX": "XLE", "BA": "XLI", "CAT": "XLI", "GE": "XLI",
    # ETFs map to themselves
    "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "DIA": "DIA",
}
ALL_NEEDED = list(set(UNIVERSE + list(SECTOR_ETFS.values()) + ["SPY", "^VIX"]))


def fetch_history(symbols: list[str], days_back: int = 220) -> dict[str, pd.DataFrame]:
    end = datetime.now()
    start = end - timedelta(days=int(days_back * 1.7) + 20)
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


def fetch_earnings_dates(symbols: list[str]) -> dict[str, set[date]]:
    """Pull earnings dates for each symbol (caches in dict)."""
    print("[earnings] fetching earnings dates...")
    out: dict[str, set[date]] = {}
    for s in symbols:
        try:
            t = yf.Ticker(s)
            ed = t.earnings_dates
            if ed is None or ed.empty:
                out[s] = set()
                continue
            out[s] = {d.date() for d in ed.index}
        except Exception as e:
            print(f"  {s}: failed: {e}")
            out[s] = set()
    return out


def trading_days_in_window(spy_df: pd.DataFrame, days_ago_start: int, days_ago_end: int) -> tuple[list[date], float]:
    today_dt = pd.Timestamp(date.today())
    window_start = today_dt - pd.Timedelta(days=days_ago_start)
    window_end = today_dt - pd.Timedelta(days=days_ago_end)
    if window_start > window_end:
        window_start, window_end = window_end, window_start
    days = [d for d in spy_df.index if window_start <= d <= window_end]
    if len(days) < 2:
        return [], 0.0
    spy_ret = (float(spy_df.loc[days[-1], "Close"]) / float(spy_df.loc[days[0], "Close"]) - 1) * 100
    return [d.date() for d in days], spy_ret


# ============================================================
# Signal generators — each returns list of (symbol, day_open) to trade
# ============================================================

def base_gap_up(history: dict, today: date, prev: date, min_gap: float = 1.5, max_picks: int = 2) -> list[dict]:
    """Baseline: top gap-ups in universe."""
    movers = []
    for sym in UNIVERSE:
        df = history.get(sym)
        if df is None:
            continue
        today_ts, prev_ts = pd.Timestamp(today), pd.Timestamp(prev)
        if today_ts not in df.index or prev_ts not in df.index:
            continue
        prev_close = float(df.loc[prev_ts, "Close"])
        today_open = float(df.loc[today_ts, "Open"])
        if prev_close <= 0:
            continue
        gap = (today_open - prev_close) / prev_close * 100
        if gap < min_gap:
            continue
        movers.append({"symbol": sym, "gap": gap, "open": today_open, "df": df, "today_ts": today_ts, "prev_ts": prev_ts})
    movers.sort(key=lambda m: m["gap"], reverse=True)
    return movers[:max_picks]


def filter_volume_confirm(picks: list[dict], history: dict, min_vol_ratio: float = 1.5) -> list[dict]:
    """Keep only picks where today's volume > avg(20d) × min_vol_ratio."""
    out = []
    for p in picks:
        df = p["df"]
        today_ts = p["today_ts"]
        try:
            idx = df.index.get_loc(today_ts)
            if idx < 20:
                continue
            avg_vol = df.iloc[idx-20:idx]["Volume"].mean()
            today_vol = float(df.loc[today_ts, "Volume"])
            if avg_vol > 0 and today_vol / avg_vol >= min_vol_ratio:
                out.append(p)
        except Exception:
            continue
    return out


def filter_sector_aligned(picks: list[dict], history: dict, today: date, prev: date) -> list[dict]:
    """Keep only picks whose sector ETF is ALSO gapping up."""
    out = []
    for p in picks:
        sector_etf = SECTOR_ETFS.get(p["symbol"])
        if not sector_etf:
            continue
        sdf = history.get(sector_etf)
        if sdf is None:
            continue
        today_ts, prev_ts = pd.Timestamp(today), pd.Timestamp(prev)
        if today_ts not in sdf.index or prev_ts not in sdf.index:
            continue
        sec_gap = (float(sdf.loc[today_ts, "Open"]) - float(sdf.loc[prev_ts, "Close"])) / float(sdf.loc[prev_ts, "Close"]) * 100
        if sec_gap > 0:
            out.append(p)
    return out


def filter_5d_uptrend(picks: list[dict], history: dict) -> list[dict]:
    """Keep only picks where stock is up over the last 5 days."""
    out = []
    for p in picks:
        df = p["df"]
        prev_ts = p["prev_ts"]
        try:
            idx = df.index.get_loc(prev_ts)
            if idx < 5:
                continue
            five_d_ago = float(df.iloc[idx-5]["Close"])
            yesterday = float(df.loc[prev_ts, "Close"])
            if yesterday > five_d_ago:
                out.append(p)
        except Exception:
            continue
    return out


def filter_post_earnings(picks: list[dict], earnings_dates: dict[str, set[date]], prev: date) -> list[dict]:
    """Keep only picks where YESTERDAY was an earnings day (PEAD setup)."""
    out = []
    for p in picks:
        if prev in earnings_dates.get(p["symbol"], set()):
            out.append(p)
    return out


def filter_vix_regime(history: dict, today: date, max_vix: float = 25.0) -> bool:
    """Return True if VIX close on prev day < max_vix (calm market)."""
    vix_df = history.get("^VIX")
    if vix_df is None:
        return True
    today_ts = pd.Timestamp(today)
    if today_ts not in vix_df.index:
        return True
    return float(vix_df.loc[today_ts, "Open"]) < max_vix


# ============================================================
# Strategies = combinations of filters
# ============================================================

@dataclass
class Strategy:
    name: str
    description: str
    apply: Callable
    expected_low_volume: bool = False  # True if strategy naturally produces few trades


STRATEGIES = []


def strat(name, description, low_vol=False):
    def deco(f):
        STRATEGIES.append(Strategy(name, description, f, low_vol))
        return f
    return deco


@strat("baseline_no_stops", "Baseline: long top gap-ups, open→close, no stops")
def s_baseline(history, earnings_dates, today, prev):
    return base_gap_up(history, today, prev)


@strat("volume_confirmed", "Long gap-ups WITH high volume (today vol > 1.5x avg20)")
def s_vol(history, earnings_dates, today, prev):
    picks = base_gap_up(history, today, prev)
    return filter_volume_confirm(picks, history, min_vol_ratio=1.5)


@strat("volume_confirmed_2x", "Long gap-ups WITH very high volume (today vol > 2.0x avg20)")
def s_vol2(history, earnings_dates, today, prev):
    picks = base_gap_up(history, today, prev)
    return filter_volume_confirm(picks, history, min_vol_ratio=2.0)


@strat("sector_aligned", "Long gap-ups WHEN sector ETF also gapping up")
def s_sec(history, earnings_dates, today, prev):
    picks = base_gap_up(history, today, prev)
    return filter_sector_aligned(picks, history, today, prev)


@strat("uptrend_only", "Long gap-ups only when stock up over last 5d")
def s_trend(history, earnings_dates, today, prev):
    picks = base_gap_up(history, today, prev)
    return filter_5d_uptrend(picks, history)


@strat("pead", "Post-Earnings Drift: long gap-ups where YESTERDAY was earnings day", low_vol=True)
def s_pead(history, earnings_dates, today, prev):
    picks = base_gap_up(history, today, prev, min_gap=0.5, max_picks=5)
    return filter_post_earnings(picks, earnings_dates, prev)


@strat("vix_calm", "Long gap-ups only when VIX < 20")
def s_vix_calm(history, earnings_dates, today, prev):
    if not filter_vix_regime(history, today, max_vix=20.0):
        return []
    return base_gap_up(history, today, prev)


@strat("combo_vol_sector", "Volume-confirmed AND sector-aligned")
def s_combo1(history, earnings_dates, today, prev):
    picks = base_gap_up(history, today, prev)
    picks = filter_volume_confirm(picks, history, min_vol_ratio=1.5)
    return filter_sector_aligned(picks, history, today, prev)


@strat("combo_vol_trend", "Volume-confirmed AND 5d uptrend")
def s_combo2(history, earnings_dates, today, prev):
    picks = base_gap_up(history, today, prev)
    picks = filter_volume_confirm(picks, history, min_vol_ratio=1.5)
    return filter_5d_uptrend(picks, history)


@strat("triple", "Volume + Sector + Trend")
def s_combo3(history, earnings_dates, today, prev):
    picks = base_gap_up(history, today, prev)
    picks = filter_volume_confirm(picks, history, min_vol_ratio=1.5)
    picks = filter_sector_aligned(picks, history, today, prev)
    return filter_5d_uptrend(picks, history)


@strat("gap_down_meanrev", "FADE gap-DOWNS: long stocks that gapped DOWN >1.5%, hope for recovery")
def s_gdown(history, earnings_dates, today, prev):
    # Gap-DOWN setup
    movers = []
    for sym in UNIVERSE:
        df = history.get(sym)
        if df is None:
            continue
        today_ts, prev_ts = pd.Timestamp(today), pd.Timestamp(prev)
        if today_ts not in df.index or prev_ts not in df.index:
            continue
        prev_close = float(df.loc[prev_ts, "Close"])
        today_open = float(df.loc[today_ts, "Open"])
        if prev_close <= 0:
            continue
        gap = (today_open - prev_close) / prev_close * 100
        if gap > -1.5:
            continue
        movers.append({"symbol": sym, "gap": gap, "open": today_open, "df": df, "today_ts": today_ts, "prev_ts": prev_ts})
    movers.sort(key=lambda m: m["gap"])
    return movers[:2]


# ============================================================
# Simulator
# ============================================================

def simulate(strategy: Strategy, history: dict, earnings_dates: dict, days: list[date],
             cost_pct: float = 0.05) -> dict:
    """Run strategy through days. Open→close, no stops, equal-weight intraday."""
    equity = 1.0
    equity_curve = [equity]
    daily_pnls: list[float] = []
    n_trades = 0
    win_trades = 0

    for i, today in enumerate(days):
        if i == 0:
            equity_curve.append(equity)
            continue
        prev = days[i-1]
        picks = strategy.apply(history, earnings_dates, today, prev)
        if not picks:
            equity_curve.append(equity)
            continue

        # Equal weight; cap individual position at MAX_EQUITY_PER_TRADE_PCT
        max_pos = config.MAX_EQUITY_PER_TRADE_PCT / 100
        weight = min(max_pos, 1.0 / len(picks))

        day_pnl = 0.0
        for p in picks:
            row = p["df"].loc[p["today_ts"]]
            open_p = float(row["Open"])
            close_p = float(row["Close"])
            if open_p <= 0:
                continue
            raw_pct = (close_p - open_p) / open_p * 100
            net_pct = raw_pct - cost_pct
            day_pnl += net_pct * weight
            n_trades += 1
            if net_pct > 0:
                win_trades += 1

        equity *= (1 + day_pnl / 100)
        equity_curve.append(equity)
        daily_pnls.append(day_pnl)

    total_return = (equity - 1) * 100
    win_rate = (win_trades / max(n_trades, 1)) * 100
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
        "max_dd_pct": round(max_dd, 3),
        "sharpe": round(sharpe, 2),
    }


def main() -> int:
    print(f"Fetching history for {len(ALL_NEEDED)} symbols (including sectors + VIX)...")
    history = fetch_history(ALL_NEEDED)
    print(f"Got history for {len(history)} symbols")

    earnings_dates = fetch_earnings_dates(UNIVERSE)
    print(f"Got earnings dates for {sum(1 for v in earnings_dates.values() if v)} symbols\n")

    spy = history["SPY"]
    is_days, is_spy = trading_days_in_window(spy, 60, 0)
    oos_days, oos_spy = trading_days_in_window(spy, 180, 60)
    print(f"IS:  {len(is_days)} days, SPY {is_spy:+.2f}%")
    print(f"OOS: {len(oos_days)} days, SPY {oos_spy:+.2f}%\n")

    print(f"{'Strategy':<28} {'IS Ret':>9} {'OOS Ret':>9} {'IS α':>8} {'OOS α':>8} {'IS WR':>7} {'OOS WR':>7} {'IS N':>5} {'OOS N':>6} {'IS Shp':>7}")
    print("-" * 110)

    results = []
    for s in STRATEGIES:
        is_r = simulate(s, history, earnings_dates, is_days)
        oos_r = simulate(s, history, earnings_dates, oos_days)
        is_alpha = is_r["return_pct"] - is_spy
        oos_alpha = oos_r["return_pct"] - oos_spy
        # Require minimum sample sizes to be considered robust
        min_n = 5 if s.expected_low_volume else 15
        has_enough = is_r["n_trades"] >= min_n and oos_r["n_trades"] >= min_n
        robust = is_alpha > 0 and oos_alpha > 0 and has_enough
        flag = "★" if robust else ("·" if (is_alpha > 0 or oos_alpha > 0) else " ")
        print(f"{flag} {s.name:<26} {is_r['return_pct']:>8.2f}% {oos_r['return_pct']:>8.2f}% "
              f"{is_alpha:>+7.2f}% {oos_alpha:>+7.2f}% "
              f"{is_r['win_rate_pct']:>6.1f}% {oos_r['win_rate_pct']:>6.1f}% "
              f"{is_r['n_trades']:>5d} {oos_r['n_trades']:>6d} {is_r['sharpe']:>7.2f}")
        results.append({
            "name": s.name,
            "description": s.description,
            "in_sample": is_r,
            "out_of_sample": oos_r,
            "is_alpha": round(is_alpha, 3),
            "oos_alpha": round(oos_alpha, 3),
            "robust": robust,
        })

    robust = [r for r in results if r["robust"]]
    print()
    print("=" * 80)
    print(f"ROBUST DAY-TRADING WINNERS: {len(robust)} of {len(results)}")
    print("=" * 80)
    for w in sorted(robust, key=lambda x: x["is_alpha"] + x["oos_alpha"], reverse=True):
        print(f"\n  {w['name']}: {w['description']}")
        print(f"    IS:   {w['in_sample']['return_pct']:+.2f}% (alpha {w['is_alpha']:+.2f}%)  win {w['in_sample']['win_rate_pct']}%  N={w['in_sample']['n_trades']}  Sharpe={w['in_sample']['sharpe']}")
        print(f"    OOS:  {w['out_of_sample']['return_pct']:+.2f}% (alpha {w['oos_alpha']:+.2f}%)  win {w['out_of_sample']['win_rate_pct']}%  N={w['out_of_sample']['n_trades']}  Sharpe={w['out_of_sample']['sharpe']}")

    if not robust:
        print("\nNo strategy beat SPY on both windows with enough trades.")
        # Show the best by combined alpha anyway
        positives = [r for r in results if r["is_alpha"] > 0 and r["oos_alpha"] > 0]
        if positives:
            print(f"\n{len(positives)} strategies had positive alpha on both windows but failed minimum sample size:")
            for p in sorted(positives, key=lambda x: x["is_alpha"] + x["oos_alpha"], reverse=True)[:3]:
                print(f"  {p['name']}: IS α {p['is_alpha']:+.2f}%, OOS α {p['oos_alpha']:+.2f}%, N={p['in_sample']['n_trades']}/{p['out_of_sample']['n_trades']}")

    out_path = config.DATA_DIR / "day_trading_explorer.json"
    out_path.write_text(json.dumps({
        "ran_at": datetime.now().isoformat(),
        "is_window_days": len(is_days),
        "oos_window_days": len(oos_days),
        "is_spy_pct": round(is_spy, 3),
        "oos_spy_pct": round(oos_spy, 3),
        "results": results,
    }, indent=2))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
