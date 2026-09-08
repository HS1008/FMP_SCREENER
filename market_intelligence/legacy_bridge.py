"""Legacy FMP bridge: read saved ``outputs/precomputed`` bundles -> canonical sector snapshots.

Reads Parquet/JSON written by ``nightly_refresh.save_bundle`` directly. It never imports
``nightly_refresh``, ``precomputed_loader`` (which substitutes ``date.today()`` for a
missing as_of), ``data_loader`` or any engine module, and never calls FMP.

Field / unit / benchmark mapping (``legacy_bridge_v1``):

``spy/``  (dataset ``ETF_RS_VS_SPY``, one row per sector ETF + AI theme)
    metrics.parquet columns ``1W RS %`` ... ``12M RS %`` are *fractions* (engine
    docstring: "decimals, heatmap x100") of the relative price ratio ETF/SPY:
    ``rs_t / rs_{t-n} - 1`` (n = 5/21/63/126/252 sessions). ``RS vs 50 DMA %`` and
    ``RS vs 200 DMA %`` are ``rs_last / mean(rs, window) - 1``. ``Corr vs SPY`` is a rolling
    return correlation when present. Prices are FMP ``adjClose``; the RS ratio is a
    relative-price-ratio change, not an arithmetic excess return. Benchmark: SPY.
    From ``prices.parquet`` this bridge also computes ETF-level returns
    (``ret_1w`` ... ``ret_12m`` fractions on adjClose), trend (``pct_vs_50dma``,
    ``pct_vs_200dma``), and risk (``vol_63d_ann`` annualized daily std x sqrt(252),
    ``max_drawdown_252d``); these are ETF returns, not constituent portfolio returns.

Session coverage (``legacy_bridge_v2``):
    Windows are counted on the bundle's *session calendar* (the union of dated rows in
    ``prices.parquet``), never on a NULL-compressed per-symbol series: a symbol missing 30
    sessions does not get a "1M" return that really spans two months. Every windowed metric
    is NULL unless its full window exists on the calendar and at most
    ``MAX_MISSING_FRACTION`` of the window is NULL for that symbol (then ``PARTIAL``); the
    per-metric verdict is stored in ``coverage.price_metrics``. A symbol whose last valid
    price precedes the bundle ``as_of`` is ``price_status=STALE`` with ``last_price_date``;
    the bundle-wide as_of never relabels it current. The stored body's ``artifact_sha256``
    covers the *complete* stored body including revision provenance
    (``content_sha256``/``revision_seq``/``previous_sha256``); see ``verify_stored_snapshot``.

``rotation/<Sector_slug>/`` (dataset ``INDUSTRY_RS_VS_SECTOR_ETF``)
    Same RS columns per industry ETF against the sector ETF benchmark.

``ai/`` and ``semi_rotation/`` (dataset ``THEME_RS``)
    Theme baskets; stored under ``THEME:*`` keys, never as a twelfth sector.

``dispersion/<Sector_slug>/`` (dataset ``CONSTITUENT_DISPERSION``)
    ``summary.json`` breadth/dispersion/concentration on the FMP profile-bulk *current*
    universe with *current* market caps: ``universe_method =
    FMP_PROFILE_BULK_CURRENT_UNIVERSE``, ``research_eligible = false``,
    ``CURRENT_UNIVERSE_CONTEXT_ONLY``. ``tables/industry_participation.parquet`` ->
    dataset ``INDUSTRY_PARTICIPATION`` with equal-weight vs (current) cap-weight 1M returns.

as_of comes from ``bundle_meta.json`` and must equal the max date of the dated data in the
bundle. Missing/inconsistent timestamps quarantine the bundle. File mtime is provenance only.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

from market_intelligence.nulls import canonical_sha256, normalize_payload, strict_dumps
from market_intelligence.sector_mapping import (
    CLASSIFICATION_VERSION,
    SECTOR_ETF_PROXY,
    SectorResolution,
    resolve_provider_sector,
    slug_to_label,
)
from market_intelligence.store import (
    RUN_FAILED,
    RUN_SKIPPED,
    RUN_SUCCEEDED,
    TRANSPORT_FAILED,
    TRANSPORT_OK,
    finish_run,
    record_freshness,
    start_run,
)

logger = logging.getLogger(__name__)

LEGACY_SOURCE_ID = "FMP_LEGACY"
SCHEMA_VERSION = "legacy_sector_snapshot_v1"
METHODOLOGY_VERSION = "legacy_bridge_v2"
TRANSPORT_PARTIAL = "PARTIAL"
# Engineering tolerance for NULL sessions inside a window (FMP occasionally drops a print).
MAX_MISSING_FRACTION = 0.05
COVERAGE_FULL = "FULL"
COVERAGE_PARTIAL = "PARTIAL"
COVERAGE_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
COVERAGE_TOO_MANY_NULLS = "TOO_MANY_NULLS"
COVERAGE_ENDPOINT_NULL = "ENDPOINT_NULL"
PRICE_OK = "OK"
PRICE_STALE = "STALE"
PRICE_UNAVAILABLE = "UNAVAILABLE"
REVISION_KEYS = ("content_sha256", "revision_seq", "previous_sha256")
VOLATILE_PROVENANCE_KEYS = ("file_fingerprint_sha256", "newest_file_mtime_utc")
RS_COLUMNS = {
    "1W RS %": "rs_chg_1w",
    "1M RS %": "rs_chg_1m",
    "3M RS %": "rs_chg_3m",
    "6M RS %": "rs_chg_6m",
    "12M RS %": "rs_chg_12m",
    "RS vs 50 DMA %": "rs_vs_50dma",
    "RS vs 200 DMA %": "rs_vs_200dma",
    "Corr vs SPY": "corr_vs_spy_63d",
}
RETURN_WINDOWS = {"ret_1w": 5, "ret_1m": 21, "ret_3m": 63, "ret_6m": 126, "ret_12m": 252}
THEME_DIRS = {"ai": "AI", "semi_rotation": "Semiconductors"}
THEME_BENCHMARKS = {"ai": "AIQ", "semi_rotation": "XLK"}
READ_RETRIES = 2


class BundleQuarantined(ValueError):
    """Bundle cannot be used as a valid snapshot (reason in message)."""


@dataclass
class BundleRead:
    path: Path
    meta: dict[str, Any]
    frames: dict[str, pd.DataFrame]
    tables: dict[str, pd.DataFrame]
    jsons: dict[str, Any]
    fingerprint: tuple
    as_of: date
    as_of_source: str

    @property
    def provenance(self) -> dict[str, Any]:
        newest_mtime = max((entry[2] for entry in self.fingerprint), default=None)
        return {
            "bundle_dir": str(self.path),
            "file_fingerprint_sha256": canonical_sha256({"files": [list(e) for e in self.fingerprint]}),
            "newest_file_mtime_utc": datetime.fromtimestamp(newest_mtime / 1e9, tz=timezone.utc).isoformat() if newest_mtime else None,
            "as_of_source": self.as_of_source,
            "note": "file mtime is provenance, not market as_of",
        }


def directory_fingerprint(path: Path) -> tuple:
    entries = []
    for file in sorted(p for p in Path(path).rglob("*") if p.is_file()):
        stat = file.stat()
        entries.append((str(file.relative_to(path)), stat.st_size, stat.st_mtime_ns))
    return tuple(entries)


def _parse_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text_value = str(raw).strip()
    if not text_value or text_value.lower() in {"none", "null", "nat", "nan"}:
        return None
    try:
        return date.fromisoformat(text_value[:10])
    except ValueError:
        return None


def _max_date(frame: pd.DataFrame | None, *, column: str | None = None) -> date | None:
    if frame is None or frame.empty:
        return None
    if column and column in frame.columns:
        series = pd.to_datetime(frame[column], errors="coerce")
    elif isinstance(frame.index, pd.DatetimeIndex):
        series = pd.Series(frame.index)
    else:
        try:
            series = pd.to_datetime(pd.Series(frame.index), errors="coerce")
        except (TypeError, ValueError):
            return None
    series = series.dropna()
    if series.empty:
        return None
    return series.max().date()


def dated_max(frames: dict[str, pd.DataFrame]) -> date | None:
    candidates = []
    if "prices" in frames:
        candidates.append(_max_date(frames["prices"], column="date"))
    for key in ("rs_ratio_history", "wide_close", "breadth_ts", "dispersion_ts"):
        if key in frames:
            candidates.append(_max_date(frames[key]))
    valid = [c for c in candidates if c is not None]
    return max(valid) if valid else None


def read_bundle(bundle_dir: Path, *, retries: int = READ_RETRIES) -> BundleRead:
    """Load a saved bundle; retry when files change mid-read; quarantine invalid bundles."""
    bundle_dir = Path(bundle_dir)
    meta_path = bundle_dir / "bundle_meta.json"
    if not meta_path.is_file():
        raise BundleQuarantined("missing bundle_meta.json")
    last_error: Exception | None = None
    for _ in range(retries + 1):
        before = directory_fingerprint(bundle_dir)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            frames: dict[str, pd.DataFrame] = {}
            jsons: dict[str, Any] = {}
            tables: dict[str, pd.DataFrame] = {}
            for p in sorted(bundle_dir.glob("*.parquet")):
                frames[p.stem] = pd.read_parquet(p)
            for p in sorted(bundle_dir.glob("*.json")):
                if p.name == "bundle_meta.json":
                    continue
                jsons[p.stem] = json.loads(p.read_text(encoding="utf-8"))  # legacy writer permits NaN tokens; normalized later
            tables_dir = bundle_dir / "tables"
            if tables_dir.is_dir():
                for p in sorted(tables_dir.glob("*.parquet")):
                    tables[p.stem] = pd.read_parquet(p)
        except (OSError, ValueError) as exc:
            last_error = exc
            continue
        after = directory_fingerprint(bundle_dir)
        if after != before:
            last_error = RuntimeError("bundle changed during read")
            continue
        if not isinstance(meta, dict):
            raise BundleQuarantined("bundle_meta.json is not an object")
        if meta.get("ok") is not True:
            raise BundleQuarantined("bundle ok={0!r} error={1!r}".format(meta.get("ok"), str(meta.get("error"))[:120]))
        meta_as_of = _parse_date(meta.get("as_of"))
        data_as_of = dated_max(frames)
        if meta_as_of is None and data_as_of is None:
            raise BundleQuarantined("no as_of in metadata and no dated data")
        if meta_as_of is not None and data_as_of is not None and meta_as_of != data_as_of:
            raise BundleQuarantined("as_of {0} inconsistent with dated data max {1}".format(meta_as_of, data_as_of))
        as_of = meta_as_of or data_as_of
        as_of_source = "bundle_meta" if meta_as_of is not None else "derived_from_dated_data"
        if not frames and not jsons and not tables:
            raise BundleQuarantined("bundle has no data files")
        return BundleRead(bundle_dir, meta, frames, tables, jsons, after, as_of, as_of_source)
    raise BundleQuarantined("unstable bundle read: {0}".format(last_error))


# ---- metric extraction ----------------------------------------------------------------

def _clean_metrics(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col, key in RS_COLUMNS.items():
        if col in row:
            val = normalize_payload(row[col])
            out[key] = val if isinstance(val, (int, float)) or val is None else None
    return out


def _wide_prices(frames: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    prices = frames.get("prices")
    if prices is None or prices.empty or not {"date", "symbol", "adjClose"}.issubset(prices.columns):
        return None
    wide = prices.pivot_table(index="date", columns="symbol", values="adjClose", aggfunc="last")
    wide.index = pd.to_datetime(wide.index, errors="coerce")
    return wide.sort_index()


PRICE_METRIC_KEYS = (*RETURN_WINDOWS, "pct_vs_50dma", "pct_vs_200dma", "vol_63d_ann", "max_drawdown_252d")
# window length in sessions per metric; returns need window+1 sessions (start and end points).
_WINDOW_SESSIONS = {**RETURN_WINDOWS, "pct_vs_50dma": 50, "pct_vs_200dma": 200, "vol_63d_ann": 64, "max_drawdown_252d": 252}


def _window_coverage(window: pd.Series, *, endpoints: tuple[int, ...] = ()) -> tuple[str, int]:
    """Coverage verdict for one metric window on the session calendar plus the NULL count."""
    nulls = int(window.isna().sum())
    for pos in endpoints:
        if pd.isna(window.iloc[pos]):
            return COVERAGE_ENDPOINT_NULL, nulls
    if nulls == 0:
        return COVERAGE_FULL, nulls
    if nulls / len(window) > MAX_MISSING_FRACTION:
        return COVERAGE_TOO_MANY_NULLS, nulls
    return COVERAGE_PARTIAL, nulls


def etf_metrics_from_prices(wide: pd.DataFrame | None, symbol: str, *, as_of: date | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """ETF return/trend/risk from adjClose (fractions) on the bundle session calendar.

    Returns ``(metrics, coverage)``. Every metric is NULL unless its full window exists on the
    calendar; windows with NULL sessions above ``MAX_MISSING_FRACTION`` or NULL endpoints are
    NULL too, and the reason is recorded per metric in ``coverage["price_metrics"]``.
    """
    out: dict[str, Any] = {key: None for key in PRICE_METRIC_KEYS}
    coverage: dict[str, Any] = {
        "price_status": PRICE_UNAVAILABLE,
        "calendar_sessions": len(wide.index) if wide is not None else 0,
        "valid_sessions": 0,
        "null_sessions": None,
        "last_price_date": None,
        "stale_sessions": None,
        "price_metrics": {key: COVERAGE_INSUFFICIENT_HISTORY for key in PRICE_METRIC_KEYS},
        "max_missing_fraction": MAX_MISSING_FRACTION,
    }
    if wide is None or symbol not in wide.columns or wide.empty:
        return out, coverage
    px = pd.to_numeric(wide[symbol], errors="coerce")
    px = px.where(px > 0)  # non-positive prices are data errors, never a valid level
    valid = px.dropna()
    if valid.empty:
        return out, coverage
    coverage["valid_sessions"] = len(valid)
    coverage["null_sessions"] = int(len(px) - len(valid))
    last_date = valid.index[-1].date() if hasattr(valid.index[-1], "date") else _parse_date(valid.index[-1])
    coverage["last_price_date"] = last_date.isoformat() if last_date else None
    calendar_end = px.index[-1].date() if hasattr(px.index[-1], "date") else _parse_date(px.index[-1])
    reference = as_of or calendar_end
    stale_sessions = int((px.index > valid.index[-1]).sum())
    if reference is not None and last_date is not None and last_date < reference:
        coverage["price_status"] = PRICE_STALE
        coverage["stale_sessions"] = stale_sessions if stale_sessions else None
    else:
        coverage["price_status"] = PRICE_OK
        coverage["stale_sessions"] = 0

    verdicts = coverage["price_metrics"]
    for key, n in RETURN_WINDOWS.items():
        if len(px) <= n:
            continue
        window = px.iloc[-1 - n :]
        verdict, _ = _window_coverage(window, endpoints=(0, -1))
        verdicts[key] = verdict
        if verdict in (COVERAGE_FULL, COVERAGE_PARTIAL):
            out[key] = float(window.iloc[-1]) / float(window.iloc[0]) - 1.0
    for key, window_n in (("pct_vs_50dma", 50), ("pct_vs_200dma", 200)):
        if len(px) < window_n:
            continue
        window = px.iloc[-window_n:]
        verdict, _ = _window_coverage(window, endpoints=(-1,))
        verdicts[key] = verdict
        if verdict in (COVERAGE_FULL, COVERAGE_PARTIAL):
            ma = float(window.mean(skipna=True))
            out[key] = float(window.iloc[-1]) / ma - 1.0 if ma > 0 else None
    if len(px) >= _WINDOW_SESSIONS["vol_63d_ann"]:
        window = px.iloc[-_WINDOW_SESSIONS["vol_63d_ann"] :]
        verdict, _ = _window_coverage(window)
        verdicts["vol_63d_ann"] = verdict
        if verdict in (COVERAGE_FULL, COVERAGE_PARTIAL):
            # NULL sessions inside the window are skipped, not zero-filled; the verdict discloses them.
            rets = window.dropna().pct_change().dropna()
            out["vol_63d_ann"] = float(rets.std(ddof=1)) * math.sqrt(252.0) if len(rets) >= 2 else None
    if len(px) >= 252:
        window = px.iloc[-252:]
        verdict, _ = _window_coverage(window)
        verdicts["max_drawdown_252d"] = verdict
        if verdict in (COVERAGE_FULL, COVERAGE_PARTIAL):
            filled = window.dropna()
            out["max_drawdown_252d"] = float((filled / filled.cummax() - 1.0).min())
    return normalize_payload(out), normalize_payload(coverage)


def _snapshot_payload(**fields: Any) -> dict[str, Any]:
    body = normalize_payload(fields)
    body["artifact_sha256"] = canonical_sha256(body)
    return body


def _content_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Payload without its digest, revision provenance and volatile file provenance.

    Re-saving an identical bundle changes file mtimes/fingerprints but not the snapshot content,
    so those keys stay in the stored body (and its ``artifact_sha256``) but not in ``content_sha256``.
    """
    body = {k: v for k, v in payload.items() if k != "artifact_sha256"}
    provenance = {k: v for k, v in (body.get("provenance") or {}).items() if k not in REVISION_KEYS and k not in VOLATILE_PROVENANCE_KEYS}
    body["provenance"] = provenance
    return body


