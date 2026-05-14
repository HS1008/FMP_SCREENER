"""
Precompute heavy dashboard artifacts (rotation prices, sector bundles, SPY vs sectors,
AI theme rotation, semiconductor rotation, per-sector dispersion) and write Parquet + JSON
under ``outputs/precomputed/``.

Layout:
  - ``rotation/rotation_prices_long.parquet`` — union prices for industry + AI rotation
  - ``rotation/<Sector>/`` — per-sector industry rotation bundles
  - ``ai/``, ``spy/`` — AI theme and SPY vs sector ETF bundles
  - ``dispersion/<Sector>/`` — dispersion dashboard bundles
  - ``semi_rotation/`` — semiconductor vs XLK bundle

Run:
  python nightly_refresh.py

Requires a Parquet engine for pandas (typically ``pip install pyarrow``).
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

import ai_rotation_engine
import comm_rotation_engine
import config
import consumer_cyclical_rotation_engine
import consumer_defensive_rotation_engine
import data_loader
import dispersion_engine
import energy_rotation_engine
import financial_services_rotation_engine
import healthcare_rotation_engine
import industrials_rotation_engine
import materials_rotation_engine
import real_estate_rotation_engine
import rotation_price_batch
import semi_rotation_engine
import spy_sector_rotation_engine
import tech_rotation_engine
import utilities_rotation_engine

PRECOMPUTED_ROOT: Path = config.OUTPUT_DIR / "precomputed"

_LOG = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, (np.ndarray,)):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def save_json(obj: Any, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=_json_default, allow_nan=True)


def save_dataframe(df: pd.DataFrame, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=True)


def _is_dataframe_dict(d: Mapping[str, Any]) -> bool:
    if not d:
        return False
    return all(isinstance(v, pd.DataFrame) for v in d.values())


def save_bundle(bundle: Mapping[str, Any], output_dir: Path) -> None:
    """
    Persist a dashboard-style bundle dict: ``ok`` / ``error`` / ``as_of`` in ``bundle_meta.json``,
    every ``pd.DataFrame`` as ``<key>.parquet``, dict-of-DataFrame values under ``<key>/<sub>.parquet``,
    other dicts as ``<key>.json``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "ok": bundle.get("ok"),
        "error": bundle.get("error"),
        "as_of": str(bundle.get("as_of")) if bundle.get("as_of") is not None else None,
    }
    save_json(meta, output_dir / "bundle_meta.json")

    meta_keys = frozenset({"ok", "error", "as_of"})
    for key, val in bundle.items():
        if key in meta_keys:
            continue
        if isinstance(val, pd.DataFrame):
            save_dataframe(val, output_dir / f"{key}.parquet")
        elif isinstance(val, dict):
            if _is_dataframe_dict(val):
                subdir = output_dir / key
                subdir.mkdir(parents=True, exist_ok=True)
                for sub_k, sub_df in val.items():
                    safe = str(sub_k).replace("/", "-").replace(" ", "_")
                    save_dataframe(sub_df, subdir / f"{safe}.parquet")
            else:
                save_json(val, output_dir / f"{key}.json")
        else:
            save_json(val, output_dir / f"{key}.json")


def _sector_dir_name(sector: str) -> str:
    return str(sector).strip().replace("/", "-").replace(" ", "_")


