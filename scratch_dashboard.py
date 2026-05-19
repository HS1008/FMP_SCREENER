"""
Scratch Dashboard — experimental Streamlit sandbox for scrap analysis.

Not wired to production engines or dashboard.py. Paste experiments into the
render_* functions below.

Run:
  python run_scratch_dashboard.py
  # or: streamlit run scratch_dashboard.py
"""

from __future__ import annotations

import io
import os
import traceback
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import config
import data_loader

# Optional default workbook (edit path for local dev; uploader overrides in UI).
DEFAULT_EXCEL_PATH = Path(
    r"c:\Users\dipka\OneDrive - Rutgers University\Fortius Financial"
    r"\brokerage-mutual-fund-availabilty-list.xlsx"
)

# ---------------------------------------------------------------------------
# Analysis type labels (sidebar selectbox)
# ---------------------------------------------------------------------------
ANALYSIS_BLANK = "Blank"
ANALYSIS_PRICE_TEST = "Price Test"
ANALYSIS_RELATIVE_STRENGTH = "Relative Strength Test"
ANALYSIS_FACTOR_SCRAP = "Factor Scrap"
ANALYSIS_HEDGE_SIMULATOR = "Hedge Simulator"
ANALYSIS_TOP_100_10Y = "Top 100 10Y Performers"

ANALYSIS_OPTIONS: tuple[str, ...] = (
    ANALYSIS_BLANK,
    ANALYSIS_PRICE_TEST,
    ANALYSIS_RELATIVE_STRENGTH,
    ANALYSIS_FACTOR_SCRAP,
    ANALYSIS_HEDGE_SIMULATOR,
    ANALYSIS_TOP_100_10Y,
)

TICKER_COLUMN_CANDIDATES: tuple[str, ...] = (
    "Symbol",
    "Ticker",
    "Fund Symbol",
    "Security",
    "SYMBOL",
    "TICKER",
)

METRIC_COLUMNS: tuple[str, ...] = (
    "ticker",
    "return_3m",
    "return_6m",
    "return_12m",
    "return_3y",
    "return_5y",
    "return_10y",
    "max_drawdown",
    "annualized_volatility",
)

HISTORY_YEARS = 10
HISTORY_BUFFER_DAYS = 30  # extra calendar days so 10Y lookback has enough bars

# Reuse project price cache: config.CACHE_DIR → outputs/cache/prices/
_SCRATCH_FMP_SESSION: Any | None = None


def scratch_fmp_api_key() -> str:
    """Load FMP_API_KEY from project ``.env`` (no sys.exit — show errors in Streamlit)."""
    load_dotenv(config.PROJECT_ROOT / ".env")
    return (os.getenv("FMP_API_KEY") or "").strip()


def scratch_fmp_session() -> Any:
    """One HTTP session per Streamlit run (reused across tickers)."""
    global _SCRATCH_FMP_SESSION
    if _SCRATCH_FMP_SESSION is None:
        _SCRATCH_FMP_SESSION = data_loader.create_http_session()
    return _SCRATCH_FMP_SESSION


def probe_fmp_connection() -> tuple[bool, str]:
    """Lightweight FMP quote check (SPY) to verify API key and connectivity."""
    api_key = scratch_fmp_api_key()
    if not api_key:
        return False, "API key not set in `.env`"
    try:
        session = scratch_fmp_session()
        data = data_loader._fmp_get(session, api_key, "quote", symbol="SPY")
        if isinstance(data, list) and len(data) > 0:
            return True, "Connected"
        if isinstance(data, dict) and data:
            return True, "Connected"
        return False, "Empty response from FMP"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def cache_directory_status() -> tuple[bool, str]:
    """Report whether the shared price cache folder exists and how many files it has."""
    cache_dir = config.CACHE_DIR
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        n = len(list(cache_dir.glob("*.parquet"))) + len(list(cache_dir.glob("*.csv")))
        return True, f"Ready — {n} cached price file(s) in `{cache_dir.name}/`"
    except OSError as e:
        return False, f"Unavailable: {e}"


