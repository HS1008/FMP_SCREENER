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

