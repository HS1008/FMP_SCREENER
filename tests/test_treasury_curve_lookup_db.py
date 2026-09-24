"""Stored observations: complete curves only, same date, Treasury preferred."""

from datetime import date, datetime, timezone

import pytest

from market_intelligence.curve_compare import COMPARE_PRIOR, COMPARE_WEEK, comparison_target
from market_intelligence.treasury_xml import COMPLETE_NOMINAL_TENORS
from market_intelligence.catalog import CURVE_TENORS

CURRENT = date(2026, 9, 23)
WEEK = date(2026, 9, 16)
FRIDAY = date(2026, 9, 18)
INCOMPLETE = date(2026, 9, 21)
EXACT = date(2026, 6, 15)


def _ust(tenor: str) -> str:
    return "UST_NOM_{0}".format(tenor)


def _seed(conn, day: date, *, source_id: str, series_for_tenor, skip_tenors=()) -> None:
    from market_intelligence import store

    for tenor in COMPLETE_NOMINAL_TENORS:
        if tenor in skip_tenors:
            continue
        series_id = series_for_tenor(tenor)
        store.upsert_macro_series(
            conn,
            series_id=series_id,
            source_id=source_id,
            provider_series_id=series_id,
            spec_fields={
                "category": "rates",
                "subcategory": "nominal_curve",
                "catalog_version": "test",
                "source_url": "https://example.test/{0}".format(series_id),
                "notes": "",
                "export_scope": "ATTRIBUTION_REQUIRED",
                "expected_frequency": "D",
            },
            meta={"title": series_id, "units": "Percent", "frequency_short": "D", "seasonal_adjustment_short": "NSA"},
            metadata_status="VALIDATED",
            mismatches=None,
        )
        value = "4.00" if source_id == "TREASURY" else "9.99"
        store.upsert_observations(
            conn,
            series_id=series_id,
            rows=[store.ObservationInput(day, value)],
            retrieved_at=datetime(2026, 9, 23, 20, tzinfo=timezone.utc),
            run_id=None,
            today=CURRENT,
        )


@pytest.fixture
def seeded(mi_db):
    from market_intelligence import store

    with mi_db.begin() as conn:
        store.upsert_source_registry(conn)
        store.upsert_source_registry(
            conn,
            [
                {
                    "source_id": "TREASURY",
                    "provider": "U.S. Department of the Treasury",
                    "dataset": "daily_treasury_yield_curve_xml",
                    "source_url": "https://home.treasury.gov/treasury-daily-interest-rate-xml-feed",
                    "expected_cadence": "D",
                    "usage_scope": "ATTRIBUTION_REQUIRED",
                    "attribution": "U.S. Treasury",
                    "terms_notes": "test",
                    "units_metadata": {"yield": "percent"},
                }
            ],
        )
        for day in (EXACT, WEEK, FRIDAY, CURRENT):
            _seed(conn, day, source_id="TREASURY", series_for_tenor=_ust)
        # FRED equivalents on the Friday print must lose to Treasury on the same date.
        _seed(conn, FRIDAY, source_id="FRED", series_for_tenor=lambda tenor: CURVE_TENORS[tenor])
        # Incomplete Monday: every tenor except 30Y. Must not be selected.
        _seed(conn, INCOMPLETE, source_id="TREASURY", series_for_tenor=_ust, skip_tenors={"30Y"})
    return mi_db


def _lookup(engine, target: date):
    from market_intelligence.read_models import complete_treasury_curve_on_or_before

    with engine.connect() as conn:
        return complete_treasury_curve_on_or_before(conn, target.isoformat(), CURRENT.isoformat())


def test_exact_weekend_holiday_and_history_gap(seeded):
    exact = _lookup(seeded, EXACT)
    assert exact["found"] is True
    assert exact["effective_date"] == EXACT.isoformat()
    assert exact["fallback"] is False
    assert {row["observation_date"] for row in exact["curve"]} == {EXACT.isoformat()}
    assert [row["tenor"] for row in exact["curve"]] == list(COMPLETE_NOMINAL_TENORS)

    sunday = _lookup(seeded, date(2026, 9, 20))
    assert sunday["found"] is True
    assert sunday["requested_date"] == "2026-09-20"
    assert sunday["effective_date"] == FRIDAY.isoformat()
    assert sunday["fallback"] is True
    assert {row["source_id"] for row in sunday["curve"]} == {"TREASURY"}
    assert all(float(row["yield_pct"]) == 4.0 for row in sunday["curve"])

    holiday = _lookup(seeded, INCOMPLETE)
    assert holiday["effective_date"] == FRIDAY.isoformat()
    assert holiday["effective_date"] != INCOMPLETE.isoformat()

    missing = _lookup(seeded, date(2020, 1, 2))
    assert missing["found"] is False
    assert missing["curve"] == []

    future = _lookup(seeded, date(2026, 9, 24))
    assert future["found"] is False
    assert future["reason"] == "requested_after_current_curve"


def test_quick_comparisons_use_complete_curves(seeded):
    prior_target = comparison_target(COMPARE_PRIOR, CURRENT)
    week_target = comparison_target(COMPARE_WEEK, CURRENT)
    prior = _lookup(seeded, prior_target)
    week = _lookup(seeded, week_target)
    assert prior["effective_date"] == FRIDAY.isoformat()
    assert prior["effective_date"] <= CURRENT.isoformat()
    assert week["effective_date"] == WEEK.isoformat()
    assert week["fallback"] is False
    assert week["effective_date"] <= CURRENT.isoformat()