def ticker_file_status_label() -> str:
    """Human-readable status for the Excel universe file."""
    uploaded = st.session_state.get("scratch_uploaded_file_name")
    if uploaded:
        n = st.session_state.get("scratch_uploaded_ticker_count")
        if n is not None:
            return f"Uploaded: **{uploaded}** ({n} tickers)"
        return f"Uploaded: **{uploaded}**"
    if DEFAULT_EXCEL_PATH.is_file():
        return f"Default on disk: **{DEFAULT_EXCEL_PATH.name}** (use Top 100 mode)"
    return "No file loaded — upload in **Top 100 10Y Performers**"


def render_connection_status() -> None:
    """Sidebar block: FMP, cache, and ticker file status."""
    st.sidebar.markdown("---")
    st.sidebar.subheader("Connection Status")

    if st.sidebar.button("Refresh status", key="refresh_connection_status"):
        st.session_state.pop("scratch_fmp_status", None)

    if "scratch_fmp_status" not in st.session_state:
        st.session_state["scratch_fmp_status"] = probe_fmp_connection()

    fmp_ok, fmp_msg = st.session_state["scratch_fmp_status"]
    cache_ok, cache_msg = cache_directory_status()

    if fmp_ok:
        st.sidebar.success(f"**FMP API:** {fmp_msg}")
    else:
        st.sidebar.error(f"**FMP API:** Not connected — {fmp_msg}")

    if cache_ok:
        st.sidebar.info(f"**Cache:** {cache_msg}")
    else:
        st.sidebar.warning(f"**Cache:** {cache_msg}")

    st.sidebar.markdown(f"**Ticker file:** {ticker_file_status_label()}")


# ---------------------------------------------------------------------------
# Reusable helpers
# ---------------------------------------------------------------------------
def parse_tickers(raw: str) -> list[str]:
    """Split comma/space/newline-separated ticker text into uppercase symbols."""
    if not raw or not str(raw).strip():
        return []
    parts: list[str] = []
    for chunk in str(raw).replace("\n", ",").split(","):
        for token in chunk.split():
            sym = token.strip().upper()
            if sym:
                parts.append(sym)
    return clean_ticker_list(parts)