def _stored_body(payload: dict[str, Any], *, existing: dict[str, Any] | None) -> dict[str, Any]:
    """Final stored body: content + file/revision provenance, ``artifact_sha256`` over all of it."""
    content = _content_body(payload)
    content_sha = canonical_sha256(content)
    provenance = {k: v for k, v in (payload.get("provenance") or {}).items() if k not in REVISION_KEYS}
    provenance["content_sha256"] = content_sha
    if existing is None:
        provenance["revision_seq"] = 1
    else:
        prior_prov = existing.get("provenance_json") or {}
        provenance["revision_seq"] = int(prior_prov.get("revision_seq") or 1) + 1
        provenance["previous_sha256"] = existing["artifact_sha256"]
    body = dict(content)
    body["provenance"] = provenance
    body["artifact_sha256"] = canonical_sha256(body)
    return body


def _existing_content_sha(existing: dict[str, Any] | None) -> str | None:
    if existing is None:
        return None
    prov = existing.get("provenance_json") or {}
    # Rows written by legacy_bridge_v1 hashed the body before revision provenance was added,
    # so their artifact_sha256 equals the content hash of that body.
    return prov.get("content_sha256") or existing["artifact_sha256"]


def snapshot_body_from_row(row: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    """Reconstruct the canonical stored body from a ``mi_sector_snapshots``/``mi_industry_snapshots`` row."""
    common = {
        "source_id": row["source_id"],
        "dataset": row["dataset"],
        "instrument_id": row.get("instrument_id"),
        "as_of": row["as_of"].isoformat() if hasattr(row["as_of"], "isoformat") else str(row["as_of"]),
        "schema_version": row["schema_version"],
        "methodology_version": row["methodology_version"],
        "benchmark": row.get("benchmark"),
        "return_basis": row.get("return_basis"),
        "value_basis": row.get("value_basis"),
        "metrics": row.get("metrics_json") or {},
        "provenance": row.get("provenance_json") or {},
    }
    if kind == "sector":
        common.update(
            {
                "entity_kind": row["entity_kind"],
                "sector_key": row["sector_key"],
                "canonical_sector": row.get("canonical_sector"),
                "provider_label": row.get("provider_label"),
                "universe_method": row.get("universe_method"),
                "research_eligible": bool(row.get("research_eligible", False)),
                "coverage": row.get("coverage_json") or {},
                "source_refs": row.get("source_refs") or {},
            }
        )
    else:
        common.update({"parent_sector_key": row["parent_sector_key"], "industry_key": row["industry_key"]})
    return normalize_payload(common)


def verify_stored_snapshot(row: Mapping[str, Any], *, kind: str) -> bool:
    """True when the row's ``artifact_sha256`` matches the canonical hash of its stored body."""
    body = snapshot_body_from_row(row, kind=kind)
    return canonical_sha256(body) == row["artifact_sha256"]


def _upsert_sector_snapshot(conn, payload: dict[str, Any], run_id: str | None) -> str:
    existing = conn.execute(
        text(
            """
            SELECT artifact_sha256, provenance_json FROM mi_sector_snapshots
            WHERE source_id = :source_id AND dataset = :dataset AND sector_key = :sector_key
              AND as_of = :as_of AND methodology_version = :methodology_version
            """
        ),
        {k: payload[k] for k in ("source_id", "dataset", "sector_key", "as_of", "methodology_version")},
    ).mappings().first()
    if _existing_content_sha(existing) == canonical_sha256(_content_body(payload)):
        return "unchanged"
    payload = _stored_body(payload, existing=existing)
    provenance = payload["provenance"]
    conn.execute(
        text(
            """
            INSERT INTO mi_sector_snapshots (
                source_id, dataset, entity_kind, sector_key, canonical_sector, provider_label, instrument_id, as_of,
                schema_version, methodology_version, benchmark, return_basis, value_basis, universe_method,
                research_eligible, metrics_json, coverage_json, provenance_json, source_refs, artifact_sha256,
                ingestion_run_id, ingested_at
            ) VALUES (
                :source_id, :dataset, :entity_kind, :sector_key, :canonical_sector, :provider_label, :instrument_id, :as_of,
                :schema_version, :methodology_version, :benchmark, :return_basis, :value_basis, :universe_method,
                :research_eligible, CAST(:metrics AS JSONB), CAST(:coverage AS JSONB), CAST(:provenance AS JSONB),
                CAST(:source_refs AS JSONB), :artifact_sha256, :run_id, NOW()
            )
            ON CONFLICT (source_id, dataset, sector_key, as_of, methodology_version) DO UPDATE SET
                metrics_json = EXCLUDED.metrics_json,
                coverage_json = EXCLUDED.coverage_json,
                provenance_json = EXCLUDED.provenance_json,
                source_refs = EXCLUDED.source_refs,
                artifact_sha256 = EXCLUDED.artifact_sha256,
                ingestion_run_id = EXCLUDED.ingestion_run_id,
                ingested_at = NOW()
            """
        ),
        {
            "source_id": payload["source_id"],
            "dataset": payload["dataset"],
            "entity_kind": payload["entity_kind"],
            "sector_key": payload["sector_key"],
            "canonical_sector": payload.get("canonical_sector"),
            "provider_label": payload.get("provider_label"),
            "instrument_id": payload.get("instrument_id"),
            "as_of": date.fromisoformat(payload["as_of"]),
            "schema_version": payload["schema_version"],
            "methodology_version": payload["methodology_version"],
            "benchmark": payload.get("benchmark"),
            "return_basis": payload.get("return_basis"),
            "value_basis": payload.get("value_basis"),
            "universe_method": payload.get("universe_method"),
            "research_eligible": bool(payload.get("research_eligible", False)),
            "metrics": strict_dumps(payload.get("metrics") or {}),
            "coverage": strict_dumps(payload.get("coverage") or {}),
            "provenance": strict_dumps(provenance),
            "source_refs": strict_dumps(payload.get("source_refs") or {}),
            "artifact_sha256": payload["artifact_sha256"],
            "run_id": run_id,
        },
    )
    return "revised" if existing else "inserted"


def _upsert_industry_snapshot(conn, payload: dict[str, Any], run_id: str | None) -> str:
    existing = conn.execute(
        text(
            """
            SELECT artifact_sha256, provenance_json FROM mi_industry_snapshots
            WHERE source_id = :source_id AND dataset = :dataset AND parent_sector_key = :parent_sector_key
              AND industry_key = :industry_key AND as_of = :as_of AND methodology_version = :methodology_version
            """
        ),
        {k: payload[k] for k in ("source_id", "dataset", "parent_sector_key", "industry_key", "as_of", "methodology_version")},
    ).mappings().first()
    if _existing_content_sha(existing) == canonical_sha256(_content_body(payload)):
        return "unchanged"
    payload = _stored_body(payload, existing=existing)
    provenance = payload["provenance"]
    conn.execute(
        text(
            """
            INSERT INTO mi_industry_snapshots (
                source_id, dataset, parent_sector_key, industry_key, instrument_id, as_of, schema_version,
                methodology_version, benchmark, return_basis, value_basis, metrics_json, provenance_json,
                artifact_sha256, ingestion_run_id, ingested_at
            ) VALUES (
                :source_id, :dataset, :parent_sector_key, :industry_key, :instrument_id, :as_of, :schema_version,
                :methodology_version, :benchmark, :return_basis, :value_basis, CAST(:metrics AS JSONB),
                CAST(:provenance AS JSONB), :artifact_sha256, :run_id, NOW()
            )
            ON CONFLICT (source_id, dataset, parent_sector_key, industry_key, as_of, methodology_version) DO UPDATE SET
                metrics_json = EXCLUDED.metrics_json,
                provenance_json = EXCLUDED.provenance_json,
                artifact_sha256 = EXCLUDED.artifact_sha256,
                ingestion_run_id = EXCLUDED.ingestion_run_id,
                ingested_at = NOW()
            """
        ),
        {
            "source_id": payload["source_id"],
            "dataset": payload["dataset"],
            "parent_sector_key": payload["parent_sector_key"],
            "industry_key": payload["industry_key"],
            "instrument_id": payload.get("instrument_id"),
            "as_of": date.fromisoformat(payload["as_of"]),
            "schema_version": payload["schema_version"],
            "methodology_version": payload["methodology_version"],
            "benchmark": payload.get("benchmark"),
            "return_basis": payload.get("return_basis"),
            "value_basis": payload.get("value_basis"),
            "metrics": strict_dumps(payload.get("metrics") or {}),
            "provenance": strict_dumps(provenance),
            "artifact_sha256": payload["artifact_sha256"],
            "run_id": run_id,
        },
    )
    return "revised" if existing else "inserted"


@dataclass
class BundleOutcome:
    bundle: str
    dataset: str
    status: str
    as_of: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    reason: str | None = None
    quarantined_labels: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "bundle": self.bundle,
            "dataset": self.dataset,
            "status": self.status,
            "as_of": self.as_of,
            "counts": dict(self.counts),
            "reason": self.reason,
            "quarantined_labels": list(self.quarantined_labels),
        }


