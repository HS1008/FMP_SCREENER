"""FINRA aggregate ingest tests on disposable PostgreSQL (no live FINRA calls)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import text

from market_intelligence.finra_catalog import CORPORATE_BREADTH, CORPORATE_CAPPED_VOLUME, CORPORATE_SENTIMENT
from market_intelligence.ingest_finra import ingest_finra, record_individual_trace_limitation, upsert_aggregate_rows
from market_intelligence.read_models import order_flow_context


class FakeFinraClient:
    def __init__(self, pages: dict[str, list[dict]]):
        self.pages = pages
        self.request_count = 0

    def query_all(self, spec, **_kwargs):
        self.request_count += 1
        return list(self.pages.get(spec.dataset) or [])


def test_ingest_preserves_capped_flags_revisions_and_does_not_fabricate_trades(mi_db):
    client = FakeFinraClient(
        {
            CORPORATE_BREADTH.dataset: [
                {
                    "tradeReportDate": "2026-01-05",
                    "productCategory": "all securities",
                    "totalVolume": 100.0,
                    "totalTrades": 10,
                    "advances": 4,
                    "declines": 3,
                    "unchanged": 1,
                    "fiftyTwoWeekHigh": 1,
                    "fiftyTwoWeekLow": 0,
                },
                {
                    "tradeReportDate": "2026-01-05",
                    "productCategory": "investment grade",
                    "totalVolume": 60.0,
                    "totalTrades": 6,
                    "advances": 2,
                    "declines": 2,
                    "unchanged": 1,
                    "fiftyTwoWeekHigh": 0,
                    "fiftyTwoWeekLow": 0,
                },
            ],
            CORPORATE_SENTIMENT.dataset: [
                {
                    "tradeReportDate": "2026-01-05",
                    "tradeType": "all securities",
                    "productCategory": "customer buy",
                    "totalVolume": 40.0,
                    "totalTrades": 4,
                    "totalTransactions": 4,
                },
                {
                    "tradeReportDate": "2026-01-05",
                    "tradeType": "all securities",
                    "productCategory": "customer sell",
                    "totalVolume": 25.0,
                    "totalTrades": 3,
                    "totalTransactions": 3,
                },
            ],
            CORPORATE_CAPPED_VOLUME.dataset: [
                {
                    "tradeReportDate": "2026-01-05",
                    "gradeCode": "IG",
                    "144AFlag": "N",
                    "tradeYear": 2026,
                    "tradeMonth": 1,
                    "totalTradeCount": 8,
                    "totalVolumeQuantity": 50.0,
                    "customerBuyParLessThan5YearsQuantity": 10.0,
                    "customerSellParLessThan5YearsQuantity": 5.0,
                }
            ],
        }
    )
    first = ingest_finra(mi_db, client, today=date(2026, 1, 6), mode="full")
    assert not first.failed
    second = ingest_finra(mi_db, client, today=date(2026, 1, 6), mode="incremental")
    assert not second.failed
    client.pages[CORPORATE_BREADTH.dataset][0]["totalVolume"] = 110.0
    third = ingest_finra(mi_db, client, today=date(2026, 1, 6), mode="incremental")
    assert not third.failed
    with mi_db.connect() as conn:
        current = conn.execute(text("SELECT COUNT(*) FROM mi_finra_aggregate_observations WHERE is_current")).scalar()
        history = conn.execute(text("SELECT COUNT(*) FROM mi_finra_aggregate_observations")).scalar()
        trades = conn.execute(text("SELECT COUNT(*) FROM mi_bond_trades")).scalar()
        capped = conn.execute(text("SELECT volume_is_capped FROM mi_v_finra_aggregate_current WHERE dataset = :d"), {"d": CORPORATE_CAPPED_VOLUME.dataset}).scalar()
        ckpt = conn.execute(text("SELECT last_committed_observation_date FROM mi_finra_ingest_checkpoint WHERE dataset = :d"), {"d": CORPORATE_BREADTH.dataset}).scalar()
        ctx = order_flow_context(conn, today=date(2026, 1, 6))
    assert current == 5
    assert history == 6  # one breadth revision
    assert trades == 0
    assert capped is True
    assert ckpt == date(2026, 1, 5)
    ig = next(r for r in ctx["breadth"]["rows"] if r["product_category"] == "investment grade")
    all_sec = next(r for r in ctx["breadth"]["rows"] if r["product_category"] == "all securities")
    assert ig["total_volume"] != all_sec["total_volume"]
    assert ctx["sentiment"]["customer_net"]["customer_net_volume"] == 15.0
    assert "dealer-reported" in ctx["sentiment"]["customer_net"]["perspective"].lower()
    assert ctx["individual_trades"]["available"] is False
    assert ctx["capped_volume"]["rows"][0]["volume_is_capped"] is True
    assert ctx["capped_volume"]["headline_eligible"] is True
    assert ctx["capped_volume"]["rows"][0]["trade_year"] == "2026"
    assert ctx["capped_volume"]["rows"][0]["trade_month"] == "1"


def test_missing_observation_date_is_rejected_not_invented(mi_db):
    with mi_db.begin() as conn:
        record_individual_trace_limitation(conn)
        counts = upsert_aggregate_rows(
            conn,
            CORPORATE_BREADTH,
            [{"productCategory": "all securities", "totalVolume": 1}],
            run_id="run",
            retrieved_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
    assert counts["rejected"] == 1 and counts["inserted"] == 0


def test_capped_volume_period_dimensions_are_distinct_identities(mi_db):
    rows = []
    for month in range(1, 13):
        rows.append(
            {
                "tradeReportDate": "2026-01-05",
                "gradeCode": "IG",
                "144AFlag": "N",
                "tradeYear": 2025,
                "tradeMonth": month,
                "totalTradeCount": month,
                "totalVolumeQuantity": float(month),
            }
        )
    client = FakeFinraClient({CORPORATE_CAPPED_VOLUME.dataset: rows, CORPORATE_BREADTH.dataset: [], CORPORATE_SENTIMENT.dataset: []})
    report = ingest_finra(mi_db, client, today=date(2026, 1, 6), mode="full", datasets=[CORPORATE_CAPPED_VOLUME.dataset])
    assert not report.failed
    capped = next(item for item in report.results if item.dataset == CORPORATE_CAPPED_VOLUME.dataset)
    assert capped.counts["inserted"] == 12
    assert capped.counts["revised"] == 0
    with mi_db.connect() as conn:
        current = conn.execute(text("SELECT COUNT(*) FROM mi_finra_aggregate_observations WHERE dataset = :d AND is_current"), {"d": CORPORATE_CAPPED_VOLUME.dataset}).scalar()
        ckpt = conn.execute(text("SELECT last_committed_observation_date FROM mi_finra_ingest_checkpoint WHERE dataset = :d"), {"d": CORPORATE_CAPPED_VOLUME.dataset}).scalar()
        ctx = order_flow_context(conn, today=date(2026, 1, 6), include_history=False)
    assert current == 12
    assert ckpt == date(2026, 1, 5)
    assert ctx["capped_volume"]["headline_eligible"] is True
    periods = {row["reporting_period"] for row in ctx["capped_volume"]["rows"]}
    assert "2025-01" in periods and "2025-12" in periods


def test_rejected_rows_do_not_advance_checkpoint(mi_db):
    client = FakeFinraClient(
        {
            CORPORATE_BREADTH.dataset: [{"productCategory": "all securities", "totalVolume": 1}],
            CORPORATE_SENTIMENT.dataset: [],
            CORPORATE_CAPPED_VOLUME.dataset: [],
        }
    )
    report = ingest_finra(mi_db, client, today=date(2026, 1, 6), mode="full", datasets=[CORPORATE_BREADTH.dataset])
    breadth = next(item for item in report.results if item.dataset == CORPORATE_BREADTH.dataset)
    assert breadth.status == "PARTIAL"
    assert breadth.counts["rejected"] == 1
    with mi_db.connect() as conn:
        ckpt = conn.execute(text("SELECT last_committed_observation_date FROM mi_finra_ingest_checkpoint WHERE dataset = :d"), {"d": CORPORATE_BREADTH.dataset}).scalar()
        quarantined = conn.execute(text("SELECT COUNT(*) FROM mi_finra_aggregate_quarantine WHERE dataset = :d"), {"d": CORPORATE_BREADTH.dataset}).scalar()
    assert ckpt is None
    assert quarantined == 1


def test_incomplete_capped_identity_is_withheld_from_headlines(mi_db):
    from datetime import datetime, timezone

    from market_intelligence.ingest_finra import supersede_incomplete_capped_identity

    retrieved = datetime(2026, 1, 6, tzinfo=timezone.utc)
    with mi_db.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO mi_finra_aggregate_observations (
                    source_id, dataset, observation_date, category_key, grain_json, metrics_json,
                    volume_is_capped, payload_hash, retrieved_at, revision_seq, is_current
                ) VALUES (
                    'FINRA_QUERY', :ds, DATE '2026-01-05', 'IG|N',
                    '{"gradeCode":"IG","144AFlag":"N"}'::jsonb,
                    '{"totalVolumeQuantity": 99}'::jsonb,
                    TRUE, 'old', :seen, 1, TRUE
                )
                """
            ),
            {"ds": CORPORATE_CAPPED_VOLUME.dataset, "seen": retrieved},
        )
        ctx = order_flow_context(conn, today=date(2026, 1, 6), include_history=False)
        assert ctx["capped_volume"]["headline_eligible"] is False
        superseded = supersede_incomplete_capped_identity(conn, retrieved_at=retrieved)
        assert superseded == 1
        current = conn.execute(text("SELECT COUNT(*) FROM mi_finra_aggregate_observations WHERE dataset = :d AND is_current"), {"d": CORPORATE_CAPPED_VOLUME.dataset}).scalar()
        history = conn.execute(text("SELECT COUNT(*) FROM mi_finra_aggregate_observations WHERE dataset = :d"), {"d": CORPORATE_CAPPED_VOLUME.dataset}).scalar()
    assert current == 0
    assert history == 1


def test_query_page_fake_session_not_required_for_limitation_row(mi_db):
    with mi_db.begin() as conn:
        record_individual_trace_limitation(conn)
        status = conn.execute(text("SELECT capability_status FROM mi_finra_dataset_capability WHERE dataset = 'TRACE_INDIVIDUAL_TRANSACTIONS'")).scalar()
    assert status == "ENTITLEMENT_REQUIRED"
