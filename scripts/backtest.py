"""Backtest the picker strategy over historical trading days.

Replays:
  1. Pull historical daily OHLCV for the universe.
  2. For each trading day D, compute the "premarket" gap (D-1 close -> D open).
  3. Try to pull historical news (D-1 close to D open) from Alpaca.
  4. Apply earnings filter.
  5. Call Claude picker with that day's snapshot.
  6. Simulate bracket order outcome using D's high/low/close.
  7. Aggregate stats. Write data/backtest.json. Print summary.

Conservative assumptions (bias against the strategy):
- If both stop and target were touched intraday, stop fills first.
  Without tick data we don't know which came first; assume the bad outcome.
- Slippage and commission set to zero (Alpaca is commission-free; slippage
  for our liquid universe is small but not zero — actual returns may be
  slightly worse than backtest).
- Position sizing: equal-weight across picks, capped at MAX_EQUITY_PER_TRADE_PCT.
"""
from __future__ import annotations
import sys
import json
import argparse
from datetime import datetime, timedelta, date, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf
import pandas as pd

from src import config, news, earnings, picker  # noqa: E402


def fetch_history(symbols: list[str], days: int) -> dict[str, pd.DataFrame]:
    """Bulk-fetch OHLCV. Returns {symbol: df} indexed by date."""
    # yf.download with group_by='ticker' gives a multi-index frame
    print(f"[backtest] fetching {days} days of OHLCV for {len(symbols)} symbols...")
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.7) + 10)  # weekends + buffer
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
    print(f"[backtest] got history for {len(out)} symbols")
    return out


def simulate_bracket_outcome(open_p: float, high: float, low: float, close: float) -> tuple[float, str]:
    """Return (pct_pnl, exit_reason) for a long bracket: -1% stop, +2% target."""
    stop = open_p * (1 - config.STOP_LOSS_PCT / 100)
    target = open_p * (1 + config.TAKE_PROFIT_PCT / 100)

    hit_stop = low <= stop
    hit_target = high >= target

    if hit_stop and hit_target:
        # Conservative: assume stop filled first.
        return (-config.STOP_LOSS_PCT, "stop (conservative — both touched)")
    if hit_target:
        return (config.TAKE_PROFIT_PCT, "target")
    if hit_stop:
        return (-config.STOP_LOSS_PCT, "stop")
    # Neither — exit at EOD (3:55pm flatten).
    pct = (close - open_p) / open_p * 100
    return (pct, "eod_close")


def build_movers_for_day(history: dict[str, pd.DataFrame], today: date, prev_day: date) -> list[dict]:
    """Synthesize the premarket snapshot the picker would have seen."""
    movers = []
    for sym, df in history.items():
        try:
            today_ts = pd.Timestamp(today)
            prev_ts = pd.Timestamp(prev_day)
            if today_ts not in df.index or prev_ts not in df.index:
                continue
            prev_close = float(df.loc[prev_ts, "Close"])
            today_open = float(df.loc[today_ts, "Open"])
            if prev_close <= 0:
                continue
            pct = (today_open - prev_close) / prev_close * 100
            movers.append({
                "symbol": sym,
                "prev_close": round(prev_close, 2),
                "premarket_price": round(today_open, 2),
                "pct_change": round(pct, 2),
                "volume": int(df.loc[prev_ts, "Volume"]),
            })
        except (KeyError, ValueError):
            continue
    movers.sort(key=lambda r: abs(r["pct_change"]), reverse=True)
    return movers[:15]  # top 15 by |%|, matching production