def _bump(counts: dict[str, int], outcome: str) -> None:
    counts[outcome] = counts.get(outcome, 0) + 1


def ingest_spy_bundle(conn, bundle: BundleRead, *, run_id: str | None) -> BundleOutcome:
    metrics = bundle.frames.get("metrics")
    outcome = BundleOutcome(str(bundle.path), "ETF_RS_VS_SPY", RUN_SUCCEEDED, bundle.as_of.isoformat())
    if metrics is None or metrics.empty:
        outcome.status = RUN_FAILED
        outcome.reason = "metrics.parquet missing or empty"
        return outcome
    wide = _wide_prices(bundle.frames)
    for row in metrics.to_dict(orient="records"):
        label = str(row.get("Industry") or "")
        etf = str(row.get("ETF") or "").upper()
        res = resolve_provider_sector(label)
        if res.quarantined:
            outcome.quarantined_labels.append(label)
            _bump(outcome.counts, "quarantined")
            continue
        price_metrics, price_coverage = etf_metrics_from_prices(wide, etf, as_of=bundle.as_of)
        if price_coverage["price_status"] == PRICE_STALE:
            _bump(outcome.counts, "stale_instruments")
        payload = _snapshot_payload(
            source_id=LEGACY_SOURCE_ID,
            dataset="ETF_RS_VS_SPY",
            entity_kind=res.entity_kind,
            sector_key=res.sector_key,
            canonical_sector=res.canonical_sector,
            provider_label=label,
            instrument_id=etf,
            as_of=bundle.as_of.isoformat(),
            schema_version=SCHEMA_VERSION,
            methodology_version=METHODOLOGY_VERSION,
            benchmark="SPY",
            return_basis="relative_price_ratio_change_adjClose",
            value_basis="fraction",
            universe_method="ETF_PROXY",
            research_eligible=False,
            metrics={**_clean_metrics(row), **price_metrics},
            coverage={"classification_version": CLASSIFICATION_VERSION, "instrument_kind": "ETF_PROXY", "rs_metrics": "ENGINE_PRECOMPUTED_AT_BUNDLE_AS_OF", **price_coverage},
            provenance=bundle.provenance,
            source_refs={"provider": "FMP", "bundle": "spy", "price_field": "adjClose"},
        )
        _bump(outcome.counts, _upsert_sector_snapshot(conn, payload, run_id))
    return outcome


