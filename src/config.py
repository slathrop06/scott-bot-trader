"""Central config — env vars + strategy constants."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

# --- Secrets ---
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")  # optional but recommended
TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()

# Hard guard: this bot is paper-only until we explicitly flip it.
if TRADING_MODE == "live" and "paper" not in ALPACA_BASE_URL:
    raise RuntimeError(
        "TRADING_MODE=live but the code has not been audited for live trading. "
        "Refuse to start. Set TRADING_MODE=paper or remove this guard intentionally."
    )

# --- Strategy ---
WEEKLY_TARGET_PCT = 1.5            # Scott's goal: +1.5% / week
STOP_LOSS_PCT = 1.0                # bracket order stop-loss
TAKE_PROFIT_PCT = 2.0              # bracket order take-profit (2:1 reward:risk)
MAX_PICKS_PER_DAY = 3              # how many tickers Claude can pick
MAX_EQUITY_PER_TRADE_PCT = 33.0    # cap each pick at 1/3 of equity
DAILY_LOSS_KILL_SWITCH_PCT = 5.0   # halt for the day if down this much
MIN_BUYING_POWER_USD = 5000        # refuse to trade if buying power drops below this

# --- Universe ---
# Liquid, day-tradable, broad sector mix. Curated for $5–$500 range with tight spreads.
TRADABLE_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "TSLA",
    # Other tech / growth
    "AMD", "NFLX", "CRM", "ADBE", "ORCL", "INTC", "PLTR", "SHOP",
    # Finance
    "JPM", "BAC", "GS", "MS", "V", "MA",
    # Consumer
    "WMT", "COST", "HD", "NKE", "MCD", "SBUX", "DIS",
    # Energy / industrial
    "XOM", "CVX", "BA", "CAT", "GE",
    # ETFs (highly liquid)
    "SPY", "QQQ", "IWM", "DIA",
]

# --- Paths ---
DATA_DIR = ROOT / "data"
TRADES_LOG = DATA_DIR / "trades.json"
