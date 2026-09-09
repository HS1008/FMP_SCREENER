"""Single page registry for sidebar navigation and in-page links.

Streamlit ``st.navigation`` pages, Overview drilldowns, and AppTest wrappers share
these route IDs. Bookmark-preserving ``url_path`` values match the historical
``pages/*.py`` slugs where those pages already existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class PageSpec:
    route_id: str
    title: str
    section: str
    url_path: str
    render_name: str | None = None
    legacy_path: str | None = None
    file_path: str | None = None
    default: bool = False
    source: str = "pages_ui"


PAGE_SPECS: tuple[PageSpec, ...] = (
    PageSpec(
        "overview",
        "Overview",
        "Overview",
        "Market_Pulse",
        render_name="render_market_pulse",
        legacy_path="pages/10_Market_Pulse.py",
        default=True,
    ),
    PageSpec(
        "sectors",
        "Sectors",
        "Markets",
        "Sector_Rotation_V2",
        render_name="render_sector_rotation_v2",
        legacy_path="pages/14_Sector_Rotation_V2.py",
    ),
    PageSpec(
        "rates",
        "Rates",
        "Markets",
        "Rates_Curve",
        render_name="render_rates_curve",
        legacy_path="pages/12_Rates_Curve.py",
    ),
    PageSpec(
        "credit",
        "Credit",
        "Markets",
        "Credit_Overview",
        render_name="render_credit_overview",
        legacy_path="pages/13_Credit_Overview.py",
    ),
    PageSpec(
        "order_flow",
        "Order Flow",
        "Markets",
        "Order_Flow",
        render_name="render_order_flow",
        legacy_path="pages/18_Order_Flow.py",
    ),
    PageSpec(
        "macro",
        "Macro",
        "Economy",
        "Macro_Overview",
        render_name="render_macro_overview",
        legacy_path="pages/11_Macro_Overview.py",
    ),
    PageSpec(
        "strategy_monitor",
        "Strategy Monitor",
        "Research",
        "strategy_monitor",
        file_path="pages/strategy_monitor.py",
        legacy_path="pages/strategy_monitor.py",
        source="file",
    ),
    PageSpec(
        "power_producers",
        "Power Producers",
        "Research",
        "Power_Producer_Watchlist",
        file_path="pages/09_Power_Producer_Watchlist.py",
        legacy_path="pages/09_Power_Producer_Watchlist.py",
        source="file",
    ),
    PageSpec(
        "data_health",
        "Data Health",
        "System",
        "Data_Health",
        render_name="render_data_health",
        legacy_path="pages/15_Data_Health.py",
    ),
    PageSpec(
        "morning_brief",
        "Morning Brief",
        "System",
        "Morning_Context",
        render_name="render_morning_context",
        legacy_path="pages/16_Morning_Context.py",
    ),
    PageSpec(
        "methodology",
        "Methodology",
        "System",
        "PIT_Sector_Internals",
        render_name="render_pit_sector_internals",
        legacy_path="pages/17_PIT_Sector_Internals.py",
    ),
    PageSpec(
        "legacy_fmp",
        "Legacy FMP comparison",
        "System",
        "legacy_fmp",
        render_name="render_legacy_fmp_dashboard",
        source="dashboard",
    ),
)

PAGE_BY_ROUTE: dict[str, PageSpec] = {spec.route_id: spec for spec in PAGE_SPECS}
NAV_SECTIONS: tuple[str, ...] = ("Overview", "Markets", "Economy", "Research", "System")

_REGISTERED_PAGES: dict[str, Any] = {}


def set_registered_pages(pages: dict[str, Any]) -> None:
    """Store the live ``st.Page`` objects created by ``dashboard.main``."""
    _REGISTERED_PAGES.clear()
    _REGISTERED_PAGES.update(pages)


def registered_page(route_id: str) -> Any | None:
    return _REGISTERED_PAGES.get(route_id)


def navigation_active() -> bool:
    return bool(_REGISTERED_PAGES)


def specs_by_section() -> dict[str, list[PageSpec]]:
    grouped: dict[str, list[PageSpec]] = {section: [] for section in NAV_SECTIONS}
    for spec in PAGE_SPECS:
        grouped.setdefault(spec.section, []).append(spec)
    return grouped


def resolve_render(spec: PageSpec, *, pages_ui: Any, dashboard_renders: dict[str, Callable[[], None]] | None = None):
    if spec.file_path:
        return spec.file_path
    if spec.source == "dashboard":
        render = (dashboard_renders or {}).get(spec.render_name or "")
        if render is None:
            raise KeyError("Dashboard render {0} was not provided".format(spec.render_name))
        return render
    if not spec.render_name:
        raise KeyError("Page {0} has no render".format(spec.route_id))
    return getattr(pages_ui, spec.render_name)
