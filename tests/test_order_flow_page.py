"""Order Flow coverage table must remain pyarrow-safe with mixed HTTP/nulls."""

from __future__ import annotations

import pandas as pd
import pytest

from market_intelligence.pages_ui import display_cell, order_flow_coverage_frame


def test_display_cell_stringifies_numbers_and_missing():
    assert display_cell(200) == "200"
    assert display_cell(0) == "0"
    assert display_cell(None) == "—"
    assert display_cell("") == "—"


def test_coverage_frame_mixed_http_converts_to_pyarrow():
    pyarrow = pytest.importorskip("pyarrow")
    frame = order_flow_coverage_frame(
        [
            {
                "dataset": "corporateMarketBreadth",
                "group_name": "fixedIncomeMarket",
                "capability_status": "AVAILABLE",
                "http_status": 200,
                "probe_record_count": 5,
                "ingest_latest_observation_date": "2026-09-08",
                "ingest_last_success_at": "2026-09-09T07:40:49Z",
                "last_probe_at": "2026-09-09T07:40:49Z",
                "transport_status": "OK",
                "freshness_status": "FRESH",
                "dataset_cadence": "D",
                "source_access_status": "CONFIGURED",
            },
            {
                "dataset": "TRACE_INDIVIDUAL_TRANSACTIONS",
                "group_name": "fixedIncomeMarket",
                "capability_status": "ENTITLEMENT_REQUIRED",
                "http_status": None,
                "probe_record_count": 0,
                "ingest_latest_observation_date": None,
                "probe_latest_observation_date": None,
                "ingest_last_success_at": None,
                "last_probe_at": None,
                "transport_status": "SKIPPED",
                "freshness_status": None,
                "dataset_cadence": "INTRADAY",
                "source_access_status": "ENTITLEMENT_REQUIRED",
            },
        ]
    )
    assert list(frame["HTTP"]) == ["200", "—"]
    assert list(frame["Probe records"]) == ["5", "0"]
    pyarrow.Table.from_pandas(frame)
    mixed = pd.DataFrame([{"HTTP": 200}, {"HTTP": "—"}])
    with pytest.raises(Exception):
        pyarrow.Table.from_pandas(mixed)
