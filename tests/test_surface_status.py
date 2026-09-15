"""Main pages use current/delayed/stale/unavailable/blocked."""

from market_intelligence.surface_status import BLOCKED, CURRENT, DELAYED, STALE, UNAVAILABLE, surface_status, worst_surface_status
from market_intelligence.ui import freshness_chip


def test_surface_status_mapping():
    assert surface_status({"freshness_status": "FRESH", "transport_status": "OK"}) == CURRENT
    assert surface_status({"freshness_status": "LATEST_AVAILABLE", "transport_status": "OK"}) == CURRENT
    assert surface_status({"freshness_status": "INGESTION_OVERDUE", "transport_status": "OK"}) == DELAYED
    assert surface_status({"freshness_status": "STALE", "transport_status": "OK"}) == STALE
    assert surface_status({"freshness_status": "STALE_INGESTION", "transport_status": "OK"}) == STALE
    assert surface_status({"freshness_status": "CURRENT_TO_SOURCE", "transport_status": "OK"}) == CURRENT
    assert surface_status({"transport_status": "FAILED"}) == BLOCKED
    assert surface_status({"transport_status": "PARTIAL"}) == DELAYED
    assert surface_status({}) == UNAVAILABLE


def test_worst_and_chips():
    assert worst_surface_status(
        [
            {"freshness_status": "FRESH", "transport_status": "OK"},
            {"freshness_status": "STALE", "transport_status": "OK"},
        ]
    ) == STALE
    assert "Current" in freshness_chip("current")
    assert "Blocked" in freshness_chip("blocked")
    assert "Publication lag" in freshness_chip("HEALTHY_PUBLICATION_LAG")


def test_research_delivery_failed_transport_is_on_demand():
    from market_intelligence.read_models import _apply_research_delivery_policy

    row = _apply_research_delivery_policy(
        {
            "source_id": "QS_RESEARCH_DELIVERY",
            "access_status": "SOURCE_REF_NOT_CONFIGURED",
            "transport_status": "FAILED",
            "freshness_status": "UNKNOWN",
        }
    )
    assert row["transport_status"] == "SKIPPED"
    assert row["freshness_status"] == "ON_DEMAND"
    assert row["policy_status"] == "ON_DEMAND"
