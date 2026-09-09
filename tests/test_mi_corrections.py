"""Regression tests for the independent-review findings (export, NULL revisions, section health)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from market_intelligence import morning_context
from market_intelligence.analytics import STATUS_WITHDRAWN, build_analytics, series_metrics
from market_intelligence.catalog import CATALOG_BY_ID
from market_intelligence.morning_context import section_status
from market_intelligence.store import ObservationInput, upsert_observations


def test_has_value_rejects_metadata_only_and_accepts_zero():
    """Finding D1: labels/status/units must not satisfy an available numerical metric."""
    assert morning_context._has_value({"metric_id": "x.y", "value": None, "status": "INSUFFICIENT_DATA", "units": "bps"}) is False
    assert morning_context._has_value({"metric_id": "x.y", "value": 0, "status": "OK", "units": "bps"}) is True
    assert morning_context._has_value({"tenor": "10Y", "yield_pct": None, "observation_date": "2024-01-02"}) is False
    assert morning_context._has_value({"curve": [{"tenor": "10Y", "yield_pct": None}], "slopes": {"10Y2Y": {"value": None, "status": "INSUFFICIENT_DATA", "units": "bps"}}}) is False
    assert morning_context._has_value({"latest": {"value": 0.0, "units": "pct"}}) is True


def test_section_status_does_not_let_one_fresh_series_conceal_a_stale_required_series():
    """Finding D2: newest date in the section must not hide a stale required input."""
    capture = date(2025, 1, 2)
    data = {
        "categories": {
            "inflation": [
                {"series_id": "CPIAUCSL", "export_scope": "ATTRIBUTION_REQUIRED", "latest": {"value": 300.0, "observation_date": "2024-12-01", "units": "index"}},
                {"series_id": "PCEPI", "export_scope": "ATTRIBUTION_REQUIRED", "latest": {"value": 120.0, "observation_date": "2023-01-01", "units": "index"}},
            ]
        }
    }
    sec = section_status("macro", data, required=["categories"], capture_date=capture, empty_reason="empty")
    assert "PCEPI" in sec["stale_required"]
    assert "CPIAUCSL" not in sec["stale_required"]
    assert sec["status"] in {morning_context.SECTION_STALE, morning_context.SECTION_PARTIAL}
    assert sec["captured_freshness"]["cadence"] == "M"
    missing = section_status(
        "macro",
        {"categories": {"inflation": [{"series_id": "CPIAUCSL", "latest": {"value": 300.0, "observation_date": "2024-12-01"}}]}},
        required=["categories"],
        capture_date=capture,
        empty_reason="empty",
    )
    assert "PAYEMS" in missing["missing_required"]
    zero = section_status(
        "rates",
        {"curve": [{"series_id": "DGS10", "tenor": "10Y", "yield_pct": 0.0, "observation_date": "2024-12-31"}], "slopes": {"10Y2Y": {"value": 0.0, "units": "bps"}}},
        required=["curve", "slopes"],
        capture_date=capture,
        empty_reason="empty",
    )
    assert zero["status"] != morning_context.SECTION_UNAVAILABLE
    assert "DGS10" not in zero["missing_required"]


def test_section_status_marks_stale_sector_as_of_without_calling_it_current():
    capture = date(2025, 1, 2)
    data = {
        "datasets": {
            "ETF_RS_VS_SPY": [
                {"sector_key": "XLK", "as_of": "2024-06-01", "metrics": {"rs_chg_1m": 0.01}},
            ]
        }
    }
    sec = section_status("sectors", data, required=["datasets"], capture_date=capture, empty_reason="empty")
    assert sec["status"] == morning_context.SECTION_STALE
    assert sec["captured_freshness"]["status"] == "STALE"


@pytest.mark.usefixtures("pg_engine")
def test_null_revision_invalidates_derived_metrics_and_recovers(pg_engine):
    """Finding C: a valid→NULL revision must unpublish derived rows; a later valid revision restores them."""
    from market_intelligence.store import finish_run, start_run, upsert_macro_series, upsert_source_registry

    sid = "DGS10"
    spec = CATALOG_BY_ID[sid]
    d1, d2, d3 = date(2024, 12, 27), date(2024, 12, 30), date(2024, 12, 31)
    retrieved = datetime(2024, 12, 31, 16, 0, tzinfo=timezone.utc)
    with pg_engine.begin() as conn:
        upsert_source_registry(conn, enabled={"FRED": True}, access={"FRED": "CONFIGURED"})
        upsert_macro_series(
            conn,
            series_id=sid,
            source_id="FRED",
            provider_series_id=sid,
            spec_fields={"category": "rates", "subcategory": "nominal_curve", "catalog_version": "fred_catalog_v2", "source_url": spec.source_url, "notes": "", "export_scope": spec.export_scope, "expected_frequency": "D"},
            meta={"id": sid, "title": "10Y", "units": "Percent", "frequency_short": "D", "seasonal_adjustment_short": "NSA"},
            metadata_status="VALIDATED",
            mismatches=[],
        )
        upsert_observations(
            conn,
            series_id=sid,
            rows=[
                ObservationInput(d1, "4.10", date(2024, 1, 1), date(9999, 12, 31)),
                ObservationInput(d2, "4.20", date(2024, 1, 1), date(9999, 12, 31)),
                ObservationInput(d3, "4.30", date(2024, 1, 1), date(9999, 12, 31)),
            ],
            retrieved_at=retrieved,
            run_id=None,
            today=d3,
        )
        rid = start_run(conn, source_id="ANALYTICS", dataset="metric_snapshots")
        first = build_analytics(conn, as_of=d3, run_id=rid, history_start=d1, series_ids=[sid, "DGS2"])
        finish_run(conn, rid, status="SUCCEEDED", details=first.as_dict())
        level_before = conn.execute(
            text("SELECT as_of, value, status FROM mi_v_metric_latest WHERE metric_id = 'DGS10.level'")
        ).mappings().one()
        hist_before = conn.execute(
            text("SELECT COUNT(*) FROM mi_metric_snapshots WHERE metric_id = 'DGS10.level' AND as_of = :d AND value IS NOT NULL"),
            {"d": d3},
        ).scalar()
    assert level_before["as_of"] == d3 and float(level_before["value"]) == pytest.approx(4.30)
    assert hist_before == 1

    since = retrieved
    with pg_engine.begin() as conn:
        upsert_observations(
            conn,
            series_id=sid,
            rows=[ObservationInput(d3, ".", date(2024, 1, 1), date(9999, 12, 31))],
            retrieved_at=datetime.now(timezone.utc),
            run_id=None,
            today=d3,
        )
        rid = start_run(conn, source_id="ANALYTICS", dataset="metric_snapshots")
        withdrawn = build_analytics(conn, as_of=d3, run_id=rid, since=since, series_ids=[sid, "DGS2"])
        finish_run(conn, rid, status="SUCCEEDED", details=withdrawn.as_dict())
        latest = conn.execute(
            text("SELECT as_of, value, status FROM mi_v_metric_latest WHERE metric_id = 'DGS10.level'")
        ).mappings().one()
        hist = conn.execute(
            text("SELECT value, status FROM mi_v_metric_history WHERE metric_id = 'DGS10.level' AND as_of = :d"),
            {"d": d3},
        ).mappings().one()
        raw_revs = conn.execute(
            text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id = :s AND observation_date = :d"),
            {"s": sid, "d": d3},
        ).scalar()
        current_null = conn.execute(
            text("SELECT value FROM mi_macro_observations WHERE series_id = :s AND observation_date = :d AND is_current"),
            {"s": sid, "d": d3},
        ).scalar()
        other = conn.execute(
            text("SELECT value FROM mi_macro_observations WHERE series_id = :s AND observation_date = :d AND is_current"),
            {"s": sid, "d": d2},
        ).scalar()
    assert raw_revs == 2 and current_null is None
    assert float(other) == pytest.approx(4.20)
    assert hist["status"] == STATUS_WITHDRAWN and hist["value"] is None
    assert latest["as_of"] == d2 and float(latest["value"]) == pytest.approx(4.20)

    with pg_engine.begin() as conn:
        upsert_observations(
            conn,
            series_id=sid,
            rows=[ObservationInput(d3, "4.35", date(2024, 1, 1), date(9999, 12, 31))],
            retrieved_at=datetime.now(timezone.utc),
            run_id=None,
            today=d3,
        )
        rid = start_run(conn, source_id="ANALYTICS", dataset="metric_snapshots")
        restored = build_analytics(conn, as_of=d3, run_id=rid, since=since, series_ids=[sid, "DGS2"])
        finish_run(conn, rid, status="SUCCEEDED", details=restored.as_dict())
        latest2 = conn.execute(
            text("SELECT as_of, value, status FROM mi_v_metric_latest WHERE metric_id = 'DGS10.level'")
        ).mappings().one()
        again = build_analytics(conn, as_of=d3, history_start=d1, series_ids=[sid, "DGS2"])
        latest3 = conn.execute(
            text("SELECT as_of, value FROM mi_v_metric_latest WHERE metric_id = 'DGS10.level'")
        ).mappings().one()
    assert latest2["as_of"] == d3 and float(latest2["value"]) == pytest.approx(4.35)
    assert latest3["as_of"] == latest2["as_of"] and float(latest3["value"]) == float(latest2["value"])
    assert again.metrics_written > 0


def test_withdrawn_series_metrics_cover_every_declared_transform():
    spec = CATALOG_BY_ID["CPIAUCSL"]
    rows = {r.metric_id: r for r in series_metrics(spec, {date(2024, 12, 1): None}, at=date(2024, 12, 1))}
    assert rows["CPIAUCSL.level"].result.status == STATUS_WITHDRAWN
    assert rows["CPIAUCSL.yoy_pct"].result.value is None
    assert rows["CPIAUCSL.ann3m_pct"].result.status == STATUS_WITHDRAWN
