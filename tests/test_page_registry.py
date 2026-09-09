"""Single page registry: sidebar, Overview links, and dashboard.py entry point."""

from __future__ import annotations

from pathlib import Path

from market_intelligence.page_registry import (
    NAV_SECTIONS,
    PAGE_BY_ROUTE,
    PAGE_SPECS,
    navigation_active,
    registered_page,
    set_registered_pages,
    specs_by_section,
)


ROOT = Path(__file__).resolve().parents[1]


def test_registry_covers_required_routes_and_sections():
    assert NAV_SECTIONS == ("Overview", "Markets", "Economy", "Research", "System")
    required = {
        "overview",
        "sectors",
        "rates",
        "credit",
        "macro",
        "order_flow",
        "strategy_monitor",
        "data_health",
    }
    assert required <= set(PAGE_BY_ROUTE)
    grouped = specs_by_section()
    assert [spec.title for spec in grouped["Overview"]] == ["Overview"]
    assert {spec.title for spec in grouped["Markets"]} == {"Sectors", "Rates", "Credit", "Order Flow"}
    assert any(spec.default for spec in PAGE_SPECS)
    urls = [spec.url_path for spec in PAGE_SPECS]
    assert len(urls) == len(set(urls))
    assert PAGE_BY_ROUTE["sectors"].url_path == "Sector_Rotation_V2"
    assert PAGE_BY_ROUTE["overview"].legacy_path == PAGE_BY_ROUTE["overview"].file_path == "pages/10_Market_Pulse.py"


def test_dashboard_builds_navigation_from_registry():
    source = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert "from market_intelligence.page_registry import" in source
    assert "set_registered_pages" in source
    assert "st.navigation" in source
    titles = {spec.title for spec in PAGE_SPECS} | set(NAV_SECTIONS)
    for label in ("Overview", "Markets", "Economy", "Research", "System", "Legacy FMP comparison", "Morning Brief", "Order Flow"):
        assert label in source or label in titles
    assert "st.Page(" in source


def test_overview_drilldowns_use_registry_not_wrapper_paths():
    source = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    assert 'open_registered_page("sectors", "Open Sectors")' in source
    assert 'open_registered_page("rates", "Open Rates")' in source
    assert 'open_registered_page("credit", "Open Credit")' in source
    assert 'open_registered_page("macro", "Open Macro")' in source
    assert 'open_registered_page("order_flow", "Open Order Flow")' in source
    assert "pages/14_Sector_Rotation_V2.py" not in source
    assert "pages/12_Rates_Curve.py" not in source
    opener = source.split("def open_registered_page", 1)[1].split("\n\n", 1)[0]
    assert "st.caption(label)" not in opener
    assert "except Exception" not in opener


def test_resolve_render_uses_same_callables_as_wrappers():
    from market_intelligence import pages_ui
    from market_intelligence.page_registry import resolve_render

    assert resolve_render(PAGE_BY_ROUTE["rates"], pages_ui=pages_ui) == "pages/12_Rates_Curve.py"
    assert resolve_render(PAGE_BY_ROUTE["sectors"], pages_ui=pages_ui) == "pages/14_Sector_Rotation_V2.py"
    assert resolve_render(PAGE_BY_ROUTE["strategy_monitor"], pages_ui=pages_ui) == "pages/strategy_monitor.py"
    assert PAGE_BY_ROUTE["rates"].file_path == PAGE_BY_ROUTE["rates"].legacy_path


def test_registered_pages_are_explicit_and_empty_until_dashboard_main():
    set_registered_pages({})
    assert navigation_active() is False
    assert registered_page("rates") is None
    set_registered_pages({"rates": object()})
    assert navigation_active() is True
    assert registered_page("rates") is not None
    set_registered_pages({})