def ingest_rotation_bundle(conn, bundle: BundleRead, *, parent: SectorResolution, benchmark: str | None, dataset: str, run_id: str | None) -> BundleOutcome:
    metrics = bundle.frames.get("metrics")
    outcome = BundleOutcome(str(bundle.path), dataset, RUN_SUCCEEDED, bundle.as_of.isoformat())
    if metrics is None or metrics.empty:
        outcome.status = RUN_FAILED
        outcome.reason = "metrics.parquet missing or empty"
        return outcome
    wide = _wide_prices(bundle.frames)
    for row in metrics.to_dict(orient="records"):
        industry = str(row.get("Industry") or "")
        etf = str(row.get("ETF") or "").upper()
        if not industry and not etf:
            _bump(outcome.counts, "rejected")
            continue
        price_metrics, price_coverage = etf_metrics_from_prices(wide, etf, as_of=bundle.as_of)
        if price_coverage["price_status"] == PRICE_STALE:
            _bump(outcome.counts, "stale_instruments")
        payload = _snapshot_payload(
            source_id=LEGACY_SOURCE_ID,
            dataset=dataset,
            parent_sector_key=parent.sector_key,
            industry_key=industry or etf,
            instrument_id=etf or None,
            as_of=bundle.as_of.isoformat(),
            schema_version=SCHEMA_VERSION,
            methodology_version=METHODOLOGY_VERSION,
            benchmark=benchmark,
            return_basis="relative_price_ratio_change_adjClose",
            value_basis="fraction",
            # mi_industry_snapshots has no coverage column; coverage travels inside metrics_json.
            metrics={**_clean_metrics(row), **price_metrics, "coverage": price_coverage},
            provenance={**bundle.provenance, "provider_parent_label": parent.provider_label},
        )
        _bump(outcome.counts, _upsert_industry_snapshot(conn, payload, run_id))
    return outcome