def clean_ticker_list(tickers: list[str] | tuple[str, ...]) -> list[str]:
    """Remove blanks, strip, uppercase, dedupe (preserve order)."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in tickers:
        sym = str(raw).strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def _normalize_column_name(name: object) -> str:
    return str(name).strip().lower().replace("_", " ")


def load_tickers_from_excel(uploaded_file: BinaryIO | Path | str | None) -> list[str]:
    """
    Read tickers from an Excel workbook. Auto-detects a symbol column when possible.

    ``uploaded_file`` may be a Streamlit upload (bytes buffer), path, or None.
    """
    if uploaded_file is None:
        return []
    if isinstance(uploaded_file, (str, Path)):
        df = pd.read_excel(Path(uploaded_file))
    else:
        data = uploaded_file.read() if hasattr(uploaded_file, "read") else uploaded_file
        df = pd.read_excel(io.BytesIO(data))

    if df.empty:
        return []

    col_map = {_normalize_column_name(c): c for c in df.columns}
    chosen: str | None = None
    for candidate in TICKER_COLUMN_CANDIDATES:
        key = _normalize_column_name(candidate)
        if key in col_map:
            chosen = col_map[key]
            break

    if chosen is None:
        for ncol, orig in col_map.items():
            if "symbol" in ncol or "ticker" in ncol or ncol == "security":
                chosen = orig
                break

    if chosen is None:
        raise ValueError(
            f"Could not find a ticker column. Tried {TICKER_COLUMN_CANDIDATES}. "
            f"Columns in file: {list(df.columns)}"
        )

    series = df[chosen].astype(str)
    return clean_ticker_list(series.tolist())


def fetch_price_history(
    session: Any,
    api_key: str,
    ticker: str,
    start_date: date,
    end_date: date,
    *,
    force_refresh: bool = False,
) -> pd.Series:
    """
    Dividend-adjusted closes from FMP via ``data_loader.get_price_history``.

    Uses the shared on-disk cache under ``config.CACHE_DIR`` so reruns are fast.
    Returns a Series indexed by date.
    """
    hist = data_loader.get_price_history(
        session,
        api_key,
        ticker,
        start_date,
        end_date,
        force_refresh=force_refresh,
    )
    if hist is None or hist.empty or "date" not in hist.columns:
        return pd.Series(dtype=float)

    col = "adjClose" if "adjClose" in hist.columns else None
    if col is None:
        for c in ("adj_close", "close", "Close"):
            if c in hist.columns:
                col = c
                break
    if col is None:
        return pd.Series(dtype=float)

    df = hist.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    prices = pd.to_numeric(df[col], errors="coerce")
    series = pd.Series(prices.values, index=df["date"]).dropna()
    if series.empty:
        return pd.Series(dtype=float)
    idx = pd.DatetimeIndex(series.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    series.index = idx.normalize()
    return series.sort_index()


def has_history_years(price_series: pd.Series, years: int) -> bool:
    """True if the first bar is at least ``years`` calendar years before the last bar."""
    prices = pd.to_numeric(price_series, errors="coerce").dropna()
    if len(prices) < 2:
        return False
    start = prices.index[0]
    end = prices.index[-1]
    return start <= end - pd.DateOffset(years=int(years))


def calculate_return(
    price_series: pd.Series,
    *,
    months: int | None = None,
    years: int | None = None,
) -> float:
    """
    Total return over ``months`` or ``years`` lookback from the last price.

    Returns NaN if history is too short or prices are invalid.
    """
    if price_series is None or price_series.empty:
        return float("nan")

    prices = pd.to_numeric(price_series, errors="coerce").dropna()
    if len(prices) < 2:
        return float("nan")

    end_dt = prices.index[-1]
    if months is not None:
        start_dt = end_dt - pd.DateOffset(months=int(months))
    elif years is not None:
        start_dt = end_dt - pd.DateOffset(years=int(years))
    else:
        return float("nan")

    window = prices[prices.index >= start_dt]
    if window.empty:
        return float("nan")

    start_px = float(window.iloc[0])
    end_px = float(prices.iloc[-1])
    if start_px <= 0 or not np.isfinite(start_px) or not np.isfinite(end_px):
        return float("nan")
    return end_px / start_px - 1.0


def calculate_max_drawdown(price_series: pd.Series) -> float:
    """Peak-to-trough drawdown on the full price series (negative number)."""
    prices = pd.to_numeric(price_series, errors="coerce").dropna()
    if len(prices) < 2:
        return float("nan")
    wealth = prices / float(prices.iloc[0])
    peak = wealth.cummax()
    dd = wealth / peak - 1.0
    return float(dd.min())


def calculate_annualized_volatility(price_series: pd.Series, trading_days: int = 252) -> float:
    """Annualized std of daily simple returns."""
    prices = pd.to_numeric(price_series, errors="coerce").dropna()
    if len(prices) < 3:
        return float("nan")
    rets = prices.pct_change().dropna()
    if rets.empty:
        return float("nan")
    return float(rets.std(ddof=1) * np.sqrt(trading_days))


def _empty_metrics_row(ticker: str) -> dict[str, Any]:
    return {col: float("nan") for col in METRIC_COLUMNS if col != "ticker"} | {"ticker": ticker}


def analyze_single_ticker(
    session: Any,
    api_key: str,
    ticker: str,
    *,
    as_of: date,
    history_start: date,
) -> dict[str, Any]:
    """Fetch prices from FMP and compute all metrics for one symbol."""
    row = _empty_metrics_row(ticker)
    try:
        prices = fetch_price_history(session, api_key, ticker, history_start, as_of)
    except Exception:
        return row

    if prices.empty or len(prices) < 5:
        return row

    row["return_3m"] = calculate_return(prices, months=3)
    row["return_6m"] = calculate_return(prices, months=6)
    row["return_12m"] = calculate_return(prices, months=12)
    row["return_3y"] = calculate_return(prices, years=3) if has_history_years(prices, 3) else float("nan")
    row["return_5y"] = calculate_return(prices, years=5) if has_history_years(prices, 5) else float("nan")
    row["return_10y"] = (
        calculate_return(prices, years=10) if has_history_years(prices, 10) else float("nan")
    )
    row["max_drawdown"] = calculate_max_drawdown(prices)
    row["annualized_volatility"] = calculate_annualized_volatility(prices)
    return row


def analyze_tickers(
    tickers: list[str],
    session: Any,
    api_key: str,
    *,
    as_of: date | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[pd.DataFrame, list[str], dict[str, Any]]:
    """
    Analyze every ticker; failures are recorded but do not stop the run.

    Returns ``(results_df, failed_tickers, summary)``.
    """
    as_of = as_of or date.today()
    history_start = as_of - timedelta(days=int(HISTORY_YEARS * 365.25) + HISTORY_BUFFER_DAYS)

    rows: list[dict[str, Any]] = []
    failed: list[str] = []
    n = len(tickers)

    for i, ticker in enumerate(tickers):
        if progress_callback is not None:
            progress_callback(i, n, ticker)
        try:
            row = analyze_single_ticker(
                session, api_key, ticker, as_of=as_of, history_start=history_start
            )
            prices_ok = any(
                np.isfinite(row.get(k, float("nan")))
                for k in ("return_3m", "return_12m", "return_10y", "max_drawdown")
            )
            if not prices_ok:
                failed.append(ticker)
            rows.append(row)
        except Exception:
            failed.append(ticker)
            rows.append(_empty_metrics_row(ticker))

    if progress_callback is not None:
        progress_callback(n, n, "done")

    results = pd.DataFrame(rows)
    if results.empty:
        summary = {
            "total_tickers": len(tickers),
            "analyzed_ok": 0,
            "failed_count": len(failed),
            "as_of": as_of,
        }
        return results, failed, summary

    # Rank by 10Y return; NaN sorts last.
    results = results.sort_values("return_10y", ascending=False, na_position="last")
    analyzed_ok = int(len(tickers) - len(failed))
    summary = {
        "total_tickers": len(tickers),
        "analyzed_ok": analyzed_ok,
        "failed_count": len(failed),
        "as_of": as_of,
    }
    return results, failed, summary


def format_results_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Human-readable column names and percentage formatting for the UI."""
    if df.empty:
        return df.copy()

    out = df.copy()
    rename = {
        "ticker": "Ticker",
        "return_3m": "3M Return",
        "return_6m": "6M Return",
        "return_12m": "12M Return",
        "return_3y": "3Y Return",
        "return_5y": "5Y Return",
        "return_10y": "10Y Return",
        "max_drawdown": "Max Drawdown",
        "annualized_volatility": "Ann. Volatility",
    }
    out = out.rename(columns=rename)

    pct_cols = [
        "3M Return",
        "6M Return",
        "12M Return",
        "3Y Return",
        "5Y Return",
        "10Y Return",
        "Max Drawdown",
        "Ann. Volatility",
    ]
    for col in pct_cols:
        if col in out.columns:
            out[col] = out[col].apply(
                lambda x: f"{float(x) * 100:.2f}%" if pd.notna(x) and np.isfinite(x) else "N/A"
            )
    return out


