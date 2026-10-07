"""Programmatic navigation from the Market Overview into a full page and subsection.

``st.navigation`` owns the sidebar; this module only switches to a registered page
after priming the session state that page reads (for example the subsector
selector on US Markets) and remembering an in-page anchor to scroll to. Scrolling
is applied once by ``dashboard.main`` after the target page has rendered.
"""

from __future__ import annotations

from typing import Any, Mapping

import streamlit as st
import streamlit.components.v1 as components

from market_intelligence.ibkr_live_universe import SECTOR_ETFS
from market_intelligence.page_registry import PAGE_BY_ROUTE, registered_page
from market_intelligence.taxonomy import SECTOR_PROXIES

PENDING_SCROLL_KEY = "mi_pending_scroll_anchor"
ORIGIN_KEY = "mi_navigation_origin"
OVERVIEW_ORIGIN = "overview"
US_MARKETS_ORIGIN = "us_markets"

# The subsector heatmap lives on US Markets. Its selector is keyed by the display
# group label ("Tech", "Financials", ...). Navigation resolves a sector by its
# stable identifiers (sector ETF symbol or canonical sector name), never by the
# heatmap's rendered row text.
SUBSECTOR_ROUTE_ID = "us_markets"
SUBSECTOR_ANCHOR = "subsector-performance"
SUBSECTOR_SELECTOR_KEY = "us_subsector_sector"
_GROUP_BY_ETF: dict[str, str] = {symbol: label for symbol, label in SECTOR_ETFS}
_GROUP_BY_SECTOR: dict[str, str] = {
    sector: _GROUP_BY_ETF.get(etf, sector) for sector, etf in SECTOR_PROXIES.items()
}
_GROUP_LABELS: frozenset[str] = frozenset(_GROUP_BY_ETF.values())

_SCROLL_NONCE_KEY = "mi_pending_scroll_nonce"

_SCROLL_HTML = """
<script>
(function () {
  // request %s
  var anchor = %s;
  var tries = 0;
  var settled = 0;
  var lastTop = null;
  var timer = setInterval(function () {
    tries += 1;
    var doc = window.parent.document;
    var target = doc.getElementById(anchor) || doc.querySelector('a[href="#' + anchor + '"]');
    if (!target) {
      if (tries > 40) { clearInterval(timer); }
      return;
    }
    // Charts above the anchor keep growing for a few seconds after first paint,
    // so re-pin the heading until the layout has stopped moving it.
    var top = Math.round(target.getBoundingClientRect().top);
    if (top === lastTop) {
      settled += 1;
    } else {
      target.scrollIntoView({behavior: "auto", block: "start"});
      settled = 0;
      lastTop = Math.round(target.getBoundingClientRect().top);
    }
    if (settled > 8 || tries > 60) { clearInterval(timer); }
  }, 150);
})();
</script>
"""


def page_target(route_id: str) -> Any:
    """The live ``st.Page`` for ``route_id``, or its script path when navigation is not active."""
    page = registered_page(route_id)
    if page is not None:
        return page
    spec = PAGE_BY_ROUTE[route_id]
    return spec.file_path or spec.url_path


def navigate_to(route_id: str, *, anchor: str | None = None, state: Mapping[str, Any] | None = None, origin: str = OVERVIEW_ORIGIN) -> None:
    """Prime ``state``, remember ``anchor``, and switch to the registered page."""
    for key, value in (state or {}).items():
        st.session_state[key] = value
    if anchor:
        st.session_state[PENDING_SCROLL_KEY] = anchor
    else:
        st.session_state.pop(PENDING_SCROLL_KEY, None)
    st.session_state[ORIGIN_KEY] = origin
    st.switch_page(page_target(route_id))


def subsector_group_for(sector_id: Any) -> str | None:
    """Subsector display group for a sector ETF symbol, canonical sector, or group label.

    Returns None for anything that is not a tracked sector so a stray click
    never changes the selector.
    """
    text = str(sector_id or "").strip()
    if not text:
        return None
    if text.upper() in _GROUP_BY_ETF:
        return _GROUP_BY_ETF[text.upper()]
    if text in _GROUP_BY_SECTOR:
        return _GROUP_BY_SECTOR[text]
    if text in _GROUP_LABELS:
        return text
    return None


def subsector_drill(sector_id: Any) -> dict[str, Any] | None:
    """Navigation payload for the subsector heatmap of ``sector_id`` (None when unknown)."""
    group = subsector_group_for(sector_id)
    if group is None:
        return None
    return {
        "route_id": SUBSECTOR_ROUTE_ID,
        "anchor": SUBSECTOR_ANCHOR,
        "state": {SUBSECTOR_SELECTOR_KEY: group},
    }


def select_subsector(sector_id: Any) -> bool:
    """Same-page drill: select the sector in the subsector heatmap and request one scroll.

    Call this before the subsector selector widget is instantiated in the run.
    The scroll request is consumed by the next ``apply_pending_scroll``.
    """
    drill = subsector_drill(sector_id)
    if drill is None:
        return False
    for key, value in drill["state"].items():
        st.session_state[key] = value
    st.session_state[PENDING_SCROLL_KEY] = drill["anchor"]
    return True


def drill_to_subsector(sector_id: Any, *, origin: str = OVERVIEW_ORIGIN) -> bool:
    """Cross-page drill: prime the selector, remember the scroll, and switch to US Markets."""
    drill = subsector_drill(sector_id)
    if drill is None:
        return False
    navigate_to(drill["route_id"], anchor=drill["anchor"], state=drill["state"], origin=origin)
    return True


def apply_pending_scroll() -> None:
    """Scroll the browser to the anchor requested by the previous page, once."""
    anchor = st.session_state.pop(PENDING_SCROLL_KEY, None)
    if not anchor:
        return
    safe = "".join(ch for ch in str(anchor) if ch.isalnum() or ch in "-_")
    if not safe:
        return
    # Identical iframe markup is not re-executed by the frontend, so a repeated
    # request for the same anchor (clicking the same sector twice) needs a
    # distinct body to scroll again.
    nonce = int(st.session_state.get(_SCROLL_NONCE_KEY, 0)) + 1
    st.session_state[_SCROLL_NONCE_KEY] = nonce
    components.html(_SCROLL_HTML % (nonce, '"{0}"'.format(safe)), height=0)


def came_from_overview() -> bool:
    return st.session_state.get(ORIGIN_KEY) == OVERVIEW_ORIGIN


def clear_origin() -> None:
    st.session_state.pop(ORIGIN_KEY, None)


__all__ = [
    "ORIGIN_KEY",
    "OVERVIEW_ORIGIN",
    "PENDING_SCROLL_KEY",
    "SUBSECTOR_ANCHOR",
    "SUBSECTOR_ROUTE_ID",
    "SUBSECTOR_SELECTOR_KEY",
    "US_MARKETS_ORIGIN",
    "apply_pending_scroll",
    "came_from_overview",
    "clear_origin",
    "drill_to_subsector",
    "navigate_to",
    "page_target",
    "select_subsector",
    "subsector_drill",
    "subsector_group_for",
]
