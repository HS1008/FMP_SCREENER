"""OpenBB dependency boundary: import/render/dry-run never load OpenBB or fetch."""

from __future__ import annotations

import sys
from pathlib import Path

from jobs.market_intelligence_refresh import EXIT_OK, build_parser, plan, run
from market_intelligence import adapters, export_policy, morning_context, read_models
from market_intelligence.openbb_provider import probe_openbb
from market_intelligence.openbb_provider.client import AcquisitionError, OpenBBClient
from market_intelligence.openbb_provider.errors import OpenBBAcquisitionError


def test_package_import_does_not_load_openbb():
    sys.modules.pop("openbb", None)
    import market_intelligence.openbb_provider as pkg
    import market_intelligence.openbb_provider.analytics as analytics

    assert pkg.OPENBB_OPTIONS_SOURCE_ID == "OPENBB_CBOE_OPTIONS"
    assert "openbb" not in sys.modules
    assert analytics.TARGET_30D == 30
    assert read_models.options_volatility_context
    assert "options_volatility" in morning_context.SECTION_ORDER


def test_probe_default_is_disabled():
    probe = probe_openbb({})
    assert probe.options.access_status == adapters.ACCESS_DISABLED
    assert probe.vix.access_status == adapters.ACCESS_DISABLED
    rights = probe_openbb({"MI_OPENBB_OPTIONS_ENABLED": "1", "MI_OPENBB_VIX_ENABLED": "1"})
    assert rights.options.access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED


def test_dry_run_and_probe_config_do_not_fetch():
    code = run(["--all-configured", "--dry-run", "--json"], env={"MI_TREASURY_ENABLED": "0"})
    assert code == EXIT_OK
    code = run(["--probe-config", "--json"], env={"MI_TREASURY_ENABLED": "0"})
    assert code == EXIT_OK


def test_explicit_options_plan_fails_when_disabled():
    args = build_parser().parse_args(["--options", "--dry-run"])
    the_plan = plan(args, {"MI_TREASURY_ENABLED": "0"})
    step = next(s for s in the_plan["steps"] if s["step"] == "options")
    assert step["action"] == "fail_unconfigured"


def test_all_configured_skips_disabled_options():
    args = build_parser().parse_args(["--all-configured", "--dry-run"])
    the_plan = plan(args, {"MI_TREASURY_ENABLED": "0"})
    step = next(s for s in the_plan["steps"] if s["step"] == "options")
    assert step["action"] == "skip_unconfigured"


def test_retries_are_bounded():
    attempts = {"n": 0}

    def _rate(symbol=None):
        attempts["n"] += 1
        raise AcquisitionError("rate_limited", "429")

    client = OpenBBClient(chain_fn=_rate, sleeper=lambda s: None)
    try:
        client.fetch_options_chain("SPY")
        raise AssertionError("expected OpenBBAcquisitionError")
    except OpenBBAcquisitionError as exc:
        assert exc.category.upper() in {"RATE_LIMITED", "RATE_LIMIT"}
        assert exc.retryable is True
    assert attempts["n"] >= 2


def test_access_denied_is_not_retried():
    attempts = {"n": 0}

    def _denied(symbol=None):
        attempts["n"] += 1
        raise AcquisitionError("access_denied", "403")

    client = OpenBBClient(chain_fn=_denied)
    try:
        client.fetch_options_chain("SPY")
        raise AssertionError("expected OpenBBAcquisitionError")
    except OpenBBAcquisitionError as exc:
        assert exc.category.upper() == "ACCESS_DENIED"
        assert exc.retryable is False
    assert attempts["n"] == 1


def test_export_policy_redacts_new_value_keys():
    body = {
        "export_scope": "INTERNAL_ONLY",
        "source_id": "OPENBB_CBOE_OPTIONS",
        "label": "SPY",
        "iv_30d": 16.2,
        "gex": {"signed_net": 1.2e9},
        "strike": 500,
        "points": [{"price": 16.1}],
    }
    filtered = export_policy.filter_for_export(body, export_mode="external")
    assert filtered.get("restricted") is True
    assert "iv_30d" not in filtered
    owner = export_policy.filter_for_export(body, export_mode="owner")
    assert owner.get("iv_30d") == 16.2


def test_cboe_is_not_on_remote_allowlist_by_default():
    example = (Path(__file__).resolve().parents[1] / "deploy" / "market_intelligence" / "market_intelligence.env.example").read_text(encoding="utf-8")
    remote = next(line for line in example.splitlines() if "AI_GATEWAY_REMOTE_VALUE_SOURCES=" in line)
    assert "OPENBB" not in remote and "CBOE" not in remote


def test_only_acquisition_client_imports_openbb_package():
    import ast
    import re

    root = Path(__file__).resolve().parents[1]
    hits = []
    roots = [root / "market_intelligence", root / "jobs", root / "pages", root / "ai_gateway"]
    for base in roots:
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if path.name == "client.py" and path.parent.name == "openbb_provider":
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"(?:from openbb import|import openbb)\b", text):
                hits.append(str(path.relative_to(root)))
    assert hits == [], hits
    client = (root / "market_intelligence" / "openbb_provider" / "client.py").read_text(encoding="utf-8")
    tree = ast.parse(client)
    found_lazy = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "openbb":
            found_lazy = True
    assert found_lazy


