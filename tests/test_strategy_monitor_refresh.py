"""Strategy Monitor refresh architecture: fragments, not browser reloads."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent
MONITOR = (ROOT / "pages" / "strategy_monitor.py").read_text(encoding="utf-8")
UI = (ROOT / "qc_research" / "monitor_ui.py").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
CONFIG = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
CRON = (ROOT / "scripts" / "install_backtest_sync_cron.sh").read_text(encoding="utf-8")
SYNC = (ROOT / "jobs" / "sync_quantconnect.py").read_text(encoding="utf-8")
DOCS = (ROOT / "docs" / "STAGE1_RESEARCH_MONITOR.md").read_text(encoding="utf-8")


def test_monitor_has_no_browser_reload():
    assert "window.parent.location.reload" not in MONITOR
    assert "window.location.reload" not in MONITOR
    assert "location.reload" not in MONITOR
    assert "location.reload()" not in MONITOR
    assert "meta http-equiv" not in MONITOR.lower()
    assert "st_autorefresh" not in MONITOR
    assert "experimental_rerun" not in MONITOR


def test_monitor_does_not_import_components_for_refresh():
    assert "streamlit.components.v1" not in MONITOR
    assert "components.html" not in MONITOR
    assert "import streamlit.components" not in MONITOR


def test_monitor_still_renders_research_sections():
    assert "render_smoke_section" in MONITOR
    assert "render_stage1_section" in MONITOR
    assert "render_backtest_vs_paper" in MONITOR
    assert "STAGE 1 RESEARCH RESULTS" in UI
    assert "### Smoke Tests" in UI
    assert "Walk-Forward" in UI
    assert "FINAL_HOLDOUT" in UI
    assert "PASS/WATCH/FAIL" in UI


def test_strategy_selector_has_stable_key():
    assert 'key="strategy_monitor_selected_strategy"' in MONITOR
    selector = MONITOR.split("st.selectbox(", 1)[1]
    assert "strategy_monitor_selected_strategy" in selector.split(")", 1)[0] or (
        'key="strategy_monitor_selected_strategy"' in MONITOR
    )


def test_live_monitor_uses_streamlit_fragment_timer():
    assert "@st.fragment(run_every=LIVE_MONITOR_REFRESH)" in MONITOR
    assert 'LIVE_MONITOR_REFRESH = "30s"' in MONITOR
    assert "render_live_strategy_monitor_auto" in MONITOR
    assert "@st.fragment" in MONITOR
    assert "render_live_strategy_monitor_manual" in MONITOR
    assert MONITOR.count("@st.fragment") == 2
    assert "st.rerun" not in MONITOR
    assert 'key="strategy_monitor_refresh"' in MONITOR
    assert 'key="strategy_monitor_auto_refresh"' in MONITOR
    assert "st.checkbox" in MONITOR
    assert "Auto refresh" in MONITOR


def test_exactly_one_frontend_refresh_mechanism():
    assert MONITOR.count("run_every") == 1
    assert "setTimeout" not in MONITOR
    assert "setInterval" not in MONITOR
    assert "time.sleep" not in MONITOR
    assert "while True" not in MONITOR


def test_fragment_does_not_trigger_research_pipeline_or_qc_sync():
    fragment_fn = MONITOR.split("def _query_live_monitor_data", 1)[1].split(
        "def _render_refresh_debug", 1
    )[0]
    assert "sync_quantconnect" not in fragment_fn
    assert "jobs.sync_quantconnect" not in MONITOR
    assert "create_backtest" not in MONITOR
    assert "/backtests/create" not in MONITOR
    assert "FINAL_HOLDOUT" not in MONITOR
    assert "load_latest_snapshot" in fragment_fn
    assert "load_backtests" in fragment_fn
    assert "Never calls QuantConnect" in fragment_fn


def test_backtests_only_cron_remains_operational():
    assert "jobs.sync_quantconnect --backtests-only" in CRON
    assert "flock -n" in CRON
    assert "* * * * *" in CRON
    assert "--backtests-only" in SYNC
    assert "folderWatchBlacklist" in CONFIG
    assert '"outputs"' in CONFIG or "'outputs'" in CONFIG


def test_streamlit_minimum_supports_stable_fragments():
    match = re.search(r"streamlit>=([0-9]+)\.([0-9]+)", REQUIREMENTS)
    assert match, REQUIREMENTS
    major, minor = int(match.group(1)), int(match.group(2))
    assert (major, minor) >= (1, 37)


def test_monitor_does_not_dispose_engine_or_call_sync():
    assert "engine.dispose" not in MONITOR
    assert "import jobs" not in MONITOR
    assert "from jobs" not in MONITOR
    assert "STREAMLIT_REFRESH_DEBUG" in MONITOR


def test_docs_describe_decoupled_sync_and_fragment_refresh():
    assert "Strategy Monitor fragment refresh" in DOCS
    assert "never hard-reloads the browser" in DOCS
    assert "fragment refresh never launches backtests" in DOCS
    assert "jobs.sync_quantconnect --backtests-only" in DOCS


def test_caption_does_not_promise_full_page_reload():
    assert "This page refreshes about every 30 seconds" not in MONITOR
    assert "Live monitor data updates automatically" in MONITOR
    assert "reloading the entire page" not in MONITOR.lower()
