"""Opt-in live Cboe ingest into disposable PostgreSQL. CI skips this by default."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from market_intelligence.export_policy import filter_for_export
from market_intelligence.openbb_provider.client import OpenBBClient
from market_intelligence.openbb_provider.config import openbb_installed
from market_intelligence.openbb_provider.ingest import ingest_openbb
from market_intelligence.openbb_provider.normalize import normalize_chain
from market_intelligence.openbb_provider.store import publish_chain, publish_curve
from market_intelligence.openbb_provider.vix import normalize_curve
from market_intelligence.read_models import options_volatility_context

pytestmark = [
    pytest.mark.skipif(os.environ.get("OPENBB_LIVE_INGEST") != "1", reason="opt-in live Cboe ingest"),
    pytest.mark.skipif(not openbb_installed(), reason="requirements-openbb.txt not installed"),
]


def test_live_cboe_normalize_ingest_replay_and_readback(mi_db):
    client = OpenBBClient(retries=0, request_timeout_sec=45.0, operation_deadline_sec=90.0)
    counts = {}
    for symbol in ("SPY", "QQQ", "IWM"):
        raw = client.fetch_options_chain(symbol)
        chain = normalize_chain(raw)
        counts[symbol] = {"in": len(raw.contracts), "kept": len(chain.contracts), "rejected": len(chain.rejected)}
        assert chain.contracts, symbol
        assert chain.session_date is not None
        with mi_db.begin() as conn:
            first = publish_chain(conn, chain, run_id="live_{0}_a".format(symbol.lower()))
            second = publish_chain(conn, chain, run_id="live_{0}_b".format(symbol.lower()))
        assert first["status"] == "COMPLETE"
        assert second["status"] == "REPLAY"
        assert first["snapshot_id"] == second["snapshot_id"]
    raw_curve = client.fetch_vix_curve()
    curve = normalize_curve(raw_curve)
    counts["VX_EOD"] = {"in": len(raw_curve.points), "kept": len(curve.points), "rejected": len(curve.rejected)}
    assert curve.points
    with mi_db.begin() as conn:
        first = publish_curve(conn, curve, run_id="live_vx_a")
        second = publish_curve(conn, curve, run_id="live_vx_b")
    assert first["status"] == "COMPLETE"
    assert second["status"] == "REPLAY"
    with mi_db.connect() as conn:
        ctx = options_volatility_context(conn)
        snap_n = conn.execute(text("SELECT COUNT(*) FROM mi_openbb_snapshots WHERE publication_status = 'COMPLETE'")).scalar()
        contract_n = conn.execute(text("SELECT COUNT(*) FROM mi_openbb_option_contracts")).scalar()
        point_n = conn.execute(text("SELECT COUNT(*) FROM mi_openbb_vix_points")).scalar()
        replay_n = conn.execute(text("SELECT COUNT(*) FROM mi_openbb_snapshots")).scalar()
    assert snap_n == 4
    assert replay_n == 4
    assert contract_n == counts["SPY"]["kept"] + counts["QQQ"]["kept"] + counts["IWM"]["kept"]
    assert point_n == counts["VX_EOD"]["kept"]
    symbols = {row["underlying_symbol"] for row in ctx["symbols"]}
    assert symbols == {"SPY", "QQQ", "IWM"}
    assert ctx["vix"] is not None
    assert filter_for_export(ctx, export_mode="external")["symbols"][0].get("restricted") is True
    print("LIVE_COUNTS", counts)


def test_live_ingest_job_path_then_skips_same_session(mi_db):
    env = {
        "MI_OPENBB_OPTIONS_ENABLED": "1",
        "MI_OPENBB_VIX_ENABLED": "1",
        "MI_OPENBB_CBOE_RIGHTS_ACK": "1",
        "MI_OPENBB_OPTIONS_SYMBOLS": "SPY",
    }
    now = datetime.now(timezone.utc)
    first = ingest_openbb(mi_db, env=env, force=True, clock=lambda: now)
    second = ingest_openbb(mi_db, env=env, force=False, clock=lambda: now)
    assert first.failed is False
    assert first.symbols["SPY"]["status"] in {"COMPLETE", "REPLAY"}
    assert second.symbols["SPY"]["status"] in {"REPLAY", "SKIPPED"} or "SPY" in second.skipped_due
