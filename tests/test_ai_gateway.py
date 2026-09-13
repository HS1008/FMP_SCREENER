"""Unit tests for the read-only AI gateway (no database, no network).

Covers: tool surface (no forbidden capability has an implementation path), MCP dispatch,
input bounds, the holdout double-filter, the export-mode decision matrix (remote is always
external), source-specific remote rights, cache partitioning, auth/JWT and log redaction.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from ai_gateway import context as gw_context
from ai_gateway.auth import mint_access_token, presented_bearer, verify_access_token
from ai_gateway.cache import CACHE, TtlCache, cached
from ai_gateway.config import configured_export_mode, cors_origins, is_loopback_host, remote_value_sources
from ai_gateway.errors import GatewayError, RATE_LIMITED
from ai_gateway.mcp_protocol import handle_rpc, parse_messages, tools_list
from ai_gateway.rate_limit import SlidingWindowLimiter
from ai_gateway.oauth import normalize_redirect_uri
from ai_gateway.services import FORBIDDEN_TOOLS, HANDLERS, TOOL_SPECS, _artifact_blocked, _holdout_blocked, _preferred_sector_rows, register
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
    build_envelope,
    filter_for_export,
    normalize_export_mode,
)


# ---- tool surface ----------------------------------------------------------------------------------


def test_every_declared_tool_has_a_handler_and_none_are_forbidden():
    names = {spec["name"] for spec in TOOL_SPECS}
    assert names == set(HANDLERS)
    assert names.isdisjoint(FORBIDDEN_TOOLS)
    for forbidden in ("execute_sql", "query_database", "run_sql", "place_order", "execute_trade", "rebalance_portfolio", "submit_order", "connect_ibkr", "launch_backtest", "create_backtest", "compile_quantconnect", "promote_model", "promote_strategy", "run_holdout", "open_holdout", "ml_final_holdout", "download_model", "get_model_binary"):
        assert forbidden in FORBIDDEN_TOOLS
        assert forbidden not in HANDLERS


def test_forbidden_capability_cannot_even_be_registered():
    with pytest.raises(RuntimeError):
        register("execute_sql")
    assert "execute_sql" not in HANDLERS


def test_tool_schemas_are_objects_without_sql_or_path_parameters():
    for spec in TOOL_SPECS:
        schema = spec["inputSchema"]
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False
        dumped = json.dumps(schema).lower()
        assert "sql" not in dumped and "path" not in dumped and "table" not in dumped
        assert spec["name"] not in FORBIDDEN_TOOLS


def test_tool_descriptions_reflect_the_treasury_and_equity_eod_architecture():
    by_name = {spec["name"]: spec["description"] for spec in TOOL_SPECS}
    assert "Treasury XML" in by_name["get_rates_curve"] and "FRED fallback" in by_name["get_rates_curve"]
    assert "rs_1d" in by_name["get_sector_rotation"] and "ret_1d" in by_name["get_sector_rotation"]
    assert "GICS" in by_name["get_subindustry_rotation"]
    assert "not implemented" not in by_name["get_rates_curve"].lower()
    assert "LATEST_AVAILABLE" in by_name["get_data_health"] and "INGESTION_OVERDUE" in by_name["get_data_health"]


def test_mcp_initialize_and_tools_list():
    init = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "FMP Market Intelligence"
    listed = handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names == [spec["name"] for spec in tools_list()]
    assert handle_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_rejects_forbidden_and_unknown_tools_and_unknown_methods():
    blocked = handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "execute_sql", "arguments": {"query": "SELECT 1"}}})
    assert blocked["error"]["message"] == "TOOL_FORBIDDEN"
    unknown = handle_rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "not_a_tool"}})
    assert unknown["error"]["data"]["error"] == "DATA_NOT_AVAILABLE"
    other = handle_rpc({"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "file:///etc/passwd"}})
    assert other["error"]["message"] == "TOOL_FORBIDDEN"


def test_parse_messages_accepts_single_and_batch():
    assert parse_messages(b'{"jsonrpc":"2.0","id":1,"method":"ping"}')[0]["method"] == "ping"
    batch = parse_messages('[{"jsonrpc":"2.0","id":1,"method":"ping"},{"jsonrpc":"2.0","id":2,"method":"ping"}]')
    assert len(batch) == 2


# ---- input bounds ----------------------------------------------------------------------------------


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
    with pytest.raises(GatewayError):
        require_series_id("DGS10; DROP TABLE backtests")
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
    with pytest.raises(GatewayError):
        optional_strategy("../etc/passwd")


# ---- holdout double filter -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        {"research_test_type": "ML_FINAL_HOLDOUT", "test_end": "2022-12-31", "research_is_holdout": False},
        {"research_test_type": "VALIDATION", "research_phase": "HOLDOUT", "test_end": "2022-12-31", "research_is_holdout": False},
        {"research_test_type": "ML_OOS_TEST", "oos_end": "2025-06-01"},
        {"research_test_type": "ML_OOS_TEST", "test_end": "2025-01-01", "research_is_holdout": False},
        {"research_test_type": "ML_OOS_TEST", "test_start": "2025-01-01", "test_end": "2024-12-31", "research_is_holdout": False},
        {"research_test_type": "VALIDATION", "test_end": "2022-12-31", "research_is_holdout": True},
        {"research_test_type": "VALIDATION", "test_end": "2022-12-31", "research_is_holdout": None},
        {"research_test_type": "VALIDATION", "test_end": None, "research_is_holdout": False},
        {"research_test_type": "POST_HOLDOUT_ML_OOS", "test_end": "2023-12-31", "research_is_holdout": False},
        {"research_test_type": "VALIDATION", "name": "S2__holdout_check", "test_end": "2022-12-31", "research_is_holdout": False},
        {"outer_window_id": "2025H", "oos_start": "2025-01-01", "oos_end": "2025-12-31"},
        {"outer_window_id": "2016", "oos_start": "2016-01-01", "oos_end": None},
        {"research_test_type": "VALIDATION", "test_end": "2022-12-31", "research_is_holdout": False, "run_holdout_status": "ACCESSED"},
        {"research_test_type": "VALIDATION", "test_end": "2022-12-31", "research_is_holdout": False, "run_holdout_exposure_status": "ACCESSED_ONCE"},
        {"research_test_type": "VALIDATION", "test_end": "2022-12-31", "research_is_holdout": False, "run_holdout_status": None},
        {"research_test_type": "VALIDATION", "test_end": "2022-12-31", "research_is_holdout": False, "run_holdout_status": "LOCKED", "run_holdout_exposure_status": None},
        {"research_test_type": "VALIDATION", "test_end": "2022-12-31", "research_is_holdout": False, "holdout_accessed": None},
        {"research_test_type": "VALIDATION", "test_end": "2022-12-31", "research_is_holdout": False, "run_holdout_status": "UNKNOWN", "run_holdout_exposure_status": "PRISTINE"},
    ],
)
def test_holdout_rows_are_blocked_in_python_as_well_as_sql(row):
    assert _holdout_blocked(row)


def test_non_holdout_rows_before_2025_pass_the_python_filter():
    assert not _holdout_blocked({"research_test_type": "VALIDATION", "test_start": "2019-01-01", "test_end": "2022-12-31", "research_is_holdout": False})
    assert not _holdout_blocked({"outer_window_id": "2016", "oos_start": "2016-01-01", "oos_end": "2016-12-31"})
    assert not _holdout_blocked({"research_test_type": "ML_FINAL_TRAIN_PREP", "test_start": "2010-01-01", "test_end": "2024-12-31", "research_is_holdout": False, "run_holdout_status": "LOCKED", "run_holdout_exposure_status": "PRISTINE"})


@pytest.mark.parametrize(
    "row",
    [
        {"artifact_type": "diagnostics", "lineage_status": "NONHOLDOUT_EXPERIMENT_BOUND", "run_experiment_count": 3, "run_visible_experiment_count": 3, "logical_path": "runs/x/model.pkl"},
        {"artifact_type": "model_metadata", "lineage_status": "NONHOLDOUT_EXPERIMENT_BOUND", "run_experiment_count": 3, "run_visible_experiment_count": 3},
        {"artifact_type": "diagnostics", "lineage_status": "UNKNOWN", "run_experiment_count": 3, "run_visible_experiment_count": 3},
        {"artifact_type": "diagnostics", "lineage_status": "NONHOLDOUT_RUN_ALL_EXPERIMENTS_PROVEN", "run_experiment_count": 3, "run_visible_experiment_count": 2},
        {"artifact_type": "diagnostics", "lineage_status": "NONHOLDOUT_RUN_ALL_EXPERIMENTS_PROVEN", "run_experiment_count": 0, "run_visible_experiment_count": 0},
        {"artifact_type": "diagnostics", "lineage_status": "NONHOLDOUT_RUN_ALL_EXPERIMENTS_PROVEN", "run_experiment_count": None, "run_visible_experiment_count": None},
        {"artifact_type": "oos_diagnostics", "lineage_status": "NONHOLDOUT_EXPERIMENT_BOUND", "run_experiment_count": 3, "run_visible_experiment_count": 3, "research_experiment_id": "ML_FINAL_HOLDOUT_2025"},
        {"artifact_type": "innocuous", "lineage_status": "NONHOLDOUT_EXPERIMENT_BOUND", "run_experiment_count": 3, "run_visible_experiment_count": 3, "transport": "object_store_binary"},
        {"artifact_type": "diagnostics", "lineage_status": "NONHOLDOUT_EXPERIMENT_BOUND", "run_experiment_count": 3, "run_visible_experiment_count": 3, "payload_json": {"metrics": 1}},
        {"artifact_type": None, "lineage_status": "NONHOLDOUT_EXPERIMENT_BOUND", "run_experiment_count": 3, "run_visible_experiment_count": 3},
    ],
)
def test_artifact_rows_fail_closed_unless_bound_to_proven_lineage(row):
    assert _artifact_blocked(row)


def test_non_holdout_artifact_bound_to_visible_experiment_passes():
    assert not _artifact_blocked({"artifact_type": "oos_diagnostics", "logical_path": "runs/r/oos_diagnostics_2016.json", "lineage_status": "NONHOLDOUT_EXPERIMENT_BOUND", "run_experiment_count": 31, "run_visible_experiment_count": 31, "research_experiment_id": "OOS_2016"})
    assert not _artifact_blocked({"artifact_type": "run_summary", "lineage_status": "NONHOLDOUT_RUN_ALL_EXPERIMENTS_PROVEN", "run_experiment_count": 31, "run_visible_experiment_count": 31, "research_experiment_id": None})


def test_gateway_sector_rows_follow_newest_date_then_source_preference():
    newer_fmp = {"canonical_sector": "Technology", "sector_key": "Technology", "source_id": "FMP_LEGACY", "as_of": "2026-09-10", "metrics": {"ret_1d": 0.02}}
    older_ibkr = {"canonical_sector": "Technology", "sector_key": "Technology", "source_id": "EQUITY_EOD", "as_of": "2026-09-09", "metrics": {"ret_1d": 0.01}}
    chosen, primary = _preferred_sector_rows({"datasets": {"ETF_RS_VS_SPY": [older_ibkr, newer_fmp]}})
    assert len(chosen) == 1 and chosen[0]["source_id"] == "FMP_LEGACY" and primary == "FMP_LEGACY"
    newer_ibkr = dict(older_ibkr, as_of="2026-09-11")
    chosen, primary = _preferred_sector_rows({"datasets": {"ETF_RS_VS_SPY": [newer_fmp, newer_ibkr]}})
    assert chosen[0]["source_id"] == "EQUITY_EOD" and chosen[0]["as_of"] == "2026-09-11" and primary == "EQUITY_EOD"
    same_fmp = dict(newer_fmp, as_of="2026-09-10")
    same_ibkr = dict(older_ibkr, as_of="2026-09-10")
    chosen, primary = _preferred_sector_rows({"datasets": {"ETF_RS_VS_SPY": [same_fmp, same_ibkr]}})
    assert chosen[0]["source_id"] == "EQUITY_EOD" and primary == "EQUITY_EOD"
    null_date = dict(newer_ibkr, as_of=None)
    chosen, _ = _preferred_sector_rows({"datasets": {"ETF_RS_VS_SPY": [null_date, newer_fmp]}})
    assert chosen[0]["source_id"] == "FMP_LEGACY"
    missing = {"canonical_sector": "Energy", "source_id": "EQUITY_EOD", "as_of": "2026-09-10", "metrics": {}}
    chosen, _ = _preferred_sector_rows({"datasets": {"ETF_RS_VS_SPY": [missing]}})
    assert chosen[0]["metrics"] == {}


def test_oauth_redirect_uri_exact_match_rejects_wildcards_and_remote_http():
    assert normalize_redirect_uri("https://chatgpt.example/cb") == "https://chatgpt.example/cb"
    assert normalize_redirect_uri("http://127.0.0.1:8787/cb") == "http://127.0.0.1:8787/cb"
    assert normalize_redirect_uri("https://chatgpt.example/*") is None
    assert normalize_redirect_uri("http://evil.example/cb") is None
    assert normalize_redirect_uri("javascript:alert(1)") is None
    assert normalize_redirect_uri("https://chatgpt.example/cb#frag") is None
    assert normalize_redirect_uri("") is None


# ---- export mode decision matrix -------------------------------------------------------------------


def test_default_export_mode_is_external(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_EXPORT_MODE", raising=False)
    assert configured_export_mode() == "external"
    monkeypatch.setenv("AI_GATEWAY_EXPORT_MODE", "anything-else")
    assert configured_export_mode() == "external"
    assert normalize_export_mode("OWNER ") == EXPORT_MODE_OWNER
    assert normalize_export_mode("public") == EXPORT_MODE_EXTERNAL


@pytest.mark.parametrize(
    "ctx, expected",
    [
        (gw_context.RequestContext(), "external"),
        (gw_context.stdio_context(), "owner"),
        (gw_context.http_context(client_host="127.0.0.1", headers={}, bind="127.0.0.1"), "owner"),
        (gw_context.http_context(client_host="::1", headers={}, bind="127.0.0.1"), "owner"),
        (gw_context.http_context(client_host="127.0.0.1", headers={"X-Forwarded-For": "203.0.113.9"}, bind="127.0.0.1"), "external"),
        (gw_context.http_context(client_host="127.0.0.1", headers={"x-real-ip": "203.0.113.9"}, bind="127.0.0.1"), "external"),
        (gw_context.http_context(client_host="127.0.0.1", headers={"Forwarded": 'for="203.0.113.9"'}, bind="127.0.0.1"), "external"),
        (gw_context.http_context(client_host="127.0.0.1", headers={"X-Forwarded-Proto": "https"}, bind="127.0.0.1"), "external"),
        (gw_context.http_context(client_host="127.0.0.1", headers={"Host": "mcp.example.com"}, bind="127.0.0.1"), "external"),
        (gw_context.http_context(client_host="127.0.0.1", headers={"X-Forwarded-For": "127.0.0.1"}, bind="127.0.0.1"), "external"),
        (gw_context.http_context(client_host="127.0.0.1", headers={"Host": "127.0.0.1"}, bind="127.0.0.1"), "owner"),
        (gw_context.http_context(client_host="203.0.113.9", headers={}, bind="127.0.0.1"), "external"),
        (gw_context.http_context(client_host="127.0.0.1", headers={}, bind="0.0.0.0"), "external"),
        (gw_context.http_context(client_host=None, headers={}, bind="127.0.0.1"), "external"),
    ],
)
def test_owner_mode_applies_only_to_proven_local_sessions(monkeypatch, ctx, expected):
    monkeypatch.setenv("AI_GATEWAY_EXPORT_MODE", "owner")
    assert gw_context.effective_export_mode(ctx) == expected


def test_external_preference_never_yields_owner_even_locally(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_EXPORT_MODE", "external")
    assert gw_context.effective_export_mode(gw_context.stdio_context()) == "external"
    assert gw_context.effective_export_mode(gw_context.http_context(client_host="127.0.0.1", headers={}, bind="127.0.0.1")) == "external"


def test_trust_proxy_off_cannot_restore_owner_mode(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_EXPORT_MODE", "owner")
    monkeypatch.setenv("AI_GATEWAY_TRUST_PROXY", "0")
    ctx = gw_context.http_context(
        client_host="127.0.0.1",
        headers={"X-Forwarded-For": "198.51.100.7"},
        bind="127.0.0.1",
    )
    assert gw_context.effective_export_mode(ctx) == "external"


def test_is_loopback_host():
    assert is_loopback_host("127.0.0.1") and is_loopback_host("::1") and is_loopback_host("localhost") and is_loopback_host("127.5.5.5")
    assert not is_loopback_host("10.0.0.1") and not is_loopback_host("") and not is_loopback_host(None)


# ---- export policy under each mode -----------------------------------------------------------------


INTERNAL_ENTRY = {"sector_key": "Technology", "source_id": "EQUITY_EOD", "export_scope": "INTERNAL_ONLY", "ret_1d": 0.012, "metrics": {"ret_1d": 0.012, "rs_chg_1d": 0.004}}
RESTRICTED_ENTRY = {"series_id": "BAMLC0A0CM", "bucket": "ig_broad", "source_id": "FRED", "export_scope": "RESTRICTED_REDISTRIBUTION", "oas_bps": 93.0, "change_1d_bps": -1.0}
UNKNOWN_ENTRY = {"series_id": "X", "export_scope": "PARTNER_ONLY", "value": 1.0}
UNSCOPED_ENTRY = {"series_id": "Y", "value": 2.0}
PUBLIC_ENTRY = {"series_id": "UST_NOM_10Y", "source_id": "TREASURY", "export_scope": "ATTRIBUTION_REQUIRED", "yield_pct": 4.95, "observation_date": "2026-09-10"}


def test_remote_external_policy_redacts_internal_restricted_and_unknown_but_keeps_identity():
    for entry in (INTERNAL_ENTRY, RESTRICTED_ENTRY, UNKNOWN_ENTRY, UNSCOPED_ENTRY):
        out = filter_for_export(entry, export_mode=EXPORT_MODE_EXTERNAL)
        assert out.get("restricted") is True, entry
        dumped = json.dumps(out)
        for value in ("0.012", "0.004", "93.0", "-1.0", "1.0", "2.0"):
            assert value not in dumped, (entry, dumped)
        assert out.get("restriction_reason")
    kept = filter_for_export(INTERNAL_ENTRY, export_mode=EXPORT_MODE_EXTERNAL)
    assert kept["sector_key"] == "Technology" and kept["source_id"] == "EQUITY_EOD"
    public = filter_for_export(PUBLIC_ENTRY, export_mode=EXPORT_MODE_EXTERNAL)
    assert public["yield_pct"] == 4.95 and public.get("restricted") is not True


def test_owner_mode_includes_internal_values_but_never_secrets_or_unknown_scopes():
    owner = filter_for_export(INTERNAL_ENTRY, export_mode=EXPORT_MODE_OWNER)
    assert owner["ret_1d"] == 0.012 and owner.get("restricted") is not True
    credit = filter_for_export(RESTRICTED_ENTRY, export_mode=EXPORT_MODE_OWNER)
    assert credit["oas_bps"] == 93.0
    assert filter_for_export(UNKNOWN_ENTRY, export_mode=EXPORT_MODE_OWNER).get("restricted") is True
    assert filter_for_export(UNSCOPED_ENTRY, export_mode=EXPORT_MODE_OWNER).get("restricted") is True
    secret = filter_for_export({"export_scope": "PUBLIC", "value": 1, "api_key": "abc", "model_binary": "b64", "database_url": "postgres://x"}, export_mode=EXPORT_MODE_OWNER)
    assert "api_key" not in secret and "model_binary" not in secret and "database_url" not in secret


def test_source_specific_remote_right_is_explicit_and_narrow():
    # Allowing EQUITY_EOD does not unlock the credit (FRED/ICE) entry or unknown scopes.
    allowed = filter_for_export(INTERNAL_ENTRY, export_mode=EXPORT_MODE_EXTERNAL, remote_value_sources=("EQUITY_EOD",))
    assert allowed["ret_1d"] == 0.012 and allowed.get("restricted") is not True
    still_redacted = filter_for_export(RESTRICTED_ENTRY, export_mode=EXPORT_MODE_EXTERNAL, remote_value_sources=("EQUITY_EOD",))
    assert still_redacted.get("restricted") is True
    assert filter_for_export(UNKNOWN_ENTRY, export_mode=EXPORT_MODE_EXTERNAL, remote_value_sources=("EQUITY_EOD", "FRED")).get("restricted") is True
    # An entry without a source cannot benefit from any allowlist.
    anonymous = dict(INTERNAL_ENTRY)
    anonymous.pop("source_id")
    assert filter_for_export(anonymous, export_mode=EXPORT_MODE_EXTERNAL, remote_value_sources=("EQUITY_EOD",)).get("restricted") is True


def test_remote_value_sources_env_is_empty_by_default_and_normalised(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_REMOTE_VALUE_SOURCES", raising=False)
    assert remote_value_sources() == ()
    assert "IBKR" not in remote_value_sources()
    monkeypatch.setenv("AI_GATEWAY_REMOTE_VALUE_SOURCES", " equity_eod, Treasury ,")
    assert remote_value_sources() == ("EQUITY_EOD", "TREASURY")
    assert "IBKR" not in remote_value_sources()


def test_envelope_records_mode_and_unknown_mode_fails_closed():
    external = build_envelope({"rows": [INTERNAL_ENTRY]}, provenance={"kind": "LIVE_VIEW", "source_snapshot_hash": None}, export_mode="whatever")
    assert external["export_mode"] == "external" and external["restricted_entries"] == 1
    owner = build_envelope({"rows": [INTERNAL_ENTRY]}, provenance={"kind": "LIVE_VIEW", "source_snapshot_hash": None}, export_mode="owner")
    assert owner["export_mode"] == "owner" and owner["restricted_entries"] == 0


def test_respond_uses_request_context_not_configuration(monkeypatch):
    from ai_gateway.envelope import respond

    monkeypatch.setenv("AI_GATEWAY_EXPORT_MODE", "owner")
    body = {"rows": [dict(INTERNAL_ENTRY)]}
    provenance = {"kind": "LIVE_VIEW", "source_snapshot_hash": None, "captured_at": "2026-09-11T00:00:00+00:00"}
    remote = gw_context.set_context(gw_context.http_context(client_host="203.0.113.9", headers={}, bind="127.0.0.1"))
    try:
        env = respond(body, provenance=provenance, tool="t", available=True)
    finally:
        gw_context.reset_context(remote)
    assert env["export_mode"] == "external" and env["owner_session"] is False and env["restricted_entries"] == 1
    assert "0.012" not in json.dumps(env)
    local = gw_context.set_context(gw_context.stdio_context())
    try:
        env = respond(body, provenance=provenance, tool="t", available=True)
        # An explicit request for owner cannot widen, but an explicit external can narrow.
        narrowed = respond(body, provenance=provenance, tool="t", available=True, mode="external")
    finally:
        gw_context.reset_context(local)
    assert env["export_mode"] == "owner" and env["owner_session"] is True and env["restricted_entries"] == 0
    assert narrowed["export_mode"] == "external" and narrowed["restricted_entries"] == 1
    # Remote session asking for owner explicitly still gets external.
    remote = gw_context.set_context(gw_context.http_context(client_host="203.0.113.9", headers={}, bind="127.0.0.1"))
    try:
        env = respond(body, provenance=provenance, tool="t", available=True, mode="owner")
    finally:
        gw_context.reset_context(remote)
    assert env["export_mode"] == "external"


def test_cache_is_partitioned_by_effective_export_mode(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_EXPORT_MODE", "owner")
    CACHE.clear()
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        return {"n": calls["n"]}

    local = gw_context.set_context(gw_context.stdio_context())
    try:
        first = cached("data_health", "k", builder)
    finally:
        gw_context.reset_context(local)
    remote = gw_context.set_context(gw_context.http_context(client_host="203.0.113.9", headers={}, bind="127.0.0.1"))
    try:
        second = cached("data_health", "k", builder)
    finally:
        gw_context.reset_context(remote)
    assert first == {"n": 1} and second == {"n": 2}
    CACHE.clear()


# ---- infrastructure --------------------------------------------------------------------------------


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


def test_cors_wildcard_is_never_honoured(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_CORS_ORIGINS", "*, https://chat.openai.com")
    assert cors_origins() == ("https://chat.openai.com",)
    monkeypatch.delenv("AI_GATEWAY_CORS_ORIGINS", raising=False)
    assert cors_origins() == ()


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
    # Tokens minted for another audience (or a wildcard) are refused.
    with pytest.raises(GatewayError):
        verify_access_token(mint_access_token(audience="https://other.example.com/mcp"), audience="https://mcp.example.com/mcp")
    with pytest.raises(GatewayError):
        verify_access_token(mint_access_token(audience="*"), audience="https://mcp.example.com/mcp")


def test_oauth_s256_helper_matches_standard():
    from ai_gateway.oauth import _s256

    assert _s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_logs_do_not_contain_secrets(caplog):
    import logging

    from ai_gateway.logging import log_event

    caplog.set_level(logging.INFO, logger="ai_gateway")
    log_event("auth", authorization="Bearer super-secret", token="super-secret", database_url="postgresql://u:pw@h/db", status="success")
    text = caplog.text
    assert "super-secret" not in text and "pw@h" not in text
    assert "***" in text


def test_freshness_v2_weekend_and_holiday_are_not_stale():
    from market_intelligence.freshness import LATEST_AVAILABLE, STALE, assess_freshness

    friday = date(2026, 9, 4)
    saturday = date(2026, 9, 5)
    labor = date(2026, 9, 7)
    assert assess_freshness(friday, "D", saturday, series_id="UST_NOM_10Y").status == LATEST_AVAILABLE
    assert assess_freshness(friday, "D", labor, series_id="UST_NOM_10Y").status == LATEST_AVAILABLE
    assert assess_freshness(date(2026, 8, 21), "D", labor, series_id="UST_NOM_10Y").status == STALE