def top_100_by_10y_return(results: pd.DataFrame) -> pd.DataFrame:
    """Keep top 100 rows by 10Y return (numeric sort already applied in analyze_tickers)."""
    if results.empty:
        return results.copy()
    return results.head(100).reset_index(drop=True)


def render_top_100_10y_performers() -> None:
    """Excel upload → FMP adjusted prices → top 100 by 10Y total return."""
    st.subheader("Top 100 10Y Performers")
    st.caption(
        "Upload the brokerage mutual-fund availability list (or use the default path if present). "
        "Prices come from **FMP** (`data_loader.get_price_history`, cached under "
        f"`{config.CACHE_DIR}`). Ranked by **10-year total return**; tickers with "
        "less than 10 years of history show 10Y as N/A."
    )

    api_key = scratch_fmp_api_key()
    if not api_key:
        st.error("Set **FMP_API_KEY** in the project `.env` file to run this analysis.")
        return

    uploaded = st.file_uploader(
        "Excel ticker list",
        type=["xlsx", "xls"],
        help="Expected columns include Symbol, Ticker, Fund Symbol, or Security.",
    )
    if uploaded is not None:
        st.session_state["scratch_uploaded_file_name"] = uploaded.name

    use_default = False
    if uploaded is None and DEFAULT_EXCEL_PATH.is_file():
        use_default = st.checkbox(
            f"Use default file on disk ({DEFAULT_EXCEL_PATH.name})",
            value=True,
        )

    run = st.button("Run Analysis", type="primary", key="run_top_100_10y")

    if not run:
        st.info("Upload a file (or enable the default path), then click **Run Analysis**.")
        return

    try:
        if uploaded is not None:
            tickers = load_tickers_from_excel(uploaded)
            source_label = uploaded.name
            st.session_state["scratch_uploaded_file_name"] = uploaded.name
            st.session_state["scratch_uploaded_ticker_count"] = len(tickers)
        elif use_default and DEFAULT_EXCEL_PATH.is_file():
            tickers = load_tickers_from_excel(DEFAULT_EXCEL_PATH)
            source_label = str(DEFAULT_EXCEL_PATH)
        else:
            st.warning("Upload an Excel file or enable the default path.")
            return
    except Exception as e:
        st.error(f"Could not read Excel file: {e}")
        return

    if not tickers:
        st.warning("No tickers found in the workbook.")
        return

    st.caption(f"Source: `{source_label}` — **{len(tickers)}** symbols after cleaning.")

    progress_bar = st.progress(0.0, text="Starting…")
    status = st.empty()

    def _progress(done: int, total: int, symbol: str) -> None:
        frac = (done / total) if total else 1.0
        progress_bar.progress(frac, text=f"Fetching {symbol} ({done}/{total})…")
        status.caption(f"Processing **{symbol}** ({done} of {total})")

    session = scratch_fmp_session()
    with st.spinner("Fetching FMP price history and computing metrics…"):
        results, failed, summary = analyze_tickers(
            tickers, session, api_key, progress_callback=_progress
        )

    progress_bar.progress(1.0, text="Complete")
    status.empty()

    top100 = top_100_by_10y_return(results)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total tickers", summary["total_tickers"])
    c2.metric("Analyzed OK", summary["analyzed_ok"])
    c3.metric("Failed / no data", summary["failed_count"])
    c4.metric("As of", str(summary["as_of"]))

    if failed:
        with st.expander(f"Failed tickers ({len(failed)})"):
            st.write(", ".join(failed[:500]))
            if len(failed) > 500:
                st.caption(f"… and {len(failed) - 500} more")

    st.markdown("**Top 100 by 10Y return**")
    display_df = format_results_for_display(top100)
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    # CSV download uses raw numeric values (not formatted strings)
    csv_df = top100.copy()
    st.download_button(
        label="Download CSV",
        data=csv_df.to_csv(index=False).encode("utf-8"),
        file_name=f"top_100_10y_performers_{summary['as_of']}.csv",
        mime="text/csv",
    )


