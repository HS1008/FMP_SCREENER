"""EIA Wholesale Electricity & Natural Gas data loader.

Priority: cached parquet -> local CSV files -> sample data.

Place real data files in ``data/eia_wholesale/`` as:
  power_prices.csv  (columns: date, hub, price)
  gas_prices.csv    (columns: date, hub, price)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import config

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
EIA_CACHE_DIR: Path = config.OUTPUT_DIR / "cache" / "eia_wholesale"
EIA_LOCAL_DIR: Path = config.PROJECT_ROOT / "data" / "eia_wholesale"

# ---------------------------------------------------------------------------
# Hub parameters for sample data generation
# ---------------------------------------------------------------------------
POWER_HUB_PARAMS: dict[str, dict] = {
    "Mass Hub":     {"base": 48, "amp": 18, "vol": 6},
    "PJM West":     {"base": 40, "amp": 12, "vol": 5},
    "Indiana Hub":  {"base": 34, "amp": 10, "vol": 4},
    "ERCOT North":  {"base": 38, "amp": 22, "vol": 9},
    "Mid-C":        {"base": 30, "amp": 10, "vol": 5},
    "NP-15":        {"base": 44, "amp": 14, "vol": 6},
    "Palo Verde":   {"base": 36, "amp": 12, "vol": 5},
    "SP-15":        {"base": 48, "amp": 16, "vol": 6},
}

GAS_HUB_PARAMS: dict[str, dict] = {
    "Henry Hub":         {"basis": 0.00, "vol": 0.12},
    "Algonquin":         {"basis": 1.50, "vol": 0.30},
    "TETCO-M3":          {"basis": 0.35, "vol": 0.12},
    "Chicago Citygates": {"basis": 0.18, "vol": 0.10},
    "Malin":             {"basis": -0.15, "vol": 0.12},
    "PG&E Citygate":     {"basis": 1.10, "vol": 0.22},
    "Socal-Ehrenberg":   {"basis": 0.65, "vol": 0.18},
    "Socal-Citygate":    {"basis": 1.30, "vol": 0.25},
}


# ---------------------------------------------------------------------------
# Local file helpers
# ---------------------------------------------------------------------------
def get_eia_wholesale_files() -> list[Path]:
    """List CSV/Excel files in the local EIA data directory."""
    if not EIA_LOCAL_DIR.is_dir():
        return []
    return sorted(
        p for p in EIA_LOCAL_DIR.iterdir()
        if p.suffix.lower() in {".csv", ".xlsx", ".xls"}
    )


def normalize_eia_columns(df: pd.DataFrame) -> pd.DataFrame | None:
    """Standardize column names and validate required columns."""
    df.columns = df.columns.str.strip().str.lower()
    for col in ("date", "hub", "price"):
        if col not in df.columns:
            return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["date", "price"])
    df["hub"] = df["hub"].str.strip()
    return df.sort_values("date").reset_index(drop=True)


def load_eia_power_prices() -> pd.DataFrame | None:
    """Load power prices from local CSV."""
    path = EIA_LOCAL_DIR / "power_prices.csv"
    if not path.is_file():
        return None
    try:
        return normalize_eia_columns(pd.read_csv(path))
    except Exception:
        return None


def load_eia_gas_prices() -> pd.DataFrame | None:
    """Load gas prices from local CSV."""
    path = EIA_LOCAL_DIR / "gas_prices.csv"
    if not path.is_file():
        return None
    try:
        return normalize_eia_columns(pd.read_csv(path))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Sample data generation
# ---------------------------------------------------------------------------
def _generate_sample_power_prices(days: int = 365) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    end = date.today()
    dates = pd.bdate_range(end=end, periods=days)
    rows: list[dict] = []
    for hub, p in POWER_HUB_PARAMS.items():
        doy = dates.dayofyear.values.astype(float)
        seasonal = p["amp"] * np.sin(2 * np.pi * (doy - 30) / 365)
        noise = rng.normal(0, p["vol"], days)
        walk = np.cumsum(rng.normal(0, 0.25, days))
        prices = np.maximum(p["base"] + seasonal + noise + walk, 5.0)
        for d, v in zip(dates, prices):
            rows.append({"date": d, "hub": hub, "price": round(float(v), 2)})
    return pd.DataFrame(rows)


def _generate_sample_gas_prices(days: int = 365) -> pd.DataFrame:
    rng = np.random.default_rng(123)
    end = date.today()
    dates = pd.bdate_range(end=end, periods=days)
    doy = dates.dayofyear.values.astype(float)
    hh_prices = np.maximum(
        2.80
        + 0.60 * np.sin(2 * np.pi * (doy - 30) / 365)
        + rng.normal(0, 0.12, days)
        + np.cumsum(rng.normal(0, 0.015, days)),
        0.50,
    )
    rows: list[dict] = []
    for hub, p in GAS_HUB_PARAMS.items():
        if hub == "Henry Hub":
            prices = hh_prices
        else:
            basis_s = p["basis"] * (1 + 0.3 * np.sin(2 * np.pi * (doy - 30) / 365))
            prices = np.maximum(
                hh_prices + basis_s + rng.normal(0, p["vol"], days), 0.25
            )
        for d, v in zip(dates, prices):
            rows.append({"date": d, "hub": hub, "price": round(float(v), 2)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------
def _read_cache() -> tuple[pd.DataFrame, pd.DataFrame] | None:
    pp = EIA_CACHE_DIR / "power_prices.parquet"
    gp = EIA_CACHE_DIR / "gas_prices.parquet"
    if not pp.is_file() or not gp.is_file():
        return None
    try:
        return pd.read_parquet(pp), pd.read_parquet(gp)
    except Exception:
        return None


def _write_cache(power: pd.DataFrame, gas: pd.DataFrame) -> None:
    EIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        power.to_parquet(EIA_CACHE_DIR / "power_prices.parquet", index=False)
        gas.to_parquet(EIA_CACHE_DIR / "gas_prices.parquet", index=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Merge hub-pair data
# ---------------------------------------------------------------------------
def merge_power_gas_hubs(
    hub_map: pd.DataFrame,
    power_df: pd.DataFrame,
    gas_df: pd.DataFrame,
) -> pd.DataFrame:
    """Join power and gas prices by date for each hub pair in the map."""
    henry = gas_df.loc[
        gas_df["hub"] == "Henry Hub", ["date", "price"]
    ].rename(columns={"price": "henry_hub_price"})

    parts: list[pd.DataFrame] = []
    for _, row in hub_map.iterrows():
        p = power_df.loc[
            power_df["hub"] == row["power_hub"], ["date", "price"]
        ].rename(columns={"price": "power_price"})
        g = gas_df.loc[
            gas_df["hub"] == row["gas_hub"], ["date", "price"]
        ].rename(columns={"price": "gas_price"})
        m = p.merge(g, on="date", how="inner").merge(henry, on="date", how="left")
        m["region"] = row["region"]
        m["iso"] = row["iso"]
        m["power_hub"] = row["power_hub"]
        m["gas_hub"] = row["gas_hub"]
        parts.append(m)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True).sort_values(["region", "date"])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def load_cached_or_fetch_eia_data(
    force_refresh: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, bool]:
    """
    Returns ``(power_df, gas_df, is_sample_data)``.

    Priority: file cache -> local CSVs -> generated sample data.
    """
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            return cached[0], cached[1], False

    power = load_eia_power_prices()
    gas = load_eia_gas_prices()
    if power is not None and gas is not None and not power.empty and not gas.empty:
        _write_cache(power, gas)
        return power, gas, False

    power = _generate_sample_power_prices()
    gas = _generate_sample_gas_prices()
    return power, gas, True
