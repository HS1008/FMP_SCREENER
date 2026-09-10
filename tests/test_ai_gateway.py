"""Unit tests for the read-only AI gateway (no database, no network)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from ai_gateway.auth import mint_access_token, presented_bearer, verify_access_token
from ai_gateway.cache import CACHE, TtlCache, cached
from ai_gateway.errors import GatewayError, RATE_LIMITED
from ai_gateway.mcp_protocol import handle_rpc, parse_messages, tools_list
from ai_gateway.rate_limit import SlidingWindowLimiter
from ai_gateway.services import FORBIDDEN_TOOLS, HANDLERS, TOOL_SPECS, _holdout_blocked
from ai_gateway.validation import (
    bounded_range,
    clamp_limit,
    optional_strategy,
    parse_since,
    require_series_id,
    resolve_sector,
)
from market_intelligence.export_policy import (
    EXPORT_MODE_EXTERNAL,
    EXPORT_MODE_OWNER,
    filter_for_export,
)


def test_every_declared_tool_has_a_handler_and_none_are_forbidden():
    names = {spec["name"] for spec in TOOL_SPECS}
    assert names == set(HANDLERS)
    assert names.isdisjoint(FORBIDDEN_TOOLS)
    assert "execute_sql" not in names
    assert "place_order" not in names
    assert "launch_backtest" not in names
    assert "ml_final_holdout" not in names


def test_tool_schemas_are_objects_without_sql_parameters():
    for spec in TOOL_SPECS:
        schema = spec["inputSchema"]
        assert schema["type"] == "object"
        dumped = json.dumps(schema)
        assert "sql" not in dumped.lower()
        assert spec["name"] not in FORBIDDEN_TOOLS


def test_mcp_initialize_and_tools_list():
    init = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "FMP Market Intelligence"
    listed = handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names == [spec["name"] for spec in tools_list()]
    assert handle_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_rejects_forbidden_and_unknown_tools():
    blocked = handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "execute_sql", "arguments": {"query": "SELECT 1"}}})
    assert blocked["error"]["message"] == "TOOL_FORBIDDEN"
    unknown = handle_rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "not_a_tool"}})
    assert unknown["error"]["data"]["error"] == "DATA_NOT_AVAILABLE"


def test_parse_messages_accepts_single_and_batch():
    assert parse_messages(b'{"jsonrpc":"2.0","id":1,"method":"ping"}')[0]["method"] == "ping"
    batch = parse_messages('[{"jsonrpc":"2.0","id":1,"method":"ping"},{"jsonrpc":"2.0","id":2,"method":"ping"}]')
    assert len(batch) == 2


def test_validation_bounds_and_enums():
    assert clamp_limit(None, default=10) == 10
    assert clamp_limit(9999, default=10, maximum=20) == 20
    with pytest.raises(GatewayError) as exc:
        clamp_limit("x", default=10)
    assert exc.value.code == "INVALID_INPUT"
    assert require_series_id("dgs10") == "DGS10"
    with pytest.raises(GatewayError) as exc:
        require_series_id("NOT_A_SERIES")
    assert exc.value.code == "UNKNOWN_SERIES"
    assert resolve_sector("Consumer Cyclical") == "Consumer Discretionary"
    with pytest.raises(GatewayError) as exc:
        resolve_sector("Not A Sector")
    assert exc.value.code == "UNKNOWN_SECTOR"
    with pytest.raises(GatewayError):
        bounded_range(date(2026, 1, 2), date(2026, 1, 1))
    assert parse_since("yesterday") == "yesterday"
    assert parse_since("2024-01-02") == date(2024, 1, 2)
    assert optional_strategy("SPYTrend") == "SPYTrend"
    with pytest.raises(GatewayError):
        optional_strategy("ML_FINAL_HOLDOUT")


def test_holdout_rows_are_blocked_in_python_as_well_as_sql():
    assert _holdout_blocked({"research_test_type": "ML_FINAL_HOLDOUT"})
    assert _holdout_blocked({"oos_end": "2025-06-01"})
    assert _holdout_blocked({"research_is_holdout": True})
    assert not _holdout_blocked({"research_test_type": "VALIDATION", "test_end": "2022-12-31"})


def test_owner_export_includes_internal_values_external_does_not():
    entry = {"sector_key": "Technology", "export_scope": "INTERNAL_ONLY", "ret_1d": 0.012, "metrics": {"ret_1d": 0.012}}
    external = filter_for_export(entry, export_mode=EXPORT_MODE_EXTERNAL)
    assert external.get("restricted") is True
    assert "ret_1d" not in external
    owner = filter_for_export(entry, export_mode=EXPORT_MODE_OWNER)
    assert owner.get("ret_1d") == 0.012
    assert owner.get("restricted") is not True
    secret = filter_for_export({"export_scope": "PUBLIC", "value": 1, "api_key": "abc"}, export_mode=EXPORT_MODE_OWNER)
    assert "api_key" not in secret


def test_ttl_cache_expires_and_hits():
    cache = TtlCache()
    cache.set("k", "v", 60)
    assert cache.get("k") == "v"
    cache.set("k", "v", 0)
    assert cache.get("k") is None
    CACHE.clear()
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        return "x"

    assert cached("data_health", "t", builder) == "x"
    assert cached("data_health", "t", builder) == "x"
    assert calls["n"] == 1
    CACHE.clear()


def test_rate_limiter_blocks_over_limit():
    limiter = SlidingWindowLimiter(window_seconds=60)
    for _ in range(3):
        limiter.check("id", 3)
    with pytest.raises(GatewayError) as exc:
        limiter.check("id", 3)
    assert exc.value.code == RATE_LIMITED
    limiter.check("other", 3)


def test_bearer_extraction_and_jwt_roundtrip(monkeypatch):
    monkeypatch.setenv("AI_CONTEXT_API_TOKEN", "unit-test-token")
    assert presented_bearer("Bearer abc") == "abc"
    assert presented_bearer("Basic abc") is None
    token = mint_access_token(audience="https://mcp.example.com/mcp")
    payload = verify_access_token(token, audience="https://mcp.example.com/mcp")
    assert payload["sub"] == "owner"
    assert payload["scope"] == "mi.read"
    with pytest.raises(GatewayError):
        verify_access_token("not-a-jwt", audience="https://mcp.example.com/mcp")


def test_oauth_s256_helper_matches_standard():
    from ai_gateway.oauth import _s256

    assert _s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_logs_do_not_contain_secrets(caplog):
    import logging

    from ai_gateway.logging import log_event

    caplog.set_level(logging.INFO, logger="ai_gateway")
    log_event("auth", authorization="Bearer super-secret", token="super-secret", status="success")
    text = caplog.text
    assert "super-secret" not in text
    assert "***" in text


def test_freshness_weekend_is_not_automatically_stale():
    from market_intelligence.freshness import assess_freshness

    friday = date(2026, 9, 4)
    saturday = date(2026, 9, 5)
    monday = date(2026, 9, 7)
    labor = date(2026, 9, 7)
    assert assess_freshness(friday, "D", saturday).status == "FRESH"
    # Labor Day 2026 is Monday Sep 7; Tuesday is the next business day.
    assert assess_freshness(friday, "D", labor).status == "FRESH"
    assert assess_freshness(friday - timedelta(days=14), "D", monday).status == "STALE"