def _cli_price_workers() -> int:
    return max(1, int(getattr(config, "PRICE_FETCH_MAX_WORKERS", 4)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(config.PROJECT_ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        print("Error: FMP_API_KEY is missing or empty in environment / .env", flush=True)
        raise SystemExit(1)

    PRECOMPUTED_ROOT.mkdir(parents=True, exist_ok=True)
    rotation_dir = PRECOMPUTED_ROOT / "rotation"
    dispersion_dir = PRECOMPUTED_ROOT / "dispersion"

    session = data_loader.create_http_session()
    print("[nightly] HTTP session created.", flush=True)

    bulk: pd.DataFrame | None = None
    print("[nightly] Fetching union rotation prices (all sector + AI symbols)…", flush=True)
    try:
        bulk = rotation_price_batch.fetch_all_dashboard_rotation_prices_long(
            session, api_key, force_refresh=False
        )
        save_dataframe(bulk, rotation_dir / "rotation_prices_long.parquet")
        print(f"[nightly] Saved rotation_prices_long.parquet ({len(bulk)} rows).", flush=True)
    except Exception as e:
        _LOG.error("Rotation bulk price fetch failed: %s", e)
        traceback.print_exc()
        bulk = None

    industry_rotations: tuple[tuple[str, Callable[..., dict[str, Any]]], ...] = (
        ("Technology", tech_rotation_engine.build_tech_rotation_bundle),
        ("Basic Materials", materials_rotation_engine.build_materials_rotation_bundle),
        ("Communication Services", comm_rotation_engine.build_comm_rotation_bundle),
        ("Consumer Cyclical", consumer_cyclical_rotation_engine.build_consumer_cyclical_rotation_bundle),
        ("Consumer Defensive", consumer_defensive_rotation_engine.build_consumer_defensive_rotation_bundle),
        ("Energy", energy_rotation_engine.build_energy_rotation_bundle),
        ("Financial Services", financial_services_rotation_engine.build_financial_services_rotation_bundle),
        ("Healthcare", healthcare_rotation_engine.build_healthcare_rotation_bundle),
        ("Industrials", industrials_rotation_engine.build_industrials_rotation_bundle),
        ("Real Estate", real_estate_rotation_engine.build_real_estate_rotation_bundle),
        ("Utilities", utilities_rotation_engine.build_utilities_rotation_bundle),
    )

    for sector_name, builder in industry_rotations:
        out = rotation_dir / _sector_dir_name(sector_name)
        print(f"[nightly] Industry rotation bundle: {sector_name} → {out}", flush=True)
        try:
            bundle = builder(session, api_key, force_refresh=False, prefetched_prices_long=bulk)
            save_bundle(bundle, out)
            print(f"[nightly] Done {sector_name} (ok={bundle.get('ok')}).", flush=True)
        except Exception as e:
            _LOG.error("Industry rotation failed for %s: %s", sector_name, e)
            traceback.print_exc()

    print("[nightly] AI rotation bundle…", flush=True)
    try:
        ai_bundle = ai_rotation_engine.build_ai_rotation_bundle(
            session, api_key, force_refresh=False, prefetched_prices_long=bulk
        )
        save_bundle(ai_bundle, PRECOMPUTED_ROOT / "ai")
        print(f"[nightly] AI rotation done (ok={ai_bundle.get('ok')}).", flush=True)
    except Exception as e:
        _LOG.error("AI rotation failed: %s", e)
        traceback.print_exc()

    print("[nightly] SPY sector rotation bundle…", flush=True)
    try:
        spy_bundle = spy_sector_rotation_engine.build_spy_sector_rotation_bundle(
            session, api_key, force_refresh=False
        )
        save_bundle(spy_bundle, PRECOMPUTED_ROOT / "spy")
        print(f"[nightly] SPY sector rotation done (ok={spy_bundle.get('ok')}).", flush=True)
    except Exception as e:
        _LOG.error("SPY sector rotation failed: %s", e)
        traceback.print_exc()

    print("[nightly] Semiconductor rotation bundle…", flush=True)
    try:
        semi_bundle = semi_rotation_engine.build_semi_rotation_bundle(session, api_key, force_refresh=False)
        save_bundle(semi_bundle, PRECOMPUTED_ROOT / "semi_rotation")
        print(f"[nightly] Semi rotation done (ok={semi_bundle.get('ok')}).", flush=True)
    except Exception as e:
        _LOG.error("Semi rotation failed: %s", e)
        traceback.print_exc()

    fmp_sectors = tuple(sorted(config.SECTOR_ETF_MAP.keys()))
    for sector in fmp_sectors:
        out = dispersion_dir / _sector_dir_name(sector)
        print(f"[nightly] Dispersion bundle: {sector} → {out}", flush=True)
        try:
            universe = dispersion_engine.build_dispersion_universe(
                session, api_key, sector=sector, force_refresh_profiles=False
            )
            bundle = dispersion_engine.run_dispersion_dashboard_bundle(
                session,
                api_key,
                sector=sector,
                force_refresh=False,
                universe=universe,
                price_fetch_max_workers=_cli_price_workers(),
            )
            save_bundle(bundle, out)
            print(f"[nightly] Dispersion {sector} done (ok={bundle.get('ok')}).", flush=True)
        except Exception as e:
            _LOG.error("Dispersion failed for %s: %s", sector, e)
            traceback.print_exc()

    print(f"[nightly] Finished. Outputs under {PRECOMPUTED_ROOT.resolve()}", flush=True)


if __name__ == "__main__":
    main()
