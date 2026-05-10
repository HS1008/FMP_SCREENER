"""
Sector analysis entrypoint (EW vs CW vs ETF). Writes `outputs/sector_model.xlsx`.

The default `main.py` runs the Z-score ranking pipeline instead.

Run:
  python run_sector_model.py
  python run_sector_model.py --top-n 200 --windows 1W,YTD --force-refresh
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

import config
import data_loader
import sector_model


def _panels_to_daily_long(panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Stack per-sector daily return panels for Excel + dashboard charts."""
    parts: list[pd.DataFrame] = []
    for sector, panel in panels.items():
        if panel is None or panel.empty:
            continue
        p = panel.copy()
        p["sector"] = sector
        parts.append(p[["date", "sector", "ew", "cw", "etf"]])
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    return out.sort_values(["sector", "date"]).reset_index(drop=True)


def _parse_windows(s: str) -> tuple[str, ...]:
    parts = tuple(x.strip() for x in s.split(",") if x.strip())
    allowed = {"1W", "1M", "YTD"}
    bad = [p for p in parts if p not in allowed]
    if bad:
        raise SystemExit(f"Unsupported window(s): {bad}. Allowed: {sorted(allowed)}")
    if not parts:
        parts = tuple(config.DEFAULT_WINDOWS)
    return parts


def main() -> int:
    parser = argparse.ArgumentParser(description="FMP sector model (EW vs CW vs ETF).")
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        help=f"Override number of stocks to pull (default: {config.STOCK_UNIVERSE_TOP_N}).",
    )
    parser.add_argument(
        "--windows",
        type=str,
        default=",".join(config.DEFAULT_WINDOWS),
        help='Comma-separated performance windows. Example: "1W,1M,YTD"',
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore local CSV price cache and re-download.",
    )
    args = parser.parse_args()

    windows = _parse_windows(args.windows)

    t_all = time.perf_counter()
    print("=== FMP sector analysis ===", flush=True)
    print(f"windows={windows} top_n={args.top_n or config.STOCK_UNIVERSE_TOP_N}", flush=True)

    try:
        import tqdm  # noqa: F401
    except ImportError:
        print("Tip: `pip install tqdm` for progress bars.", flush=True)

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    api_key = data_loader.load_api_key()
    print(f"[time] API key loaded in {time.perf_counter() - t0:.2f}s", flush=True)

    session = data_loader.create_http_session()
    date_from, date_to = data_loader.default_price_window()

    t0 = time.perf_counter()
    universe = data_loader.get_stock_universe(session, api_key, top_n=args.top_n)
    print(f"[time] universe built in {time.perf_counter() - t0:.2f}s", flush=True)

    t0 = time.perf_counter()
    data_loader.prefetch_stock_prices(
        session,
        api_key,
        universe["symbol"].astype(str).tolist(),
        date_from,
        date_to,
        force_refresh=args.force_refresh,
    )
    print(f"[time] stock price prefetch finished in {time.perf_counter() - t0:.2f}s", flush=True)

    t0 = time.perf_counter()
    etf_prices = data_loader.get_etf_prices(
        session,
        api_key,
        date_from,
        date_to,
        force_refresh=args.force_refresh,
    )
    print(f"[time] ETF prices finished in {time.perf_counter() - t0:.2f}s", flush=True)

    t0 = time.perf_counter()
    summary, panels = sector_model.build_sector_outputs(
        session,
        api_key,
        universe,
        etf_prices,
        windows=windows,
        date_from=date_from,
        date_to=date_to,
        force_refresh=args.force_refresh,
    )
    print(f"[time] sector model finished in {time.perf_counter() - t0:.2f}s", flush=True)

    if summary.empty:
        print("No sector summary produced (check warnings above).", file=sys.stderr)
        return 2

    pd.set_option("display.max_columns", 200)
    pd.set_option("display.width", 200)
    rounded = summary.round(2)
    print("\n=== Sector summary (sorted by ETF lead window) ===\n", flush=True)
    print(rounded.to_string(index=False), flush=True)

    ranks = sector_model.rank_sectors(summary, windows)
    etf_col = sector_model.primary_etf_column(windows)
    breadth_col = sector_model.primary_breadth_column(windows)

    print("\n=== Top 3 sectors (by ETF window return) ===\n", flush=True)
    print(ranks["top_etf"].round(2)[["sector", etf_col]].to_string(index=False), flush=True)

    print("\n=== Bottom 3 sectors (by ETF window return) ===\n", flush=True)
    print(ranks["bottom_etf"].round(2)[["sector", etf_col]].to_string(index=False), flush=True)

    print("\n=== Strongest breadth (EW minus CW) - top 5 ===\n", flush=True)
    print(ranks["breadth"].round(2)[["sector", breadth_col]].to_string(index=False), flush=True)

    daily_long = _panels_to_daily_long(panels)

    out_path: Path = config.EXCEL_OUTPUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        rounded.to_excel(writer, sheet_name="summary", index=False)
        universe.head(2000).to_excel(writer, sheet_name="universe_top", index=False)
        if not daily_long.empty:
            daily_long.to_excel(writer, sheet_name="sector_model_daily", index=False)
            print(f"[excel] sector_model_daily rows={len(daily_long)}", flush=True)

    print(f"\nWrote: {out_path}", flush=True)
    print(f"[time] TOTAL {time.perf_counter() - t_all:.2f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
