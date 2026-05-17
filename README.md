# scott-bot-trader

A small autonomous day-trading bot. Runs on paper money via Alpaca. Picks 1-3 stocks each morning using Claude, submits bracket orders, and flattens everything by 3:55pm ET.

**Goal:** beat the market — target +1.5% per week.

**Status:** paper trading only. Live trading is gated behind a hard guard in `src/config.py`.

---

## How it works

1. **9:35am ET** (GHA cron) — fetch premarket movers for ~35 liquid US stocks, ask Claude to pick 1-3 long trades, submit bracket orders (entry + stop-loss at -1% + take-profit at +2%).
2. **3:55pm ET** (GHA cron) — close any open positions. Same-day exit enforced.
3. Each run appends to `data/trades.json` and auto-commits it back to the repo.

Bracket orders mean the bot doesn't have to "watch" the market — Alpaca handles the exits server-side. GHA cron jitter doesn't matter.

### Safety rails (in `src/safety.py`)

- Refuses to trade if today's P&L is already down 5%.
- Refuses if PDT flag is set.
- Caps each pick at 33% of equity.
- Refuses to run in `TRADING_MODE=live` unless code is changed (paper-only by construction).

---

## Setup

### 1. Clone + install
```bash
cd ~/Documents/scott-bot-trader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Copy `.env`
```bash
cp .env.example .env
# fill in your Alpaca paper keys + Anthropic API key
```

### 3. Smoke test
```bash
python scripts/check_account.py
```
Should print account #, equity ($100k for paper), market clock, etc.

### 4. Manual run
```bash
python scripts/morning_run.py   # picks + bracket orders
python scripts/eod_flatten.py   # close everything
```

---

## GitHub Actions setup

1. Push this repo to GitHub.
2. In repo **Settings → Secrets and variables → Actions**, add:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
   - `ANTHROPIC_API_KEY`
3. The crons will fire automatically Mon-Fri. You can also trigger manually via **Actions → morning-run / eod-flatten → Run workflow**.

---

## Going live (later)

When you're ready to risk real money:

1. Fund a **separate Alpaca live account** (start with $100-$1000).
2. Generate live API keys (they start with `AK`, not `PK`).
3. Update GHA secrets to the live keys.
4. Change `ALPACA_BASE_URL` to `https://api.alpaca.markets` in both workflow files.
5. Set `TRADING_MODE=live` and **remove the guard at the top of `src/config.py`**.
6. Optional but recommended: lower `MAX_EQUITY_PER_TRADE_PCT` to 25% and `MAX_PICKS_PER_DAY` to 1-2 for the first few weeks.

Don't do this until you've watched paper trading for at least 2-4 weeks.

---

## Files

```
src/
  config.py         — env + strategy constants
  alpaca_client.py  — thin Alpaca wrapper
  data.py           — premarket movers (yfinance)
  picker.py         — Claude-powered stock picker
  trader.py         — bracket order submission
  flatten.py        — EOD close-all
  safety.py         — kill switch + preflight checks
  tracker.py        — P&L log + weekly goal tracking
scripts/
  morning_run.py    — entry point for 9:35am cron
  eod_flatten.py    — entry point for 3:55pm cron
  check_account.py  — smoke test, no trading
.github/workflows/
  morning.yml       — daily morning cron
  flatten.yml       — daily EOD cron
data/
  trades.json       — auto-committed trade log
```