def test_streamlit_and_read_models_do_not_reference_openbb_package():
    root = Path(__file__).resolve().parents[1]
    consumers = [
        root / "market_intelligence" / "pages_ui.py",
        root / "market_intelligence" / "read_models.py",
        root / "market_intelligence" / "morning_context.py",
        root / "market_intelligence" / "export_policy.py",
        root / "ai_context_api.py",
        *sorted((root / "pages").glob("*.py")),
    ]
    for path in consumers:
        text = path.read_text(encoding="utf-8")
        assert "from openbb" not in text
        assert "import openbb\n" not in text
        assert "obb.derivatives" not in text


def test_openbb_does_not_replace_preferred_sources():
    refresh = (Path(__file__).resolve().parents[1] / "jobs" / "market_intelligence_refresh.py").read_text(encoding="utf-8")
    assert "from market_intelligence.ingest_treasury import ingest_treasury" in refresh
    assert "from market_intelligence.ingest_finra import ingest_finra" in refresh
    assert "from market_intelligence.equity_eod import ingest_equity_eod" in refresh
    treasury = (Path(__file__).resolve().parents[1] / "market_intelligence" / "ingest_treasury.py").read_text(encoding="utf-8")
    assert "from market_intelligence.treasury_xml import" in treasury
    assert "TREASURY_SOURCE_ID = \"TREASURY\"" in treasury
    assert "openbb" not in treasury.lower()


def test_pinned_openbb_extra_imports_when_installed():
    from importlib.metadata import version
    from market_intelligence.openbb_provider.config import openbb_installed

    if not openbb_installed():
        import pytest

        pytest.skip("requirements-openbb.txt is not installed in this interpreter")
    assert version("openbb") == "4.7.2"
    assert version("openbb-cboe") == "1.6.1"


def test_options_and_vix_probes_are_independent():
    both_off = probe_openbb({})
    assert both_off.options.access_status == adapters.ACCESS_DISABLED
    assert both_off.vix.access_status == adapters.ACCESS_DISABLED
    assert both_off.options.enabled is False
    assert both_off.vix.enabled is False

    opt_only = probe_openbb({"MI_OPENBB_OPTIONS_ENABLED": "1"})
    assert opt_only.options.access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED
    assert opt_only.vix.access_status == adapters.ACCESS_DISABLED
    assert opt_only.vix.enabled is False

    vix_only = probe_openbb({"MI_OPENBB_VIX_ENABLED": "1"})
    assert vix_only.options.access_status == adapters.ACCESS_DISABLED
    assert vix_only.vix.access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED
    assert vix_only.options.enabled is False

    from market_intelligence.openbb_provider.config import options_enabled_from_env, vix_enabled_from_env

    ack_opt = {"MI_OPENBB_OPTIONS_ENABLED": "1", "MI_OPENBB_CBOE_RIGHTS_ACK": "1"}
    assert options_enabled_from_env(ack_opt) is True
    assert vix_enabled_from_env(ack_opt) is False
    ack_vix = {"MI_OPENBB_VIX_ENABLED": "1", "MI_OPENBB_CBOE_RIGHTS_ACK": "1"}
    assert options_enabled_from_env(ack_vix) is False
    assert vix_enabled_from_env(ack_vix) is True

    both_on = probe_openbb({"MI_OPENBB_OPTIONS_ENABLED": "1", "MI_OPENBB_VIX_ENABLED": "1"})
    assert both_on.options.access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED
    assert both_on.vix.access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED

    umbrella = probe_openbb({"MI_OPENBB_ENABLED": "1"})
    assert umbrella.options.access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED
    assert umbrella.vix.access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED

    mixed = probe_openbb({"MI_OPENBB_ENABLED": "1", "MI_OPENBB_OPTIONS_ENABLED": "1"})
    assert mixed.options.access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED
    assert mixed.vix.access_status == adapters.ACCESS_DISABLED
    assert mixed.vix.enabled is False


def test_disabled_openbb_exception_note_is_not_broken():
    from market_intelligence.quote_status import exception_note

    disabled = exception_note(
        {
            "source_id": "OPENBB_CBOE_OPTIONS",
            "access_status": "DISABLED",
            "optional_disabled": True,
            "policy_status": "DISABLED",
        }
    )
    assert "DISABLED" in disabled
    assert "outage" in disabled.lower()
    waiting = exception_note(
        {
            "source_id": "OPENBB_CBOE_VIX",
            "access_status": "ENTITLEMENT_REQUIRED",
            "optional_disabled": True,
            "policy_status": "AWAITING_RIGHTS_ACK",
        }
    )
    assert "AWAITING RIGHTS ACK" in waiting
    assert "outage" in waiting.lower()


def test_intraday_flag_allows_additional_option_snapshots_not_vix():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from market_intelligence.openbb_provider.due import options_due, vix_due

    ny = ZoneInfo("America/New_York")
    now = datetime(2026, 9, 11, 16, 30, tzinfo=ny)
    session = now.date()
    blocked = options_due(last_published_session=session, last_attempt_status="SUCCEEDED", now=now, env={})
    assert blocked["due"] is False
    extra = options_due(
        last_published_session=session,
        last_attempt_status="SUCCEEDED",
        now=now,
        env={"MI_OPENBB_INTRADAY_SNAPSHOTS": "1"},
    )
    assert extra["due"] is True
    assert extra["reason"] == "intraday_additional_snapshot"
    vix = vix_due(last_published_date=session, now=now)
    assert vix["due"] is False


def test_deploy_does_not_install_openbb_extra_while_dormant():
    root = Path(__file__).resolve().parents[1]
    host = (root / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    release = (root / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")
    for text in (host, release):
        assert "MI_OPENBB_INSTALL_EXTRA" in text
        assert "openbb_extra=skipped_dormant" in text
        assert "find_spec('openbb_cboe')" not in text