def _constituent_coverage(wide_close: pd.DataFrame | None, universe_size: Any, as_of: date) -> dict[str, Any]:
    """Denominators for the dispersion summary: how many constituents actually have current prices."""
    out: dict[str, Any] = {"constituents_with_prices": None, "stale_constituents": None, "price_sessions": None, "denominator_status": "UNAVAILABLE"}
    if not isinstance(wide_close, pd.DataFrame) or wide_close.empty:
        return out
    try:
        idx = pd.to_datetime(pd.Series(wide_close.index), errors="coerce")
    except (TypeError, ValueError):
        return out
    if idx.isna().all():
        return out
    numeric = wide_close.apply(pd.to_numeric, errors="coerce")
    numeric.index = idx.values
    numeric = numeric.sort_index()
    last_valid = numeric.apply(lambda col: col.last_valid_index())
    with_prices = int(last_valid.notna().sum())
    stale = int(sum(1 for ts in last_valid.dropna() if pd.Timestamp(ts).date() < as_of))
    out.update({"constituents_with_prices": with_prices, "stale_constituents": stale, "price_sessions": len(numeric.index)})
    try:
        size = int(universe_size) if universe_size is not None else None
    except (TypeError, ValueError):
        size = None
    if size is None or size <= 0:
        out["denominator_status"] = "UNIVERSE_SIZE_UNAVAILABLE"
    elif with_prices - stale >= size:
        out["denominator_status"] = COVERAGE_FULL
    else:
        out["denominator_status"] = COVERAGE_PARTIAL
        out["current_price_fraction"] = (with_prices - stale) / size
    return out