def render_blank(
    tickers: list[str],
    start_date: date,
    end_date: date,
) -> None:
    """Default empty shell — use as a template for new analysis types."""
    st.subheader("Blank")
    st.caption("No analysis selected. Paste code here or pick another analysis type in the sidebar.")
    st.info(f"Tickers: {tickers or '(none)'} | Range: {start_date} → {end_date}")


def render_price_test(
    tickers: list[str],
    start_date: date,
    end_date: date,
) -> None:
    """Price pulls, returns, simple charts."""
    st.subheader("Price Test")
    st.info(f"Tickers: {tickers or '(none)'} | Range: {start_date} → {end_date}")
    st.markdown("**Charts** — add `st.line_chart` or matplotlib here.")
    st.markdown("**Tables** — add latest closes / returns DataFrame here.")


def render_relative_strength_test(
    tickers: list[str],
    start_date: date,
    end_date: date,
) -> None:
    """RS ratios vs a benchmark (e.g. first ticker or SPY)."""
    st.subheader("Relative Strength Test")
    st.info(f"Tickers: {tickers or '(none)'} | Range: {start_date} → {end_date}")
    st.markdown("**Charts** — RS ratio time series.")
    st.markdown("**Tables** — 1W / 1M / 3M RS % heatmap or metrics.")


