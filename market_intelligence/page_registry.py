"""Single page registry for sidebar navigation and in-page links.

Streamlit ``st.navigation`` pages, Overview drilldowns, and AppTest wrappers share
these route IDs. Bookmark-preserving ``url_path`` values match the historical
``pages/*.py`` slugs where those pages already existed.

``hidden`` specs are retired sidebar destinations. Their render functions, page
scripts, and data stay in the repository (Power instruments, subsector baskets,
Morning Brief, bond tools), but ``st.navigation`` does not list them and
``open_registered_page`` draws no link to them.
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
    hidden: bool = False


SECTION_MARKETS = "Markets"
SECTION_POSITIONING = "Positioning"
SECTION_ECONOMY = "Economy"
SECTION_RESEARCH = "Research"
SECTION_SYSTEM = "System"
SECTION_RETIRED = "Retired"

PAGE_SPECS: tuple[PageSpec, ...] = (
    PageSpec(
        "overview",
        "Market Overview",
        SECTION_MARKETS,
        "Market_Pulse",
        render_name="render_market_pulse",
        legacy_path="pages/10_Market_Pulse.py",
        file_path="pages/10_Market_Pulse.py",
        default=True,
    ),
    PageSpec(
        "us_markets",
        "US Markets",
        SECTION_MARKETS,
        "US_Markets",
        render_name="render_us_markets",
        legacy_path="pages/22_US_Markets.py",
        file_path="pages/22_US_Markets.py",
    ),
    PageSpec(
        "global_markets",
        "Global Markets",
        SECTION_MARKETS,
        "Global_Markets",
        render_name="render_global_markets",
        legacy_path="pages/23_Global_Markets.py",
        file_path="pages/23_Global_Markets.py",
    ),
    PageSpec(
        "rates",
        "Rates & Curve",
        SECTION_MARKETS,
        "Rates_Curve",
        render_name="render_rates_curve",
        legacy_path="pages/12_Rates_Curve.py",
        file_path="pages/12_Rates_Curve.py",
    ),
    PageSpec(
        "credit",
        "Credit",
        SECTION_MARKETS,
        "Credit_Overview",
        render_name="render_credit_overview",
        legacy_path="pages/13_Credit_Overview.py",
        file_path="pages/13_Credit_Overview.py",
    ),
    PageSpec(
        "options",
        "Options & Volatility",
        SECTION_MARKETS,
        "Options_Volatility",
        render_name="render_options_volatility",
        legacy_path="pages/21_Options_Volatility.py",
        file_path="pages/21_Options_Volatility.py",
    ),
    PageSpec(
        "forex",
        "FOREX",
        SECTION_MARKETS,
        "Forex",
        render_name="render_forex",
        file_path="pages/24_Forex.py",
    ),
    PageSpec(
        "commodities",
        "Commodities",
        SECTION_MARKETS,
        "Commodities",
        render_name="render_commodities",
        legacy_path="pages/20_Commodities.py",
        file_path="pages/20_Commodities.py",
    ),
    PageSpec(
        "crypto",
        "Crypto",
        SECTION_MARKETS,
        "Crypto",
        render_name="render_crypto",
        file_path="pages/26_Crypto.py",
    ),
    PageSpec(
        "positioning",
        "CFTC COT",
        SECTION_POSITIONING,
        "CFTC_COT",
        render_name="render_positioning",
        file_path="pages/25_CFTC_COT.py",
    ),
    PageSpec(
        "macro",
        "Macro & Liquidity",
        SECTION_ECONOMY,
        "Macro_Overview",
        render_name="render_macro_overview",
        legacy_path="pages/11_Macro_Overview.py",
        file_path="pages/11_Macro_Overview.py",
    ),
    PageSpec(
        "strategy_monitor",
        "Strategy Monitor",
        SECTION_RESEARCH,
        "strategy_monitor",
        file_path="pages/strategy_monitor.py",
        legacy_path="pages/strategy_monitor.py",
        source="file",
    ),
    PageSpec(
        "data_health",
        "Data Health",
        SECTION_SYSTEM,
        "Data_Health",
        render_name="render_data_health",
        legacy_path="pages/15_Data_Health.py",
        file_path="pages/15_Data_Health.py",
    ),
    PageSpec(
        "legacy_fmp",
        "Legacy FMP comparison",
        SECTION_SYSTEM,
        "legacy_fmp",
        render_name="render_legacy_fmp_dashboard",
        source="dashboard",
    ),
    # ---- retired sidebar entries (code and data retained; not navigable) ----
    PageSpec(
        "sectors",
        "Equities & Sectors",
        SECTION_RETIRED,
        "Sector_Rotation_V2",
        render_name="render_sector_rotation_v2",
        legacy_path="pages/14_Sector_Rotation_V2.py",
        file_path="pages/14_Sector_Rotation_V2.py",
        hidden=True,
    ),
    PageSpec(
        "order_flow",
        "Bond Trading Activity",
        SECTION_RETIRED,
        "Order_Flow",
        render_name="render_order_flow",
        legacy_path="pages/18_Order_Flow.py",
        file_path="pages/18_Order_Flow.py",
        hidden=True,
    ),
    PageSpec(
        "fixed_income",
        "Bond Research",
        SECTION_RETIRED,
        "Fixed_Income",
        render_name="render_fixed_income",
        legacy_path="pages/19_Fixed_Income.py",
        file_path="pages/19_Fixed_Income.py",
        hidden=True,
    ),
    PageSpec(
        "power_producers",
        "Power Producers",
        SECTION_RETIRED,
        "Power_Producer_Watchlist",
        file_path="pages/09_Power_Producer_Watchlist.py",
        legacy_path="pages/09_Power_Producer_Watchlist.py",
        source="file",
        hidden=True,
    ),
    PageSpec(
        "morning_brief",
        "Morning Brief",
        SECTION_RETIRED,
        "Morning_Context",
        render_name="render_morning_context",
        legacy_path="pages/16_Morning_Context.py",
        file_path="pages/16_Morning_Context.py",
        hidden=True,
    ),
    PageSpec(
        "methodology",
        "Methodology",
        SECTION_RETIRED,
        "PIT_Sector_Internals",
        render_name="render_pit_sector_internals",
        legacy_path="pages/17_PIT_Sector_Internals.py",
        file_path="pages/17_PIT_Sector_Internals.py",
        hidden=True,
    ),
)

RETIRED_ROUTE_IDS: tuple[str, ...] = tuple(spec.route_id for spec in PAGE_SPECS if spec.hidden)


def visible_page_specs() -> tuple[PageSpec, ...]:
    """Specs ``dashboard.main`` registers with ``st.navigation`` (sidebar order)."""
    from market_intelligence.fmp_mode import legacy_fmp_enabled

    shown = tuple(spec for spec in PAGE_SPECS if not spec.hidden)
    if legacy_fmp_enabled():
        return shown
    return tuple(spec for spec in shown if spec.route_id != "legacy_fmp")


PAGE_BY_ROUTE: dict[str, PageSpec] = {spec.route_id: spec for spec in PAGE_SPECS}
NAV_SECTIONS: tuple[str, ...] = (
    SECTION_MARKETS,
    SECTION_POSITIONING,
    SECTION_ECONOMY,
    SECTION_RESEARCH,
    SECTION_SYSTEM,
)

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
    """Sidebar groups. Retired (hidden) specs are not listed."""
    grouped: dict[str, list[PageSpec]] = {section: [] for section in NAV_SECTIONS}
    for spec in PAGE_SPECS:
        if spec.hidden:
            continue
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
