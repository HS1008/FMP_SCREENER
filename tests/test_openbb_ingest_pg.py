"""PostgreSQL snapshot idempotency and failed-attempt visibility."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from market_intelligence.export_policy import filter_for_export
from market_intelligence.freshness import assess_freshness
from market_intelligence.openbb_provider.client import OpenBBClient, RawChainFetch, RawCurveFetch
from market_intelligence.openbb_provider.ingest import ingest_openbb
from market_intelligence.openbb_provider.normalize import normalize_chain
from market_intelligence.openbb_provider.store import publish_chain
from market_intelligence.openbb_provider.vix import normalize_curve
from market_intelligence.read_models import options_volatility_context
from tests.test_openbb_normalize import _raw, _row

NY = ZoneInfo("America/New_York")


def _enabled():
    return {
        "MI_OPENBB_OPTIONS_ENABLED": "1",
        "MI_OPENBB_VIX_ENABLED": "1",
        "MI_OPENBB_CBOE_RIGHTS_ACK": "1",
        "MI_OPENBB_OPTIONS_SYMBOLS": "SPY",
    }


def test_same_snapshot_replay_is_idempotent(mi_db):
    snap = normalize_chain(_raw([_row(), _row(contract_symbol="SPY260918P00500000", option_type="put", delta=-0.5)], fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY)))
    with mi_db.begin() as conn:
        first = publish_chain(conn, snap, run_id="run_a")
        second = publish_chain(conn, snap, run_id="run_b")
    assert first["status"] in {"COMPLETE", "SUCCEEDED"}
    assert second["status"] == "REPLAY"
    assert first["snapshot_id"] == second["snapshot_id"]
    with mi_db.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM mi_openbb_snapshots")).scalar()
    assert count == 1


def test_failed_attempt_keeps_last_valid(mi_db):
    good = normalize_chain(_raw([_row()], fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY)))
    with mi_db.begin() as conn:
        published = publish_chain(conn, good, run_id="run_ok")
        publish_chain(conn, good, run_id="run_fail", failed=True, error="TIMEOUT")
    with mi_db.connect() as conn:
        current = conn.execute(text("SELECT snapshot_id FROM mi_openbb_snapshots WHERE is_current AND publication_status = 'COMPLETE'")).scalar()
        ctx = options_volatility_context(conn)
    assert current == published["snapshot_id"]
    assert ctx["symbols"][0]["snapshot_id"] == published["snapshot_id"]


def test_one_symbol_failure_does_not_block_other(mi_db):
    def chains(symbol):
        if symbol == "QQQ":
            raise RuntimeError("boom")
        return RawChainFetch(
            symbol=symbol,
            contracts=[_row()],
            metadata={"symbol": symbol, "current_price": 500, "last_trade_timestamp": "2026-09-11 16:00:00"},
            fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY),
        )

    client = OpenBBClient(chain_fn=chains, curve_fn=lambda: (_ for _ in ()).throw(AssertionError("vix")))
    env = _enabled()
    env["MI_OPENBB_OPTIONS_SYMBOLS"] = "SPY,QQQ"
    env["MI_OPENBB_VIX_ENABLED"] = "0"
    report = ingest_openbb(mi_db, client, env=env, include_vix=False, force=True, clock=lambda: datetime(2026, 9, 11, 16, 10, tzinfo=NY))
    assert report.failed is True
    assert report.symbols["SPY"]["status"] in {"COMPLETE", "REPLAY", "SUCCEEDED"}
    assert report.symbols["QQQ"]["status"] == "FAILED"


def test_vix_read_model_and_export(mi_db):
    raw = RawCurveFetch(
        symbol="VX_EOD",
        points=[{"expiration": "2026-09", "price": 15.2, "date": "2026-09-11"}, {"expiration": "2026-10", "price": 16.1, "date": "2026-09-11"}],
        metadata={},
        fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY),
    )
    curve = normalize_curve(raw)
    from market_intelligence.openbb_provider.store import publish_curve

    with mi_db.begin() as conn:
        publish_curve(conn, curve, run_id="run_vix")
    with mi_db.connect() as conn:
        ctx = options_volatility_context(conn)
    assert ctx["vix"] is not None
    redacted = filter_for_export(ctx, export_mode="external")
    assert (redacted.get("vix") or {}).get("restricted") is True
    assert "points" not in (redacted.get("vix") or {})
    owner = filter_for_export(ctx, export_mode="owner")
    assert owner.get("vix") is not None
    assert owner["vix"].get("restricted") is not True
    assert owner["vix"].get("front_shape") or owner["vix"].get("m1")


def test_provenance_and_freshness_are_stored(mi_db):
    snap = normalize_chain(_raw([_row()], fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY)))
    with mi_db.begin() as conn:
        published = publish_chain(conn, snap, run_id="run_prov")
    with mi_db.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT source_id, provider, underlying, session_date, observation_precision,
                       collected_at, source_timestamp_utc, content_hash, publication_status,
                       openbb_version, export_scope, normalization_version
                FROM mi_openbb_snapshots WHERE snapshot_id = :id
                """
            ),
            {"id": published["snapshot_id"]},
        ).mappings().one()
        fresh = conn.execute(
            text("SELECT source_id, dataset, transport_status, latest_observation_date, coverage_status FROM mi_data_freshness WHERE source_id = 'OPENBB_CBOE_OPTIONS'")
        ).mappings().first()
        contracts = conn.execute(text("SELECT COUNT(*) FROM mi_openbb_option_contracts WHERE snapshot_id = :id"), {"id": published["snapshot_id"]}).scalar()
    assert row["source_id"] == "OPENBB_CBOE_OPTIONS"
    assert row["provider"] == "cboe"
    assert row["underlying"] == "SPY"
    assert str(row["session_date"]) == "2026-09-11"
    assert row["observation_precision"] == "timestamp"
    assert row["collected_at"] is not None
    assert row["source_timestamp_utc"] is not None
    assert row["content_hash"] == snap.content_hash
    assert row["publication_status"] == "COMPLETE"
    assert row["export_scope"] == "INTERNAL_ONLY"
    assert contracts == 1
    assert fresh["transport_status"] == "OK"
    assert str(fresh["latest_observation_date"]) == "2026-09-11"