def ingest_dispersion_bundle(conn, bundle: BundleRead, *, parent: SectorResolution, run_id: str | None) -> list[BundleOutcome]:
    outcomes: list[BundleOutcome] = []
    summary = bundle.jsons.get("summary")
    sector_outcome = BundleOutcome(str(bundle.path), "CONSTITUENT_DISPERSION", RUN_SUCCEEDED, bundle.as_of.isoformat())
    if not isinstance(summary, dict) or not summary:
        sector_outcome.status = RUN_FAILED
        sector_outcome.reason = "summary.json missing or empty"
        outcomes.append(sector_outcome)
        return outcomes
    breadth = bundle.tables.get("breadth_table")
    coverage: dict[str, Any] = {"universe_size": summary.get("universe_size")}
    if isinstance(breadth, pd.DataFrame) and not breadth.empty:
        first = breadth.iloc[0].to_dict()
        for key in ("count_valid_50dma", "count_valid_200dma", "count_above_50dma", "count_above_200dma"):
            coverage[key] = first.get(key)
    coverage.update(_constituent_coverage(bundle.frames.get("wide_close"), summary.get("universe_size"), bundle.as_of))
    coverage.update({"universe_method": "FMP_PROFILE_BULK_CURRENT_UNIVERSE", "weights": "CURRENT_MARKET_CAP", "membership": "CURRENT", "pit": False})
    payload = _snapshot_payload(
        source_id=LEGACY_SOURCE_ID,
        dataset="CONSTITUENT_DISPERSION",
        entity_kind=parent.entity_kind,
        sector_key=parent.sector_key,
        canonical_sector=parent.canonical_sector,
        provider_label=parent.provider_label,
        instrument_id=None,
        as_of=bundle.as_of.isoformat(),
        schema_version=SCHEMA_VERSION,
        methodology_version=METHODOLOGY_VERSION,
        benchmark=None,
        return_basis="constituent_adjClose_returns",
        value_basis="fraction",
        universe_method="CURRENT_UNIVERSE_CONTEXT_ONLY",
        research_eligible=False,
        metrics={k: v for k, v in normalize_payload(summary).items() if k != "universe_size"},
        coverage=coverage,
        provenance=bundle.provenance,
        source_refs={"provider": "FMP", "bundle": "dispersion", "universe": "profile-bulk"},
    )
    _bump(sector_outcome.counts, _upsert_sector_snapshot(conn, payload, run_id))
    outcomes.append(sector_outcome)

    participation = bundle.tables.get("industry_participation")
    ind_outcome = BundleOutcome(str(bundle.path), "INDUSTRY_PARTICIPATION", RUN_SUCCEEDED, bundle.as_of.isoformat())
    if isinstance(participation, pd.DataFrame) and not participation.empty and "industry" in participation.columns:
        for row in participation.to_dict(orient="records"):
            industry = str(row.get("industry") or "")
            if not industry:
                _bump(ind_outcome.counts, "rejected")
                continue
            metrics = normalize_payload({k: v for k, v in row.items() if k != "industry"})
            ind_payload = _snapshot_payload(
                source_id=LEGACY_SOURCE_ID,
                dataset="INDUSTRY_PARTICIPATION",
                parent_sector_key=parent.sector_key,
                industry_key=industry,
                instrument_id=None,
                as_of=bundle.as_of.isoformat(),
                schema_version=SCHEMA_VERSION,
                methodology_version=METHODOLOGY_VERSION,
                benchmark=None,
                return_basis="constituent_adjClose_1m_return_ew_vs_current_cw",
                value_basis="fraction",
                metrics=metrics,
                provenance={**bundle.provenance, "universe_method": "CURRENT_UNIVERSE_CONTEXT_ONLY"},
            )
            _bump(ind_outcome.counts, _upsert_industry_snapshot(conn, ind_payload, run_id))
    else:
        ind_outcome.status = RUN_SKIPPED
        ind_outcome.reason = "industry_participation table absent"
    outcomes.append(ind_outcome)
    return outcomes


