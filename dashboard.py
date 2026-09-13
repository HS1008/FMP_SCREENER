"""Market Intelligence Streamlit entry.

Boots without importing legacy FMP provider engines or requiring the FMP key.
The legacy comparison page is an explicit optional migration reference.
Refresh on MI pages reloads PostgreSQL reads only.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st
from db.dashboard_engine import load_streamlit_env
from market_intelligence.fmp_mode import fmp_free_mode, legacy_fmp_enabled

ROOT = Path(__file__).resolve().parent


def render_legacy_fmp_dashboard() -> None:
    if fmp_free_mode() and not legacy_fmp_enabled():
        st.title("Legacy FMP comparison")
        st.info(
            "Legacy FMP is disabled in FMP-free mode. It remains an optional migration "
            "reference (set MI_ALLOW_LEGACY_FMP=1). Historical evidence is not deleted."
        )
        return
    from legacy_fmp_dashboard import render_legacy_fmp_dashboard as _render

    _render()


def main() -> None:
    load_streamlit_env()
    st.set_page_config(page_title="Market Intelligence", page_icon="📊", layout="wide")
    from market_intelligence import pages_ui
    from market_intelligence.page_registry import (
        NAV_SECTIONS,
        visible_page_specs,
        resolve_render,
        set_registered_pages,
    )

    registered: dict[str, object] = {}
    grouped: dict[str, list] = {section: [] for section in NAV_SECTIONS}
    for spec in visible_page_specs():
        target = resolve_render(
            spec,
            pages_ui=pages_ui,
            dashboard_renders={"render_legacy_fmp_dashboard": render_legacy_fmp_dashboard},
        )
        page = st.Page(target, title=spec.title, url_path=spec.url_path, default=spec.default)
        registered[spec.route_id] = page
        grouped.setdefault(spec.section, []).append(page)
    set_registered_pages(registered)
    navigation = st.navigation(grouped)
    navigation.run()


if __name__ == "__main__":
    main()