def test_provider_exception_does_not_clobber_complete_or_invent_zeroes(mi_db):
    good = normalize_chain(_raw([_row(gamma=0.02, open_interest=10)], fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY)))
    with mi_db.begin() as conn:
        first = publish_chain(conn, good, run_id="run_ok")
        failed = publish_chain(conn, good, run_id="run_fail", failed=True, error="TIMEOUT")
    with mi_db.connect() as conn:
        current = conn.execute(text("SELECT snapshot_id, publication_status FROM mi_openbb_snapshots WHERE is_current AND publication_status = 'COMPLETE'")).mappings().one()
        failed_row = conn.execute(text("SELECT is_current, publication_status, quality_json FROM mi_openbb_snapshots WHERE snapshot_id = :id"), {"id": failed["snapshot_id"]}).mappings().one()
        ctx = options_volatility_context(conn)
    assert current["snapshot_id"] == first["snapshot_id"]
    assert failed_row["is_current"] is False
    assert failed_row["publication_status"] == "FAILED"
    assert (failed_row["quality_json"] or {}).get("error") == "TIMEOUT"
    assert ctx["symbols"][0]["snapshot_id"] == first["snapshot_id"]
    gex = ctx["symbols"][0]["gex"]
    assert gex.get("dealer_gex") is False


def test_read_models_do_not_call_openbb(mi_db, monkeypatch):
    import sys

    snap = normalize_chain(_raw([_row()], fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY)))
    curve = normalize_curve(
        RawCurveFetch(
            symbol="VX_EOD",
            points=[{"expiration": "2026-09", "price": 15.2, "date": "2026-09-11"}],
            metadata={},
            fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY),
        )
    )
    with mi_db.begin() as conn:
        publish_chain(conn, snap, run_id="run_ro")
        from market_intelligence.openbb_provider.store import publish_curve

        publish_curve(conn, curve, run_id="run_ro_vix")

    def boom(*_a, **_k):
        raise AssertionError("read path fetched OpenBB/Cboe")

    monkeypatch.setattr("market_intelligence.openbb_provider.client.OpenBBClient.fetch_options_chain", boom)
    monkeypatch.setattr("market_intelligence.openbb_provider.client.OpenBBClient.fetch_vix_curve", boom)
    sys.modules.pop("openbb", None)
    from market_intelligence.read_models import data_health_context

    with mi_db.connect() as conn:
        ctx = options_volatility_context(conn)
        health = data_health_context(conn)
    assert "openbb" not in sys.modules
    assert ctx["symbols"][0]["underlying_symbol"] == "SPY"
    assert ctx["vix"] is not None
    assert health["options_volatility"]["symbols"][0]["snapshot_id"] == ctx["symbols"][0]["snapshot_id"]
    remote = filter_for_export(ctx, export_mode="external")
    assert remote["symbols"][0].get("restricted") is True
    assert (remote.get("vix") or {}).get("restricted") is True
    assert "iv_30d" not in remote["symbols"][0]


def test_stale_snapshot_is_labelled_not_rewritten(mi_db):
    snap = normalize_chain(_raw([_row(gamma=0.02, open_interest=10)], fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY)))
    with mi_db.begin() as conn:
        publish_chain(conn, snap, run_id="run_stale")
    with mi_db.connect() as conn:
        ctx = options_volatility_context(conn)
    row = ctx["symbols"][0]
    assessment = assess_freshness(
        date(2026, 9, 11),
        "D",
        date(2026, 9, 21),
        source_id="OPENBB_CBOE_OPTIONS",
    )
    assert assessment.status == "STALE"
    assert str(row["session_date"]) == "2026-09-11"
    assert row["gex"].get("dealer_gex") is False
    assert row["snapshot_id"]


