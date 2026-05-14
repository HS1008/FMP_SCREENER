"""
Load dashboard bundles written by ``nightly_refresh.py`` (Parquet + JSON) from ``outputs/precomputed/``.

Preferred layout (see ``nightly_refresh``):
  - ``rotation/<Sector_slug>/`` — industry rotation bundles
  - ``rotation/rotation_prices_long.parquet`` — union rotation prices (optional speed-up for live fallback)
  - ``ai/`` — AI theme rotation bundle
  - ``spy/`` — SPY vs sector ETF rotation bundle
  - ``dispersion/<Sector_slug>/`` — dispersion dashboard bundles

Legacy fallbacks (older nightly output): ``sector_rotation/``, ``ai_rotation/``, ``spy_sector_rotation/``.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

import config

PRECOMPUTED_ROOT: Path = config.OUTPUT_DIR / "precomputed"

_DISPERSION_TABLE_KEYS: tuple[str, ...] = (
    "dispersion_summary_table",
    "breadth_table",
    "concentration_table",
    "top_contributors",
    "bottom_contributors",
    "industry_participation",
)


def _sector_slug(sector: str) -> str:
    return str(sector).strip().replace("/", "-").replace(" ", "_")


def _parse_as_of(raw: object) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    s = str(raw).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _normalize_dispersion_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Ensure keys and ``tables`` entries match live ``run_dispersion_dashboard_bundle`` output."""
    out = dict(bundle)
    if "summary" not in out or not isinstance(out.get("summary"), dict):
        out["summary"] = {}
    for col in ("universe", "wide_close", "breadth_ts", "dispersion_ts"):
        if col not in out or not isinstance(out[col], pd.DataFrame):
            out[col] = pd.DataFrame()
    tabs = out.get("tables")
    if not isinstance(tabs, dict):
        tabs = {}
    merged: dict[str, pd.DataFrame] = {}
    for k in _DISPERSION_TABLE_KEYS:
        v = tabs.get(k)
        merged[k] = v if isinstance(v, pd.DataFrame) else pd.DataFrame()
    for k, v in tabs.items():
        if k not in merged and isinstance(v, pd.DataFrame):
            merged[k] = v
    out["tables"] = merged
    if "as_of" not in out or out["as_of"] is None:
        out["as_of"] = date.today()
    return out


def try_load_saved_bundle(bundle_dir: Path) -> dict[str, Any] | None:
    """
    Load a directory produced by ``nightly_refresh.save_bundle`` into one dict
    (same top-level keys as live engine bundles).
    """
    bundle_dir = Path(bundle_dir)
    meta_path = bundle_dir / "bundle_meta.json"
    if not meta_path.is_file():
        return None
    try:
        with meta_path.open(encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    bundle: dict[str, Any] = {
        "ok": meta.get("ok"),
        "error": meta.get("error"),
        "as_of": _parse_as_of(meta.get("as_of")),
    }
    try:
        for p in sorted(bundle_dir.glob("*.parquet")):
            bundle[p.stem] = pd.read_parquet(p)
        for p in sorted(bundle_dir.glob("*.json")):
            if p.name == "bundle_meta.json":
                continue
            with p.open(encoding="utf-8") as f:
                bundle[p.stem] = json.load(f)
        tables_dir = bundle_dir / "tables"
        if tables_dir.is_dir():
            tables: dict[str, pd.DataFrame] = {}
            for p in sorted(tables_dir.glob("*.parquet")):
                tables[p.stem] = pd.read_parquet(p)
            bundle["tables"] = tables
    except (OSError, ValueError, ImportError):
        return None

    if "dispersion" in bundle_dir.parts or bundle_dir.parent.name == "dispersion":
        return _normalize_dispersion_bundle(bundle)
    return bundle


def load_industry_rotation_bundle(fmp_sector: str) -> dict[str, Any] | None:
    """Industry rotation vs sector ETF (Technology, Basic Materials, …)."""
    slug = _sector_slug(fmp_sector)
    if not slug:
        return None
    for base in (PRECOMPUTED_ROOT / "rotation", PRECOMPUTED_ROOT / "sector_rotation"):
        got = try_load_saved_bundle(base / slug)
        if got is not None:
            return got
    return None


def load_ai_rotation_bundle() -> dict[str, Any] | None:
    for name in ("ai", "ai_rotation"):
        got = try_load_saved_bundle(PRECOMPUTED_ROOT / name)
        if got is not None:
            return got
    return None


def load_spy_sector_rotation_bundle() -> dict[str, Any] | None:
    for name in ("spy", "spy_sector_rotation"):
        got = try_load_saved_bundle(PRECOMPUTED_ROOT / name)
        if got is not None:
            return got
    return None


def load_dispersion_dashboard_bundle(fmp_sector: str) -> dict[str, Any] | None:
    slug = _sector_slug(fmp_sector)
    if not slug:
        return None
    return try_load_saved_bundle(PRECOMPUTED_ROOT / "dispersion" / slug)


def load_dispersion_universe(fmp_sector: str) -> pd.DataFrame | None:
    """``universe`` slice from a precomputed dispersion bundle, if present."""
    slug = _sector_slug(fmp_sector)
    if not slug:
        return None
    u_path = PRECOMPUTED_ROOT / "dispersion" / slug / "universe.parquet"
    if not u_path.is_file():
        return None
    try:
        return pd.read_parquet(u_path)
    except (OSError, ValueError, ImportError):
        return None


def load_rotation_prices_long() -> pd.DataFrame | None:
    """Optional union long panel saved next to rotation bundles."""
    p = PRECOMPUTED_ROOT / "rotation" / "rotation_prices_long.parquet"
    if not p.is_file():
        return None
    try:
        return pd.read_parquet(p)
    except (OSError, ValueError, ImportError):
        return None
