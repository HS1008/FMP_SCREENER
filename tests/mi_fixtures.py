"""SYNTHETIC test fixtures for Market Intelligence tests.

Everything here is fabricated for tests/demos only. Nothing is real market data and nothing
in this module may be published as research evidence.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from market_intelligence.fred_client import FredObservation

SYNTHETIC_MARKER = "SYNTHETIC_TEST_FIXTURE"

LEGACY_SECTORS = {
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
RS_COLS = ["1W RS %", "1M RS %", "3M RS %", "6M RS %", "12M RS %", "RS vs 50 DMA %", "RS vs 200 DMA %", "Corr vs SPY"]


def business_days(end: date, n: int) -> list[date]:
    days: list[date] = []
    cur = end
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur -= timedelta(days=1)
    return sorted(days)


def synthetic_prices_long(symbols: list[str], end: date, sessions: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = business_days(end, sessions)
    rows = []
    for i, sym in enumerate(symbols):
        level = 100.0 + 10 * i
        for d in days:
            level *= 1.0 + rng.normal(0.0003, 0.01)
            rows.append({"date": pd.Timestamp(d), "symbol": sym, "adjClose": round(level, 4)})
    return pd.DataFrame(rows)


def _save_json(obj, path: Path) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _slug(label: str) -> str:
    return label.replace("/", "-").replace(" ", "_")


def write_rotation_style_bundle(bundle_dir: Path, *, rows: list[dict], symbols: list[str], as_of: date, ok: bool = True, error: str | None = None, meta_as_of: str | None = "auto", include_nan: bool = True) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    meta = {"ok": ok, "error": error, "as_of": (as_of.isoformat() if meta_as_of == "auto" else meta_as_of)}
    _save_json(meta, bundle_dir / "bundle_meta.json")
    metrics = pd.DataFrame(rows)
    if include_nan and not metrics.empty:
        metrics.loc[metrics.index[0], "12M RS %"] = float("nan")
    metrics.to_parquet(bundle_dir / "metrics.parquet", index=True)
    prices = synthetic_prices_long(symbols, as_of)
    prices.to_parquet(bundle_dir / "prices.parquet", index=True)
    heat = metrics.copy()
    heat.to_parquet(bundle_dir / "heatmap.parquet", index=True)
    rs_hist = pd.DataFrame({"x": np.linspace(0.9, 1.1, 30)}, index=pd.DatetimeIndex(business_days(as_of, 30)))
    rs_hist.to_parquet(bundle_dir / "rs_ratio_history.parquet", index=True)
    return bundle_dir


def rs_row(etf: str, industry: str, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    row = {"ETF": etf, "Industry": industry}
    for col in RS_COLS:
        row[col] = float(rng.normal(0.0, 0.05)) if col != "Corr vs SPY" else float(rng.uniform(0.3, 0.9))
    return row


def write_spy_bundle(root: Path, as_of: date, *, include_unknown_label: bool = False) -> Path:
    rows = [rs_row(etf, sector, i) for i, (sector, etf) in enumerate(sorted(LEGACY_SECTORS.items()))]
    rows.append(rs_row("AIQ", "AI", 99))
    if include_unknown_label:
        rows.append(rs_row("ZZZ", "Mystery Sector", 123))
    symbols = ["SPY", *LEGACY_SECTORS.values(), "AIQ"]
    return write_rotation_style_bundle(root / "spy", rows=rows, symbols=symbols, as_of=as_of)


def write_sector_rotation_bundle(root: Path, sector: str, as_of: date) -> Path:
    industries = [("IGV", "Software"), ("SMH", "Semiconductors"), ("HACK", "Cybersecurity")]
    rows = [rs_row(etf, ind, i + 10) for i, (etf, ind) in enumerate(industries)]
    symbols = [LEGACY_SECTORS.get(sector, "XLK"), *[e for e, _ in industries]]
    return write_rotation_style_bundle(root / "rotation" / _slug(sector), rows=rows, symbols=symbols, as_of=as_of)


def write_theme_bundle(root: Path, as_of: date) -> Path:
    rows = [rs_row("BOTZ", "Robotics", 50), rs_row("AIQ", "AI", 51)]
    return write_rotation_style_bundle(root / "ai", rows=rows, symbols=["AIQ", "BOTZ"], as_of=as_of)


def write_dispersion_bundle(root: Path, sector: str, as_of: date, *, ok: bool = True, meta_as_of: str | None = "auto") -> Path:
    bundle_dir = root / "dispersion" / _slug(sector)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    meta = {"ok": ok, "error": None if ok else "Empty dispersion universe", "as_of": (as_of.isoformat() if meta_as_of == "auto" else meta_as_of)}
    _save_json(meta, bundle_dir / "bundle_meta.json")
    summary = {
        "universe_size": 40,
        "pct_above_50dma": 0.55,
        "pct_above_200dma": 0.62,
        "equal_weight_std": 0.081,
        "cap_weight_std": 0.064,
        "avg_pairwise_corr": 0.41,
        "median_return_1m": 0.012,
        "return_spread": 0.19,
        "top5_weight": 0.48,
        "top10_weight": 0.66,
        "top5_return_contribution": float("nan"),  # legacy writer permits NaN JSON
        "hhi": 0.07,
    }
    # Emulate nightly_refresh.save_json permitting NaN tokens.
    (bundle_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    days = business_days(as_of, 260)
    wide = pd.DataFrame({"AAA": np.linspace(10, 12, len(days)), "BBB": np.linspace(20, 18, len(days))}, index=pd.DatetimeIndex(days))
    wide.to_parquet(bundle_dir / "wide_close.parquet", index=True)
    pd.DataFrame({"symbol": ["AAA", "BBB"], "industry": ["Software", "Hardware"], "marketCap": [1e9, 2e9]}).to_parquet(bundle_dir / "universe.parquet", index=True)
    tables = bundle_dir / "tables"
    tables.mkdir(exist_ok=True)
    pd.DataFrame([{"pct_above_50dma": 0.55, "pct_above_200dma": 0.62, "count_above_50dma": 22, "count_above_200dma": 25, "count_valid_50dma": 40, "count_valid_200dma": 40}]).to_parquet(tables / "breadth_table.parquet", index=True)
    pd.DataFrame(
        [
            {"industry": "Software", "company_count": 20, "avg_return_1m": 0.02, "pct_above_50dma": 0.6, "pct_above_200dma": 0.7, "equal_weight_return_1m": 0.021, "cap_weight_return_1m": 0.03},
            {"industry": "Hardware", "company_count": 20, "avg_return_1m": -0.01, "pct_above_50dma": 0.5, "pct_above_200dma": 0.55, "equal_weight_return_1m": -0.012, "cap_weight_return_1m": float("nan")},
        ]
    ).to_parquet(tables / "industry_participation.parquet", index=True)
    return bundle_dir


def write_full_precomputed_root(root: Path, as_of: date) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_spy_bundle(root, as_of)
    for sector in ("Technology", "Healthcare"):
        write_sector_rotation_bundle(root, sector, as_of)
    write_theme_bundle(root, as_of)
    write_dispersion_bundle(root, "Technology", business_days(as_of - timedelta(days=1), 1)[0])  # dispersion lags rotation by a session
    (root / SYNTHETIC_MARKER).write_text("synthetic fixtures only", encoding="utf-8")
    return root


# ---- synthetic FRED ---------------------------------------------------------------------

class FakeFredClient:
    """Deterministic in-memory FRED double. ``failures`` marks series that raise."""

    def __init__(self, data: dict[str, list[tuple[date, str]]], *, metadata: dict[str, dict] | None = None, failures: set[str] | None = None):
        self.data = data
        self.metadata = metadata or {}
        self.failures = failures or set()
        self.retry_count = 0
        self.request_count = 0
        self.observation_calls: list[tuple[str, date | None, date | None]] = []

    def series_metadata(self, series_id: str) -> dict:
        from market_intelligence.fred_client import FredError

        self.request_count += 1
        if series_id in self.failures:
            raise FredError("FRED HTTP 500 for series", status=500, retryable=True)
        base = {
            "id": series_id,
            "title": "Synthetic {0}".format(series_id),
            "units": _units_for(series_id),
            "units_short": _units_for(series_id),
            "frequency": "Daily" if _is_daily(series_id) else "Monthly",
            "frequency_short": "D" if _is_daily(series_id) else "M",
            "seasonal_adjustment": "Not Seasonally Adjusted" if _is_daily(series_id) else "Seasonally Adjusted",
            "seasonal_adjustment_short": "NSA" if _is_daily(series_id) else "SA",
            "observation_start": "2000-01-01",
            "observation_end": "2024-12-31",
            "last_updated": "2024-12-31 16:00:00-06",
        }
        base.update(self.metadata.get(series_id, {}))
        return base

    def observations(self, series_id: str, *, observation_start=None, observation_end=None, **_kw) -> list[FredObservation]:
        self.request_count += 1
        self.observation_calls.append((series_id, observation_start, observation_end))
        rows = self.data.get(series_id, [])
        out = []
        for d, v in rows:
            if observation_start and d < observation_start:
                continue
            if observation_end and d > observation_end:
                continue
            out.append(FredObservation(d, v, date(2024, 1, 1), date(9999, 12, 31)))
        return out


def _is_daily(series_id: str) -> bool:
    return series_id.startswith(("DGS", "DFII", "T5Y", "T10Y", "BAML", "DFF", "SOFR", "RRPONTSYD"))


def _units_for(series_id: str) -> str:
    if _is_daily(series_id) or series_id == "UNRATE":
        return "Percent"
    if series_id.startswith("CPI") or series_id.startswith("PCE") or series_id == "INDPRO":
        return "Index 1982-1984=100"
    if series_id == "PAYEMS":
        return "Thousands of Persons"
    if series_id in {"ICSA", "CCSA"}:
        return "Number"
    if series_id == "GDPC1":
        return "Billions of Chained 2017 Dollars"
    return "Billions of U.S. Dollars"


def daily_series(end: date, sessions: int, start_value: float, step: float, *, missing_every: int | None = None) -> list[tuple[date, str]]:
    days = business_days(end, sessions)
    out = []
    for i, d in enumerate(days):
        if missing_every and i % missing_every == 3:
            out.append((d, "."))
        else:
            out.append((d, "{0:.2f}".format(start_value + step * i)))
    return out


def monthly_index(end_month: date, months: int, start_value: float, monthly_growth: float) -> list[tuple[date, str]]:
    out = []
    year, month = end_month.year, end_month.month
    values = []
    for _ in range(months):
        values.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    values.reverse()
    level = start_value
    for d in values:
        out.append((d, "{0:.3f}".format(level)))
        level *= 1 + monthly_growth
    return out


def synthetic_fred_data(end: date = date(2024, 12, 31)) -> dict[str, list[tuple[date, str]]]:
    data: dict[str, list[tuple[date, str]]] = {}
    for sid, start in (("DGS3MO", 4.3), ("DGS6MO", 4.25), ("DGS1", 4.2), ("DGS2", 4.1), ("DGS3", 4.05), ("DGS5", 4.0), ("DGS7", 4.05), ("DGS10", 4.1), ("DGS20", 4.4), ("DGS30", 4.5)):
        data[sid] = daily_series(end, 800, start, 0.0005, missing_every=97)
    for sid, start in (("DFII5", 1.8), ("DFII10", 1.9), ("DFII20", 2.0), ("DFII30", 2.1), ("T5YIE", 2.3), ("T10YIE", 2.25), ("T5YIFR", 2.2), ("DFF", 4.33), ("SOFR", 4.3)):
        data[sid] = daily_series(end, 800, start, 0.0002)
    for sid, start in (("BAMLC0A0CM", 0.80), ("BAMLH0A0HYM2", 2.9), ("BAMLC0A1CAAA", 0.4), ("BAMLC0A2CAA", 0.5), ("BAMLC0A3CA", 0.7), ("BAMLC0A4CBBB", 1.0), ("BAMLH0A1HYBB", 1.9), ("BAMLH0A2HYB", 2.8), ("BAMLH0A3HYC", 8.0)):
        data[sid] = daily_series(end, 300, start, 0.0007)  # ICE history limited (~14 months) by design
    for sid, start, g in (("CPIAUCSL", 290.0, 0.0025), ("CPILFESL", 300.0, 0.0022), ("PCEPI", 118.0, 0.002), ("PCEPILFE", 119.0, 0.0021), ("INDPRO", 102.0, 0.001), ("M2SL", 20800.0, 0.003), ("RSAFS", 690000.0, 0.003)):
        data[sid] = monthly_index(date(end.year, end.month, 1), 40, start, g)
    data["PAYEMS"] = [(d, "{0:.0f}".format(155000 + 150 * i)) for i, (d, _) in enumerate(monthly_index(date(end.year, end.month, 1), 40, 1, 0))]
    data["UNRATE"] = [(d, "{0:.1f}".format(3.6 + 0.02 * i)) for i, (d, _) in enumerate(monthly_index(date(end.year, end.month, 1), 40, 1, 0))]
    data["GDPC1"] = [(date(y, m, 1), "{0:.1f}".format(22000 + 100 * i)) for i, (y, m) in enumerate((yy, mm) for yy in range(2019, 2025) for mm in (1, 4, 7, 10))]
    weekly_days = [end - timedelta(days=7 * i) for i in range(120)][::-1]
    data["ICSA"] = [(d, "{0:.0f}".format(210000 + (i % 5) * 1000)) for i, d in enumerate(weekly_days)]
    data["CCSA"] = [(d, "{0:.0f}".format(1800000 + (i % 7) * 5000)) for i, d in enumerate(weekly_days)]
    data["WALCL"] = [(d, "{0:.0f}".format(7000000 - 3000 * i)) for i, d in enumerate(weekly_days)]
    data["WTREGEN"] = [(d, "{0:.1f}".format(700 + (i % 9) * 10)) for i, d in enumerate(weekly_days)]
    data["WRESBAL"] = [(d, "{0:.1f}".format(3300 - 2 * i)) for i, d in enumerate(weekly_days)]
    data["RRPONTSYD"] = daily_series(end, 400, 500.0, -0.5)
    return data


def fake_fred_client(end: date = date(2024, 12, 31), **kw) -> FakeFredClient:
    return FakeFredClient(synthetic_fred_data(end), **kw)


__all__ = [
    "FakeFredClient",
    "LEGACY_SECTORS",
    "RS_COLS",
    "SYNTHETIC_MARKER",
    "business_days",
    "daily_series",
    "fake_fred_client",
    "monthly_index",
    "synthetic_fred_data",
    "write_dispersion_bundle",
    "write_full_precomputed_root",
    "write_sector_rotation_bundle",
    "write_spy_bundle",
    "write_theme_bundle",
]
