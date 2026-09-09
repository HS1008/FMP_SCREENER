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


def test_query_page_fake_session_not_required_for_limitation_row(mi_db):
    with mi_db.begin() as conn:
        record_individual_trace_limitation(conn)
        status = conn.execute(text("SELECT capability_status FROM mi_finra_dataset_capability WHERE dataset = 'TRACE_INDIVIDUAL_TRANSACTIONS'")).scalar()
    assert status == "ENTITLEMENT_REQUIRED"
