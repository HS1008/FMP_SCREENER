"""
Central settings for the FMP screener project.

Edit values here instead of hunting through the codebase.
"""

from __future__ import annotations

from pathlib import Path

# --- Paths (project root = folder containing this file) ---
PROJECT_ROOT: Path = Path(__file__).resolve().parent
OUTPUT_DIR: Path = PROJECT_ROOT / "outputs"
CACHE_DIR: Path = OUTPUT_DIR / "cache" / "prices"
FUNDAMENTALS_CACHE_DIR: Path = OUTPUT_DIR / "cache" / "fundamentals"
# Full `profile-bulk` concat (all FMP parts) — reused by dispersion / fundamentals / valuation.
PROFILE_BULK_CACHE_PATH: Path = OUTPUT_DIR / "cache" / "profile_bulk_all.pkl"
PROFILE_BULK_CACHE_TTL_SECONDS: int = 6 * 3600  # refresh bulk profiles at most every 6h unless forced

# Legacy sector model workbook (optional / other scripts)
EXCEL_OUTPUT: Path = OUTPUT_DIR / "sector_model.xlsx"

# --- Ranking engine output ---
RANKING_EXCEL_OUTPUT: Path = OUTPUT_DIR / "top_10_by_sector_zscore.xlsx"

# Multi-factor model (Quality / Value / Momentum / Risk)
FINAL_SECTOR_MODEL_OUTPUT: Path = OUTPUT_DIR / "final_sector_model.xlsx"
# Default universe size for multifactor run (avoid pulling tens of thousands of names).
MULTIFACTOR_UNIVERSE_TOP_N: int = 1000
# Print full-universe momentum diagnostics before top-10 filtering.
PRINT_FULL_UNIVERSE_DIAGNOSTICS: bool = True
RISK_FILTER_MAX_DRAWDOWN_PCT: float = 60.0
RISK_FILTER_MAX_SECTOR_VOL_PERCENTILE: float = 0.90
RISK_FILTER_REQUIRE_ABOVE_200DMA: bool = False

MAX_STOCK_WEIGHT: float = 0.10
MIN_STOCK_WEIGHT: float = 0.005

SECTOR_OVERWEIGHT_MULTIPLIER: float = 1.25
SECTOR_UNDERWEIGHT_MULTIPLIER: float = 0.75
SECTOR_NEUTRAL_MULTIPLIER: float = 1.00

TOP_SECTOR_COUNT: int = 3
BOTTOM_SECTOR_COUNT: int = 3

PORTFOLIO_OUTPUT: Path = OUTPUT_DIR / "portfolio_engine.xlsx"

# --- FMP ---
FMP_BASE_URL: str = "https://financialmodelingprep.com/stable"

# --- Sector ETF proxies (also used as "valid sector" list for ranking universe) ---
SECTOR_ETF_MAP: dict[str, str] = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
}

RANKING_VALID_SECTORS: frozenset[str] = frozenset(SECTOR_ETF_MAP.keys())

# --- Universe filters ---
MIN_MARKET_CAP: int = 500_000_000  # USD (alias in docs: min_market_cap)
MIN_AVG_VOLUME: int = 250_000  # screener `volumeMoreThan`
MIN_VOLUME: int = MIN_AVG_VOLUME  # alias for specs / readability (min_volume)

# --- Technology internal dispersion (dashboard `dispersion_engine`) ---
DISPERSION_MIN_MARKET_CAP: int = 2_000_000_000
DISPERSION_MIN_AVG_VOLUME: int = 750_000
DISPERSION_MIN_PRICE: float = 10.0
DISPERSION_ALLOWED_EXCHANGES: frozenset[str] = frozenset(
    {"NASDAQ", "NYSE", "AMEX", "NYSEARCA", "BATS"}
)
# Rolling return window (~1 calendar month of trading sessions).
DISPERSION_RETURN_TRADING_DAYS: int = 21
# Pairwise correlation uses this many trailing daily returns.
DISPERSION_CORR_TRADING_DAYS: int = 60
# Breadth / dispersion time-series length (calendar days back from `as_of`).
DISPERSION_CHART_LOOKBACK_DAYS: int = 380
# Extra calendar history before the chart window so 200-DMA rolling windows are valid
# across the full visible breadth series (not shown; filtered in breadth_time_series).
DISPERSION_DMA_WARMUP_DAYS: int = 420
# Calendar span of EOD prices pulled for dispersion (one knob; was chart+warmup ≈ 800d).
# ~620d is enough for ~270 trading rows (200-DMA + 21d returns + chart). Raise if charts truncate.
DISPERSION_PRICE_HISTORY_CAL_DAYS: int = 620
# Dispersion σ time-series: 1 = every session; 2–5 skips rows for faster CPU (chart slightly coarser).
DISPERSION_TS_STRIDE: int = 1

# Major US listing venues (exclude OTC / pink sheets)
US_MAJOR_EXCHANGES: frozenset[str] = frozenset(
    {
        "NYSE",
        "NASDAQ",
        "AMEX",
        "NYSEAMERICAN",
        "NYSEARCA",
        "BATS",
    }
)

# --- Universe sizing ---
# Screener pulls candidates; we keep top N by market cap before fundamentals (API heavy).
RANKING_UNIVERSE_TOP_N: int = 2000

# Default for `get_stock_universe(..., top_n=None)` (other / legacy callers).
STOCK_UNIVERSE_TOP_N: int = 1000

# Legacy sector-model windows (optional scripts).
DEFAULT_WINDOWS: tuple[str, ...] = ("1W", "1M", "YTD")

# Price history lookback (calendar days) — must cover 252 trading sessions + YTD buffer.
PRICE_HISTORY_LOOKBACK_DAYS: int = 450

# --- Trading windows ---
TRADING_DAYS_1W: int = 5
TRADING_DAYS_1M: int = 21
TRADING_DAYS_3M: int = 63  # ~3 calendar months of trading sessions
TRADING_DAYS_1Y: int = 252  # ranking + legacy helpers

# --- Risk-free (annual, decimal) for Sharpe and specs (rf_rate) ---
RISK_FREE_RATE: float = 0.045
RF_RATE: float = RISK_FREE_RATE

# --- Runtime ---
HTTP_TIMEOUT_S: int = 120
HTTP_MAX_RETRIES: int = 3
HTTP_BACKOFF_S: float = 1.5
# Parallel FMP historical-price pulls (FMP has no bulk dividend-adjusted time-series endpoint).
PRICE_FETCH_MAX_WORKERS: int = 10

# Streamlit @st.cache_data TTL for dashboard dispersion + rotation bundles (seconds).
DASHBOARD_CACHE_TTL_SECONDS: int = 3600