def historical_news(symbols: list[str], when: date) -> dict[str, list[dict]]:
    """Fetch news from (when - 1d 4pm ET) to (when 9:30am ET)."""
    try:
        end = datetime.combine(when, datetime.min.time(), tzinfo=timezone.utc)
        # Approximate: 16h window before market open UTC ~= 13:30 UTC
        end = end.replace(hour=13, minute=30)
        start = end - timedelta(hours=16)
        from alpaca.data.requests import NewsRequest
        from src.news import client as news_client
        req = NewsRequest(
            symbols=",".join(symbols),
            start=start,
            end=end,
            limit=50,
            sort="desc",
            exclude_contentless=True,
        )
        resp = news_client().get_news(req)
        articles = resp.data.get("news", [])
        by_symbol: dict[str, list[dict]] = {s: [] for s in symbols}
        for n in articles:
            for s in (n.symbols or []):
                if s in by_symbol and len(by_symbol[s]) < 3:
                    by_symbol[s].append({
                        "headline": n.headline,
                        "summary": (n.summary or "")[:240],
                        "source": n.source,
                        "url": n.url or "",
                        "ts": n.created_at.isoformat() if n.created_at else "",
                    })
        return by_symbol
    except Exception as e:
        print(f"[backtest]   news fetch failed for {when}: {e}")
        return {s: [] for s in symbols}


