"""
Multi-factor sector ranking (Quality / Value / Momentum / Risk) with within-sector Z-scores.

Run:
  python main.py
  python main.py --top-n 500 --force-refresh
"""

from __future__ import annotations

import argparse
import sys
import time

import pandas as pd

import config
import data_loader
import portfolio_engine
import ranking_engine


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Multi-factor US sector ranking (FMP, Z-scores within sector)."
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        help=f"Universe size by market cap before the panel build (default: {config.MULTIFACTOR_UNIVERSE_TOP_N}).",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore local JSON/CSV caches and re-download from FMP.",
    )
    args = parser.parse_args()

    try:
        import tqdm  # noqa: F401
    except ImportError:
        print("Tip: `pip install tqdm` for progress bars.", flush=True)

    t_all = time.perf_counter()
    print("=== FMP multi-factor sector ranking ===", flush=True)

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    config.FUNDAMENTALS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    api_key = data_loader.load_api_key()
    print(f"[time] API key loaded in {time.perf_counter() - t0:.2f}s", flush=True)

    session = data_loader.create_http_session()

    t0 = time.perf_counter()
    try:
        scored = ranking_engine.run_full_scored_universe(
            session,
            api_key,
            top_n=args.top_n,
            force_refresh=args.force_refresh,
        )
    except Exception as e:
        print(f"Fatal error during ranking: {e}", file=sys.stderr)
        return 1
    print(f"[time] ranking pipeline finished in {time.perf_counter() - t0:.2f}s", flush=True)

    if scored.empty:
        print("No results (empty dataframe). Check filters and API responses.", file=sys.stderr)
        return 2

    df = ranking_engine.top10_by_sector_from_scored(scored)
    ranking_engine.print_sector_tables(df)

    out_path = config.FINAL_SECTOR_MODEL_OUTPUT
    ranking_engine.save_final_sector_model(df, out_path)
    print(f"\nWrote: {out_path}", flush=True)

    p0 = time.perf_counter()
    portfolio_df, sector_scores = portfolio_engine.build_portfolio(scored)
    portfolio_path = config.PORTFOLIO_OUTPUT
    with pd.ExcelWriter(portfolio_path, engine="openpyxl") as writer:
        portfolio_df.round(6).to_excel(writer, sheet_name="Portfolio", index=False)
        sector_scores.round(6).to_excel(writer, sheet_name="Sector Scores", index=False)
        scored.round(6).to_excel(writer, sheet_name="Full Scored Universe", index=False)
    print(f"Wrote: {portfolio_path}", flush=True)
    print(f"[time] portfolio engine finished in {time.perf_counter() - p0:.2f}s", flush=True)

    if not portfolio_df.empty:
        print(f"\n[portfolio] Holdings: {len(portfolio_df)}", flush=True)
        print(
            portfolio_df.sort_values("final_weight_pct", ascending=False)
            .head(10)[["symbol", "sector", "final_weight_pct", "final_score"]]
            .round(3)
            .to_string(index=False),
            flush=True,
        )
        sec_w = (
            portfolio_df.groupby("sector", as_index=False)["final_weight"]
            .sum()
            .assign(sector_weight_pct=lambda x: x["final_weight"] * 100.0)
            .sort_values("sector_weight_pct", ascending=False)
        )
        print("\n[portfolio] Sector weights (%)", flush=True)
        print(sec_w[["sector", "sector_weight_pct"]].round(2).to_string(index=False), flush=True)
        if not sector_scores.empty:
            ow = sector_scores.loc[sector_scores["sector_tilt"] == "overweight", "sector"].tolist()
            uw = sector_scores.loc[sector_scores["sector_tilt"] == "underweight", "sector"].tolist()
            print(f"[portfolio] Overweight sectors: {', '.join(ow) if ow else 'None'}", flush=True)
            print(f"[portfolio] Underweight sectors: {', '.join(uw) if uw else 'None'}", flush=True)
    else:
        print("[portfolio] No holdings generated.", flush=True)

    print(f"[time] TOTAL {time.perf_counter() - t_all:.2f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
