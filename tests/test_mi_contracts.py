"""Pure (no-DB) Market Intelligence contracts: normalization, hashing, transforms, freshness,
sector mapping, export policy, catalog invariants.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from market_intelligence import catalog, export_policy, freshness, nulls, sector_mapping, transforms
from market_intelligence.fred_client import FRED_REALTIME_END_SENTINEL, parse_fred_date


# ---- nulls / strict JSON / hashing ----------------------------------------------------------

def test_missing_tokens_become_null_and_zero_is_valid():
    assert nulls.normalize_numeric(".") == (None, "missing_token:.")
    assert nulls.normalize_numeric("") == (None, "missing_token:<empty>")
    assert nulls.normalize_numeric(float("nan")) == (None, "non_finite")
    assert nulls.normalize_numeric(float("inf")) == (None, "non_finite")
    assert nulls.normalize_numeric("0") == (Decimal("0"), None)
    assert nulls.normalize_numeric(0) == (Decimal(0), None)
    assert nulls.normalize_numeric("4.35") == (Decimal("4.35"), None)
    assert nulls.normalize_numeric("1,234.5") == (Decimal("1234.5"), None)


def test_malformed_values_are_rejected_not_zeroed():
    with pytest.raises(nulls.MalformedValueError):
        nulls.normalize_numeric("abc")
    with pytest.raises(nulls.MalformedValueError):
        nulls.normalize_numeric(True)


def test_strict_json_has_no_nan_nat_or_infinity():
    payload = {"a": float("nan"), "b": pd.NaT, "c": float("inf"), "d": pd.Timestamp("2024-01-02"), "e": Decimal("1.50"), "f": date(2024, 1, 3), "g": [1.0, float("nan")]}
    text = nulls.strict_dumps(payload)
    assert "NaN" not in text and "Infinity" not in text and "NaT" not in text
    loaded = nulls.strict_loads(text)
    assert loaded == {"a": None, "b": None, "c": None, "d": "2024-01-02T00:00:00", "e": 1.5, "f": "2024-01-03", "g": [1.0, None]}
    with pytest.raises(ValueError):
        nulls.strict_loads('{"x": NaN}')


def test_canonical_hash_excludes_only_top_level_artifact_hash_and_preserves_order():
    body = {"features": ["MOM_12_1", "MOM_6_1", "RET_3M"], "nested": {"artifact_sha256": "kept", "z": 1, "a": 2}}
    stamped = nulls.with_artifact_hash(body)
    ok, actual = nulls.verify_artifact_hash(stamped)
    assert ok and actual == stamped["artifact_sha256"]
    # Golden: producer/consumer share this exact digest for this exact body.
    assert stamped["artifact_sha256"] == nulls.canonical_sha256(body)
    reordered = {"features": ["MOM_6_1", "MOM_12_1", "RET_3M"], "nested": body["nested"]}
    assert nulls.canonical_sha256(reordered) != stamped["artifact_sha256"]
    tampered = dict(stamped)
    tampered["nested"] = dict(tampered["nested"], z=2)
    ok, _ = nulls.verify_artifact_hash(tampered)
    assert not ok
    missing = dict(stamped)
    missing.pop("artifact_sha256")
    ok, _ = nulls.verify_artifact_hash(missing)
    assert not ok


def test_golden_hash_is_stable_across_nan_normalization():
    a = {"x": [1.0, None], "y": {"k": None}}
    b = {"x": [1.0, float("nan")], "y": {"k": pd.NaT}}
    assert nulls.canonical_sha256(a) == nulls.canonical_sha256(b)
    # Fixed golden value (shared with qc_research.object_store_sync) guards against serializer drift.
    assert nulls.canonical_sha256({"a": 1, "b": [1, 2]}) == "8baa73198470c7bb4c3ce142a8fd651affc0310d878bb9bd159e37a573fb4874"
    from qc_research.object_store_sync import sha256_payload

    assert sha256_payload({"a": 1, "b": [1, 2]}) == "8baa73198470c7bb4c3ce142a8fd651affc0310d878bb9bd159e37a573fb4874"


# ---- FRED dates -----------------------------------------------------------------------------

def test_fred_realtime_end_sentinel_is_a_plain_date_not_a_timestamp():
    parsed = parse_fred_date("9999-12-31")
    assert parsed == date(9999, 12, 31) and FRED_REALTIME_END_SENTINEL == "9999-12-31"
    assert isinstance(parsed, date) and not isinstance(parsed, pd.Timestamp)
    with pytest.raises((ValueError, OverflowError, pd.errors.OutOfBoundsDatetime)):
        pd.Timestamp(parsed).as_unit("ns")  # nanosecond timestamps cannot hold year 9999; the pipeline keeps DATEs


# ---- transforms -----------------------------------------------------------------------------

def _monthly(values: dict[str, float]) -> dict[date, float]:
    return {date.fromisoformat(k): v for k, v in values.items()}


def test_inflation_transforms_reference_cases():
    obs = _monthly({"2023-12-01": 300.0, "2024-06-01": 306.0, "2024-09-01": 309.0, "2024-11-01": 311.0, "2024-12-01": 312.0})
    yoy = transforms.yoy_pct(obs, date(2024, 12, 1))
    assert yoy.value == pytest.approx(4.0)
    assert yoy.detail["lag_date"] == "2023-12-01"
    ann3 = transforms.ann3m_pct(obs, date(2024, 12, 1))
    assert ann3.value == pytest.approx(100 * ((312.0 / 309.0) ** 4 - 1))
    ann6 = transforms.ann6m_pct(obs, date(2024, 12, 1))
    assert ann6.value == pytest.approx(100 * ((312.0 / 306.0) ** 2 - 1))
    mom = transforms.mom_pct(obs, date(2024, 12, 1))
    assert mom.value == pytest.approx(100 * (312.0 / 311.0 - 1))


def test_missing_month_yields_null_not_row_shifted_lag():
    # 2023-12 missing: YoY must NOT fall back to 2023-11 or any other month.
    obs = _monthly({"2023-11-01": 299.0, "2024-12-01": 312.0})
    yoy = transforms.yoy_pct(obs, date(2024, 12, 1))
    assert yoy.value is None and yoy.status == "INSUFFICIENT_DATA" and yoy.reason == "missing_lag_observation"
    assert yoy.detail["lag_date"] == "2023-12-01"


def test_quarterly_annualization_uses_quarter_convention():
    obs = {date(2024, 4, 1): 100.0, date(2024, 7, 1): 101.0}
    qoq = transforms.qoq_annualized_pct(obs, date(2024, 7, 1))
    assert qoq.value == pytest.approx(100 * (1.01 ** 4 - 1))


def test_bps_scaling_reference_cases():
    assert transforms.pct_to_bps(4.35 - 4.25) == pytest.approx(10.0)
    assert transforms.pct_to_bps(0.81) == pytest.approx(81.0)
    assert transforms.pct_to_bps(None) is None


def test_previous_observation_change_flags_multi_session_gap():
    obs = {date(2024, 12, 20): 4.25, date(2024, 12, 23): 4.35, date(2025, 1, 6): 4.40}
    one_session = transforms.previous_observation_change(obs, date(2024, 12, 23), units="bps", scale=100)
    assert one_session.value == pytest.approx(10.0) and one_session.status == "OK"
    assert one_session.detail["comparison_date"] == "2024-12-20"
    gap = transforms.previous_observation_change(obs, date(2025, 1, 6), units="bps", scale=100)
    assert gap.value == pytest.approx(5.0) and gap.status == "MULTI_SESSION_GAP"
    assert gap.detail["gap_days"] == 14


def test_calendar_change_reports_anchor_and_respects_allowed_lag():
    obs = {date(2024, 11, 1): 4.0, date(2024, 12, 2): 4.2}
    chg = transforms.calendar_change(obs, date(2024, 12, 2), months=1, cadence="D", units="bps", scale=100)
    assert chg.value == pytest.approx(20.0)
    assert chg.detail["anchor_date"] == "2024-11-02" and chg.detail["comparison_date"] == "2024-11-01"
    sparse = {date(2024, 9, 1): 4.0, date(2024, 12, 2): 4.2}
    out = transforms.calendar_change(sparse, date(2024, 12, 2), months=1, cadence="D", units="bps", scale=100)
    assert out.value is None and out.reason == "comparison_observation_outside_allowed_lag"


def test_curve_slope_requires_common_date_and_reports_missing_legs():
    d = date(2024, 12, 31)
    ten = {d: 4.5, d - timedelta(days=1): 4.4}
    two = {d - timedelta(days=1): 4.2}
    slope = transforms.curve_slope(ten, two, d)
    assert slope.value is None and slope.detail["missing_legs"] == ["short"]
    common, missing = transforms.common_curve_date({"DGS10": ten, "DGS2": two}, d)
    assert common == d - timedelta(days=1) and missing == []
    slope_common = transforms.curve_slope(ten, two, common)
    assert slope_common.value == pytest.approx(20.0)


def test_window_statistics_coverage_zero_variance_and_midrank():
    at = date(2024, 12, 31)
    flat = {at - timedelta(days=i): 1.0 for i in range(40)}
    stat = transforms.window_statistics(flat, at, window_days=365, min_observations=30, label="1Y")
    assert stat.value == pytest.approx(50.0)  # all ties -> midrank 50
    assert stat.detail["zscore"] is None and stat.detail["zscore_reason"] == "zero_variance"
    assert stat.detail["std_convention"] == "sample_ddof1"
    short = {at - timedelta(days=i): float(i) for i in range(10)}
    insufficient = transforms.window_statistics(short, at, window_days=365, min_observations=30, label="1Y")
    assert insufficient.value is None and insufficient.status == "INSUFFICIENT_HISTORY"
    assert insufficient.detail["observations"] == 10
    assert transforms.percentile_rank([1, 2, 3, 4], 4) == pytest.approx(87.5)


# ---- freshness --------------------------------------------------------------------------------

def test_us_federal_holidays_and_business_days():
    hol = freshness.us_federal_holidays(2024)
    assert date(2024, 1, 1) in hol and date(2024, 11, 28) in hol and date(2024, 12, 25) in hol
    assert date(2024, 7, 4) in hol
    assert not freshness.is_business_day(date(2024, 12, 25))
    # Fri 2024-12-20 -> Fri 2024-12-27: Mon 23, Tue 24, Thu 26, Fri 27 (Wed 25 holiday) = 4
    assert freshness.business_days_between(date(2024, 12, 20), date(2024, 12, 27)) == 4


def test_freshness_depends_on_cadence():
    today = date(2024, 12, 31)
    assert freshness.assess_freshness(date(2024, 12, 27), "D", today).status == "FRESH"
    assert freshness.assess_freshness(date(2024, 12, 13), "D", today).status == "STALE"
    assert freshness.assess_freshness(date(2024, 11, 1), "M", today).status == "FRESH"
    assert freshness.assess_freshness(date(2024, 9, 1), "M", today).status == "STALE"
    assert freshness.assess_freshness(date(2024, 7, 1), "Q", today).status == "FRESH"
    assert freshness.assess_freshness(date(2024, 12, 14), "W", today).status == "STALE"
    assert freshness.assess_freshness(None, "D", today).status == "UNKNOWN"


# ---- sector mapping --------------------------------------------------------------------------

def test_sector_mapping_versioned_and_quarantines_unknowns():
    assert sector_mapping.resolve_provider_sector("Consumer Cyclical").canonical_sector == "Consumer Discretionary"
    assert sector_mapping.resolve_provider_sector("Financial Services").canonical_sector == "Financials"
    assert sector_mapping.resolve_provider_sector("Healthcare").canonical_sector == "Health Care"
    assert sector_mapping.resolve_provider_sector("Basic Materials").canonical_sector == "Materials"
    assert sector_mapping.resolve_provider_sector("Consumer Defensive").canonical_sector == "Consumer Staples"
    ai = sector_mapping.resolve_provider_sector("AI")
    assert ai.entity_kind == "THEME" and ai.canonical_sector is None
    unknown = sector_mapping.resolve_provider_sector("Mystery Sector")
    assert unknown.entity_kind == "UNKNOWN" and unknown.quarantined
    assert len(sector_mapping.CANONICAL_SECTORS) == 11 and "AI" not in sector_mapping.CANONICAL_SECTORS


# ---- export policy ---------------------------------------------------------------------------

def test_export_policy_redacts_restricted_values_and_hashes_filtered_body():
    body = {
        "credit": {
            "buckets": [
                {"series_id": "BAMLC0A0CM", "label": "IG", "export_scope": catalog.EXPORT_RESTRICTED, "oas_bps": 81.0, "change_1d_bps": 2.0, "percentile": 40.0, "history": [1, 2], "as_of": "2024-12-31", "units": "bps"},
            ]
        },
        "rates": [{"series_id": "DGS10", "export_scope": catalog.EXPORT_ATTRIBUTION_REQUIRED, "yield_pct": 4.5}],
        "api_key": "should-never-leak",
    }
    envelope = export_policy.export_safe_payload(body, source_snapshot_hash="src" * 21 + "x")
    bucket = envelope["body"]["credit"]["buckets"][0]
    assert bucket["restricted"] is True and "oas_bps" not in bucket and "history" not in bucket and "percentile" not in bucket
    assert bucket["series_id"] == "BAMLC0A0CM" and bucket["as_of"] == "2024-12-31"
    assert envelope["body"]["rates"][0]["yield_pct"] == 4.5
    assert "api_key" not in envelope["body"]
    assert envelope["export_filtered"] is True and envelope["restricted_entries"] == 1
    assert envelope["source_snapshot_hash"] == "src" * 21 + "x"
    assert envelope["export_sha256"] != envelope["source_snapshot_hash"]
    assert export_policy.verify_export_hash(envelope)
    assert not export_policy.verify_export_hash(dict(envelope, restricted_entries=0))
    text = nulls.strict_dumps(envelope)
    assert "81.0" not in text and "should-never-leak" not in text


def test_export_policy_unfiltered_when_nothing_restricted():
    body = {"rates": [{"series_id": "DGS10", "export_scope": catalog.EXPORT_ATTRIBUTION_REQUIRED, "yield_pct": 4.5}]}
    envelope = export_policy.export_safe_payload(body, source_snapshot_hash=None)
    assert envelope["export_filtered"] is False and envelope["restricted_entries"] == 0
    assert envelope["body"] == body


# ---- catalog ----------------------------------------------------------------------------------

def test_catalog_contains_required_series_and_flags_ice_as_restricted():
    ids = {s.series_id for s in catalog.CATALOG}
    required = {
        "GDPC1", "INDPRO", "RSAFS", "PAYEMS", "UNRATE", "ICSA", "CCSA", "CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE",
        "DFF", "SOFR", "DGS3MO", "DGS6MO", "DGS1", "DGS2", "DGS3", "DGS5", "DGS7", "DGS10", "DGS20", "DGS30",
        "DFII5", "DFII10", "DFII20", "DFII30", "T5YIE", "T10YIE", "T5YIFR", "WALCL", "RRPONTSYD", "WTREGEN", "WRESBAL", "M2SL",
        "BAMLC0A0CM", "BAMLH0A0HYM2", "BAMLC0A1CAAA", "BAMLC0A2CAA", "BAMLC0A3CA", "BAMLC0A4CBBB", "BAMLH0A1HYBB", "BAMLH0A2HYB", "BAMLH0A3HYC",
    }
    assert required <= ids
    for spec in catalog.CATALOG:
        if spec.series_id.startswith("BAML"):
            assert spec.export_scope == catalog.EXPORT_RESTRICTED
            assert "Ice Data Indices" in spec.attribution
        assert spec.source_url == "https://fred.stlouisfed.org/series/{0}".format(spec.series_id)
    assert catalog.CATALOG_VERSION == "fred_catalog_v1"
    assert "not endorsed or certified by the Federal Reserve Bank of St. Louis" in catalog.FRED_ATTRIBUTION
    for sid in ("T5YIE", "T10YIE", "T5YIFR"):
        assert "survey" in catalog.CATALOG_BY_ID[sid].notes.lower()