def render_factor_scrap(
    tickers: list[str],
    start_date: date,
    end_date: date,
) -> None:
    """Ad-hoc factor / z-score / ranking experiments."""
    st.subheader("Factor Scrap")
    st.info(f"Tickers: {tickers or '(none)'} | Range: {start_date} → {end_date}")
    st.markdown("**Tables** — factor panel, ranks, deciles.")
    st.markdown("**Scratch Output** — print diagnostics, correlations, IC stubs.")


def render_hedge_simulator(
    tickers: list[str],
    start_date: date,
    end_date: date,
) -> None:
    """Simple hedge / beta / notional experiments."""
    st.subheader("Hedge Simulator")
    st.info(f"Tickers: {tickers or '(none)'} | Range: {start_date} → {end_date}")
    st.markdown("**Inputs** — notionals, hedge ratios, target exposures.")
    st.markdown("**Tables** — simulated weights and hedge legs.")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------
def _main_body() -> None:
    render_connection_status()

    with st.sidebar:
        st.header("Scratch controls")
        ticker_text = st.text_input(
            "Tickers",
            value="SPY,QQQ,XLK",
            help="Comma-, space-, or newline-separated symbols.",
        )
        today = date.today()
        default_start = today - timedelta(days=365)
        date_range = st.date_input(
            "Date range",
            value=(default_start, today),
            max_value=today,
        )
        analysis_type = st.selectbox("Analysis type", options=ANALYSIS_OPTIONS, index=0)

    tickers = parse_tickers(ticker_text)
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range[0], date_range[1]
    elif isinstance(date_range, date):
        start_date = end_date = date_range
    else:
        start_date = end_date = today

    st.title("Scratch Dashboard")
    st.warning(
        "Experiments only — not production. Results are not validated; do not use for trading decisions.",
        icon="⚠️",
    )
    st.caption(
        "Isolated from `dashboard.py` and ranking/rotation engines. "
        "Edit `scratch_dashboard.py` and the `render_*` functions to iterate quickly."
    )

    st.markdown("##### Notes")
    with st.container(border=True):
        st.markdown("_Add markdown notes, hypotheses, or links to notebooks here._")

    st.markdown("##### Inputs")
    with st.container(border=True):
        if analysis_type != ANALYSIS_TOP_100_10Y:
            c1, c2, c3 = st.columns(3)
            c1.metric("Tickers", ", ".join(tickers) if tickers else "—")
            c2.metric("Start", str(start_date))
            c3.metric("End", str(end_date))
        st.caption(f"Analysis: **{analysis_type}**")

    st.markdown("##### Charts")
    with st.container(border=True):
        if analysis_type != ANALYSIS_TOP_100_10Y:
            st.empty()

    st.markdown("##### Tables")
    with st.container(border=True):
        if analysis_type != ANALYSIS_TOP_100_10Y:
            st.empty()

    st.markdown("##### Scratch Output")
    with st.container(border=True):
        if analysis_type == ANALYSIS_BLANK:
            render_blank(tickers, start_date, end_date)
        elif analysis_type == ANALYSIS_PRICE_TEST:
            render_price_test(tickers, start_date, end_date)
        elif analysis_type == ANALYSIS_RELATIVE_STRENGTH:
            render_relative_strength_test(tickers, start_date, end_date)
        elif analysis_type == ANALYSIS_FACTOR_SCRAP:
            render_factor_scrap(tickers, start_date, end_date)
        elif analysis_type == ANALYSIS_HEDGE_SIMULATOR:
            render_hedge_simulator(tickers, start_date, end_date)
        elif analysis_type == ANALYSIS_TOP_100_10Y:
            render_top_100_10y_performers()
        else:
            render_blank(tickers, start_date, end_date)


def main() -> None:
    st.set_page_config(page_title="Scratch Dashboard", layout="wide")
    try:
        _main_body()
    except Exception as exc:
        traceback.print_exc()
        st.error(
            "Scratch Dashboard encountered an error. See the details below; "
            "the full traceback was also printed to the terminal.",
            icon="🛑",
        )
        st.exception(exc)


if __name__ == "__main__":
    main()
