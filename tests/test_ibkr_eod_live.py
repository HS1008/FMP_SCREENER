"""Opt-in, bounded, read-only live IBKR historical test.

Skipped unless IBKR_LIVE_EOD=1 and a localhost TWS session is accepting sockets.
Never posts orders. Never changes TWS settings.
"""

from __future__ import annotations

import os

import pytest

from ibkr_collector.historical import (
    PHASE3_SYMBOLS,
    STATUS_OK,
    WHAT_TO_SHOW_ADJUSTED,
    connect_historical_session,
    coverage_summary,
    fetch_universe,
)
from ibkr_collector.live_eod_validate import LOCAL_TWS_PORTS, detect_local_tws_port

LIVE_ENABLED = os.environ.get("IBKR_LIVE_EOD", "").strip() == "1"


@pytest.mark.skipif(not LIVE_ENABLED, reason="IBKR_LIVE_EOD not set; live TWS test skipped")
def test_bounded_live_adjusted_last_daily_bars():
    detected = detect_local_tws_port()
    port = detected.get("port")
    if not port:
        pytest.skip("no localhost TWS on {0}".format(",".join(str(p) for p in LOCAL_TWS_PORTS)))
    session, client, error = connect_historical_session(host="127.0.0.1", port=port, client_id=72)
    if session is None:
        pytest.skip("TWS session unavailable: {0}".format(error))
    try:
        results = fetch_universe(session, list(PHASE3_SYMBOLS), duration="2 Y", what_to_show=WHAT_TO_SHOW_ADJUSTED)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    summary = coverage_summary(results)
    assert summary["requested"] == len(PHASE3_SYMBOLS)
    ok = [row for row in results if row.status == STATUS_OK]
    assert ok, summary
    for row in ok:
        assert row.qualified is not None
        assert row.qualified.con_id > 0
        assert row.what_to_show == WHAT_TO_SHOW_ADJUSTED
        assert len(row.bars) >= 60
        dates = [b.bar_date for b in row.bars]
        assert dates == sorted(dates)
        assert all(b.close is not None for b in row.bars)