def max_drawdown(equity_curve: list[float]) -> float:
    """Return max peak-to-trough drawdown as a % (negative number)."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = (v - peak) / peak * 100
        max_dd = min(max_dd, dd)
    return max_dd


def annualized_sharpe(daily_returns: list[float]) -> float:
    """Crude Sharpe assuming 252 trading days, risk-free rate 0."""
    if len(daily_returns) < 2:
        return 0.0
    import statistics
    mean = statistics.mean(daily_returns)
    sd = statistics.stdev(daily_returns)
    if sd == 0:
        return 0.0
    return (mean / sd) * (252 ** 0.5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60, help="Calendar days to look back")
    parser.add_argument("--no-news", action="store_true", help="Skip historical news fetch (faster, price-only strategy)")
    parser.add_argument("--no-claude", action="store_true", help="Use naive 'pick top 2 gappers' instead of Claude (for sanity check)")
    args = parser.parse_args()

    universe = config.TRADABLE_UNIVERSE
    history = fetch_history(universe, args.days)
    if not history:
        print("[backtest] no history fetched — aborting")
        return 1

    # Trading days = intersection of all symbol indices, take the most recent N
    spy_df = history.get("SPY")
    if spy_df is None or spy_df.empty:
        print("[backtest] need SPY for benchmark — aborting")
        return 1
    trading_days = list(spy_df.index)
    # Need a prev day for each, so skip the first one.
    pairs = [(trading_days[i].date(), trading_days[i-1].date()) for i in range(1, len(trading_days))]

    print(f"[backtest] simulating {len(pairs)} trading days...")

    equity = 1.0  # start at 1.0 (representing $1)
    equity_curve = [equity]
    daily_results: list[dict] = []
    earnings_cache: dict = {}

    for i, (today, prev) in enumerate(pairs):
        movers = build_movers_for_day(history, today, prev)
        if not movers:
            continue

        # Earnings filter
        safe_syms, blocked = earnings.filter_out_earnings_risk(
            [m["symbol"] for m in movers], min_days_buffer=1
        )
        movers = [m for m in movers if m["symbol"] in safe_syms]
        if not movers:
            daily_results.append({"date": str(today), "picks": [], "pnl_pct": 0, "note": f"all blocked by earnings: {list(blocked.keys())}"})
            equity_curve.append(equity)
            continue

        # Picker
        try:
            if args.no_claude:
                # Naive baseline: pick top 2 positive gappers > 1%
                gappers = [m for m in movers if m["pct_change"] > 1.0][:2]
                claude_picks = [{"symbol": m["symbol"], "confidence": "n/a", "catalyst": "naive baseline", "rationale": ""} for m in gappers]
                picks_resp = {"picks": claude_picks, "market_take": "naive baseline"}
            else:
                news_by_sym = {} if args.no_news else historical_news([m["symbol"] for m in movers], today)
                if args.no_news:
                    news_by_sym = {m["symbol"]: [] for m in movers}
                picks_resp = picker.pick_stocks(movers, news_by_sym)
        except Exception as e:
            print(f"[backtest] {today}: picker error: {e}")
            daily_results.append({"date": str(today), "picks": [], "pnl_pct": 0, "note": f"picker error: {e}"})
            equity_curve.append(equity)
            continue

        picks = picks_resp.get("picks", [])

        # Simulate outcomes
        day_pnl_pcts = []
        pick_outcomes = []
        budget_pct = min(config.MAX_EQUITY_PER_TRADE_PCT / 100, 1.0 / max(len(picks), 1))

        for p in picks:
            sym = p["symbol"]
            df = history.get(sym)
            today_ts = pd.Timestamp(today)
            if df is None or today_ts not in df.index:
                continue
            row = df.loc[today_ts]
            open_p = float(row["Open"])
            high = float(row["High"])
            low = float(row["Low"])
            close = float(row["Close"])
            pct, exit_reason = simulate_bracket_outcome(open_p, high, low, close)
            # Contribution to portfolio pnl, weighted by position size
            day_pnl_pcts.append(pct * budget_pct)
            pick_outcomes.append({
                "symbol": sym,
                "catalyst": p.get("catalyst", ""),
                "open": round(open_p, 2),
                "pct": round(pct, 3),
                "exit": exit_reason,
            })

        day_pnl = sum(day_pnl_pcts)  # already percentage points of total equity
        equity *= (1 + day_pnl / 100)
        equity_curve.append(equity)
        daily_results.append({
            "date": str(today),
            "n_picks": len(picks),
            "pnl_pct": round(day_pnl, 4),
            "outcomes": pick_outcomes,
        })
        print(f"[backtest] {today}: {len(picks)} picks, day P&L {day_pnl:+.3f}%, equity={equity:.4f}")

    # Aggregate
    strategy_return_pct = (equity - 1) * 100
    daily_returns = [d["pnl_pct"] for d in daily_results if d.get("n_picks", 0) > 0]
    win_days = sum(1 for r in daily_returns if r > 0)
    lose_days = sum(1 for r in daily_returns if r < 0)
    trading_days_with_picks = len(daily_returns)
    avg_win = sum(r for r in daily_returns if r > 0) / max(win_days, 1)
    avg_loss = sum(r for r in daily_returns if r < 0) / max(lose_days, 1)
    win_rate = win_days / max(trading_days_with_picks, 1) * 100

    spy_first = float(spy_df.iloc[0]["Close"])
    spy_last = float(spy_df.iloc[-1]["Close"])
    spy_return_pct = (spy_last / spy_first - 1) * 100

    summary = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": args.days,
        "trading_days_simulated": len(pairs),
        "trading_days_with_picks": trading_days_with_picks,
        "no_news": args.no_news,
        "no_claude": args.no_claude,
        "strategy_return_pct": round(strategy_return_pct, 3),
        "spy_return_pct": round(spy_return_pct, 3),
        "alpha_pct": round(strategy_return_pct - spy_return_pct, 3),
        "win_rate_pct": round(win_rate, 2),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "max_drawdown_pct": round(max_drawdown(equity_curve), 3),
        "sharpe_annualized": round(annualized_sharpe(daily_returns), 3),
        "weekly_pace_pct": round(strategy_return_pct / max(trading_days_with_picks, 1) * 5, 3),
        "target_weekly_pct": config.WEEKLY_TARGET_PCT,
        "daily": daily_results,
    }

    out_path = config.DATA_DIR / "backtest.json"
    out_path.write_text(json.dumps(summary, indent=2))

    print()
    print("=" * 60)
    print("BACKTEST SUMMARY")
    print("=" * 60)
    print(f"  Lookback:           {args.days} calendar days ({len(pairs)} trading days)")
    print(f"  Strategy return:    {strategy_return_pct:+.2f}%")
    print(f"  SPY return:         {spy_return_pct:+.2f}%")
    print(f"  Alpha:              {summary['alpha_pct']:+.2f}%")
    print(f"  Win rate:           {win_rate:.1f}%  ({win_days}W / {lose_days}L of {trading_days_with_picks} active days)")
    print(f"  Avg win:            +{avg_win:.2f}%")
    print(f"  Avg loss:           {avg_loss:.2f}%")
    print(f"  Max drawdown:       {summary['max_drawdown_pct']:.2f}%")
    print(f"  Annualized Sharpe:  {summary['sharpe_annualized']:.2f}")
    print(f"  Weekly pace:        {summary['weekly_pace_pct']:+.2f}%  (target +{config.WEEKLY_TARGET_PCT}%)")
    print()
    print(f"  Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
