"""
All FMP `stock-list` tickers merged with `profile-bulk`, excluding ETFs and funds,
sorted by sector and industry, with counts per sector and per sector+industry.

Run:
  python sector_industry_report.py
"""

from __future__ import annotations

import sys

import pandas as pd

import config
import data_loader
import tech_universe

OUTPUT_PATH = config.OUTPUT_DIR / "sector_industry_stocks.xlsx"
SORT_SECTOR_MISSING = "(no profile)"
SORT_INDUSTRY_MISSING = "(no industry)"


def _stocks_only_mask(df: pd.DataFrame) -> pd.Series:
    """Exclude ETFs and mutual funds; missing flags treated as not ETF/fund."""
    ok = pd.Series(True, index=df.index)
    if "isEtf" in df.columns:
        ok &= ~tech_universe._truthy_mask(df["isEtf"])
    if "isFund" in df.columns:
        ok &= ~tech_universe._truthy_mask(df["isFund"])
    return ok


def build_sorted_stocks(merged: pd.DataFrame) -> pd.DataFrame:
    df = merged.loc[_stocks_only_mask(merged)].copy()
    df["_sector_sort"] = df["sector"].fillna(SORT_SECTOR_MISSING).astype(str).str.strip()
    df["_industry_sort"] = df["industry"].fillna(SORT_INDUSTRY_MISSING).astype(str).str.strip()
    df = df.sort_values(["_sector_sort", "_industry_sort", "symbol"], ascending=[True, True, True])
    df = df.drop(columns=["_sector_sort", "_industry_sort"], errors="ignore")
    return df.reset_index(drop=True)


def count_by_sector(stocks: pd.DataFrame) -> pd.DataFrame:
    s = stocks.copy()
    s["sector"] = s["sector"].fillna(SORT_SECTOR_MISSING).astype(str).str.strip()
    out = (
        s.groupby("sector", dropna=False)
        .size()
        .reset_index(name="stock_count")
        .sort_values(["stock_count", "sector"], ascending=[False, True])
        .reset_index(drop=True)
    )
    return out


def count_by_sector_industry(stocks: pd.DataFrame) -> pd.DataFrame:
    s = stocks.copy()
    s["sector"] = s["sector"].fillna(SORT_SECTOR_MISSING).astype(str).str.strip()
    s["industry"] = s["industry"].fillna(SORT_INDUSTRY_MISSING).astype(str).str.strip()
    out = (
        s.groupby(["sector", "industry"], dropna=False)
        .size()
        .reset_index(name="stock_count")
        .sort_values(["sector", "industry", "stock_count"], ascending=[True, True, False])
        .reset_index(drop=True)
    )
    return out


def main() -> int:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    api_key = data_loader.load_api_key()
    session = data_loader.create_http_session()

    print("[sector_industry] stock-list ...", flush=True)
    stock_list = tech_universe.fetch_stock_list(session, api_key)
    print(f"[sector_industry] tickers from stock-list: {len(stock_list):,}", flush=True)

    print("[sector_industry] profile-bulk ...", flush=True)
    profiles = tech_universe.fetch_profile_bulk_all(session, api_key)
    if profiles.empty:
        print("[sector_industry] ERROR: profile-bulk returned no data.", file=sys.stderr)
        return 1

    merged = tech_universe.merge_stock_list_with_profiles(stock_list, profiles)
    merged = tech_universe._normalize_exchange_column(merged)
    stocks_sorted = build_sorted_stocks(merged)

    stocks_export = stocks_sorted.copy()
    stocks_export["sector"] = stocks_export["sector"].fillna(SORT_SECTOR_MISSING).astype(str).str.strip()
    stocks_export["industry"] = stocks_export["industry"].fillna(SORT_INDUSTRY_MISSING).astype(str).str.strip()

    preferred_cols = [
        "symbol",
        "companyName",
        "sector",
        "industry",
        "exchange",
        "exchangeShortName",
        "country",
        "marketCap",
        "isEtf",
        "isFund",
    ]
    for c in preferred_cols:
        if c not in stocks_export.columns:
            stocks_export[c] = pd.NA
    display_cols = [c for c in preferred_cols if c in stocks_export.columns]
    extra = [c for c in stocks_export.columns if c not in display_cols]
    stocks_out = stocks_export[display_cols + extra]

    by_sector = count_by_sector(stocks_sorted)
    by_sector_industry = count_by_sector_industry(stocks_sorted)

    print(f"\nStocks (ex ETF/fund): {len(stocks_sorted):,}", flush=True)
    print(f"Sectors (incl. missing label): {len(by_sector)}", flush=True)
    print(f"Sector–industry rows: {len(by_sector_industry)}", flush=True)

    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        tech_universe.sanitize_for_excel(stocks_out).to_excel(
            writer, sheet_name="Stocks sorted", index=False
        )
        tech_universe.sanitize_for_excel(by_sector).to_excel(
            writer, sheet_name="Count by sector", index=False
        )
        tech_universe.sanitize_for_excel(by_sector_industry).to_excel(
            writer, sheet_name="Count by sector industry", index=False
        )

    print(f"\nWrote: {OUTPUT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
