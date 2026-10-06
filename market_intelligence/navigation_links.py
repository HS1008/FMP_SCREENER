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

from market_intelligence.page_registry import PAGE_BY_ROUTE, registered_page

PENDING_SCROLL_KEY = "mi_pending_scroll_anchor"
ORIGIN_KEY = "mi_navigation_origin"
OVERVIEW_ORIGIN = "overview"

_SCROLL_HTML = """
<script>
(function () {
  var anchor = %s;
  var tries = 0;
  var timer = setInterval(function () {
    tries += 1;
    var doc = window.parent.document;
    var target = doc.getElementById(anchor) || doc.querySelector('a[href="#' + anchor + '"]');
    if (target) {
      target.scrollIntoView({behavior: "smooth", block: "start"});
      clearInterval(timer);
    } else if (tries > 40) {
      clearInterval(timer);
    }
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


def apply_pending_scroll() -> None:
    """Scroll the browser to the anchor requested by the previous page, once."""
    anchor = st.session_state.pop(PENDING_SCROLL_KEY, None)
    if not anchor:
        return
    safe = "".join(ch for ch in str(anchor) if ch.isalnum() or ch in "-_")
    if not safe:
        return
    components.html(_SCROLL_HTML % ('"{0}"'.format(safe)), height=0)


def came_from_overview() -> bool:
    return st.session_state.get(ORIGIN_KEY) == OVERVIEW_ORIGIN


def clear_origin() -> None:
    st.session_state.pop(ORIGIN_KEY, None)


__all__ = [
    "ORIGIN_KEY",
    "OVERVIEW_ORIGIN",
    "PENDING_SCROLL_KEY",
    "apply_pending_scroll",
    "came_from_overview",
    "clear_origin",
    "navigate_to",
    "page_target",
]