@dataclass
class LegacyIngestReport:
    root: str
    outcomes: list[BundleOutcome] = field(default_factory=list)
    quarantined: list[dict[str, str]] = field(default_factory=list)

    @property
    def failed_bundles(self) -> list[BundleOutcome]:
        return [o for o in self.outcomes if o.status == RUN_FAILED]

    @property
    def succeeded_bundles(self) -> list[BundleOutcome]:
        return [o for o in self.outcomes if o.status == RUN_SUCCEEDED]

    @property
    def transport_status(self) -> str:
        """OK only when every discovered bundle was accepted; any quarantine/failure is PARTIAL (or FAILED)."""
        degraded = bool(self.failed_bundles or self.quarantined)
        if not self.succeeded_bundles:
            return TRANSPORT_FAILED
        return TRANSPORT_PARTIAL if degraded else TRANSPORT_OK

    @property
    def stale_instruments(self) -> int:
        return sum(o.counts.get("stale_instruments", 0) for o in self.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": LEGACY_SOURCE_ID,
            "root": self.root,
            "methodology_version": METHODOLOGY_VERSION,
            "transport_status": self.transport_status,
            "bundles_processed": len(self.outcomes),
            "bundles_failed": len(self.failed_bundles),
            "bundles_quarantined": len(self.quarantined),
            "stale_instruments": self.stale_instruments,
            "quarantined": list(self.quarantined),
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


def discover_bundles(root: Path) -> list[tuple[str, Path, dict[str, Any]]]:
    """Return ``(kind, path, context)`` for every bundle directory present under ``root``."""
    root = Path(root)
    found: list[tuple[str, Path, dict[str, Any]]] = []
    for spy_dir in (root / "spy", root / "spy_sector_rotation"):
        if (spy_dir / "bundle_meta.json").is_file():
            found.append(("spy", spy_dir, {}))
            break
    for rot_root in (root / "rotation", root / "sector_rotation"):
        if rot_root.is_dir():
            for sub in sorted(p for p in rot_root.iterdir() if p.is_dir()):
                if (sub / "bundle_meta.json").is_file():
                    found.append(("rotation", sub, {"label": slug_to_label(sub.name)}))
            break
    for dir_name, theme_label in THEME_DIRS.items():
        for cand in (root / dir_name, root / "{0}_rotation".format(dir_name)):
            if (cand / "bundle_meta.json").is_file():
                found.append(("theme", cand, {"label": theme_label, "benchmark": THEME_BENCHMARKS[dir_name]}))
                break
    disp_root = root / "dispersion"
    if disp_root.is_dir():
        for sub in sorted(p for p in disp_root.iterdir() if p.is_dir()):
            if (sub / "bundle_meta.json").is_file():
                found.append(("dispersion", sub, {"label": slug_to_label(sub.name)}))
    return found


def ingest_precomputed_root(engine, root: str | os.PathLike, *, parent_run_id: str | None = None, today: date | None = None) -> LegacyIngestReport:
    root_path = Path(root)
    report = LegacyIngestReport(str(root_path))
    bundles = discover_bundles(root_path)
    latest_as_of: date | None = None
    for kind, path, ctx in bundles:
        with engine.begin() as conn:
            run_id = start_run(conn, source_id=LEGACY_SOURCE_ID, dataset="bundle:{0}".format(path.relative_to(root_path)), parent_run_id=parent_run_id)
        try:
            bundle = read_bundle(path)
        except BundleQuarantined as exc:
            report.quarantined.append({"bundle": str(path), "reason": str(exc)})
            with engine.begin() as conn:
                finish_run(conn, run_id, status=RUN_SKIPPED, error_redacted=str(exc)[:300], details={"quarantined": True})
            continue
        try:
            with engine.begin() as conn:
                if kind == "spy":
                    outcomes = [ingest_spy_bundle(conn, bundle, run_id=run_id)]
                elif kind == "rotation":
                    parent = resolve_provider_sector(ctx["label"])
                    if parent.quarantined:
                        raise BundleQuarantined("unknown sector label {0!r}".format(ctx["label"]))
                    benchmark = SECTOR_ETF_PROXY.get(parent.canonical_sector or "")
                    outcomes = [ingest_rotation_bundle(conn, bundle, parent=parent, benchmark=benchmark, dataset="INDUSTRY_RS_VS_SECTOR_ETF", run_id=run_id)]
                elif kind == "theme":
                    parent = resolve_provider_sector(ctx["label"])
                    outcomes = [ingest_rotation_bundle(conn, bundle, parent=parent, benchmark=ctx.get("benchmark"), dataset="THEME_RS", run_id=run_id)]
                else:
                    parent = resolve_provider_sector(ctx["label"])
                    if parent.quarantined:
                        raise BundleQuarantined("unknown sector label {0!r}".format(ctx["label"]))
                    outcomes = ingest_dispersion_bundle(conn, bundle, parent=parent, run_id=run_id)
                if any(o.status == RUN_FAILED for o in outcomes):
                    raise BundleQuarantined("; ".join(o.reason or "failed" for o in outcomes if o.status == RUN_FAILED))
                totals: dict[str, int] = {}
                for o in outcomes:
                    for k, v in o.counts.items():
                        totals[k] = totals.get(k, 0) + v
                finish_run(conn, run_id, status=RUN_SUCCEEDED, counts={"received": sum(totals.values()), "inserted": totals.get("inserted", 0), "revised": totals.get("revised", 0), "unchanged": totals.get("unchanged", 0), "rejected": totals.get("rejected", 0) + totals.get("quarantined", 0)}, details={"as_of": bundle.as_of.isoformat(), "outcomes": [o.as_dict() for o in outcomes]})
            report.outcomes.extend(outcomes)
            latest_as_of = bundle.as_of if latest_as_of is None else max(latest_as_of, bundle.as_of)
        except BundleQuarantined as exc:
            report.quarantined.append({"bundle": str(path), "reason": str(exc)})
            report.outcomes.append(BundleOutcome(str(path), kind, RUN_FAILED, bundle.as_of.isoformat(), reason=str(exc)))
            with engine.begin() as conn:
                finish_run(conn, run_id, status=RUN_FAILED, error_redacted=str(exc)[:300])
        except Exception as exc:  # noqa: BLE001 - isolate bundle failures
            logger.exception("legacy bundle %s failed", path)
            report.outcomes.append(BundleOutcome(str(path), kind, RUN_FAILED, bundle.as_of.isoformat(), reason=exc.__class__.__name__))
            with engine.begin() as conn:
                finish_run(conn, run_id, status=RUN_FAILED, error_redacted=exc.__class__.__name__)
    with engine.begin() as conn:
        transport = report.transport_status
        # "success" (last_success_at) means the whole root was accepted; a quarantined bundle
        # must surface in health even when the other bundles loaded fine.
        success = transport == TRANSPORT_OK
        if success:
            error = None
        elif report.failed_bundles or report.quarantined:
            error = "{0} bundle(s) failed/quarantined: {1}".format(
                len(report.failed_bundles) + len(report.quarantined),
                "; ".join(sorted({Path(q["bundle"]).name for q in report.quarantined} | {Path(o.bundle).name for o in report.failed_bundles}))[:200],
            )
        else:
            error = "no bundles found"
        record_freshness(
            conn,
            source_id=LEGACY_SOURCE_ID,
            dataset="precomputed_sector_bundles",
            cadence="D",
            transport_status=transport,
            latest_observation=latest_as_of,
            success=success,
            error_redacted=error,
            run_id=parent_run_id,
            today=today,
        )
    return report


__all__ = [
    "BundleOutcome",
    "BundleQuarantined",
    "BundleRead",
    "COVERAGE_FULL",
    "COVERAGE_INSUFFICIENT_HISTORY",
    "COVERAGE_PARTIAL",
    "LEGACY_SOURCE_ID",
    "LegacyIngestReport",
    "MAX_MISSING_FRACTION",
    "METHODOLOGY_VERSION",
    "PRICE_METRIC_KEYS",
    "PRICE_STALE",
    "RS_COLUMNS",
    "SCHEMA_VERSION",
    "TRANSPORT_PARTIAL",
    "directory_fingerprint",
    "discover_bundles",
    "etf_metrics_from_prices",
    "ingest_dispersion_bundle",
    "ingest_precomputed_root",
    "ingest_rotation_bundle",
    "ingest_spy_bundle",
    "read_bundle",
    "snapshot_body_from_row",
    "verify_stored_snapshot",
]
