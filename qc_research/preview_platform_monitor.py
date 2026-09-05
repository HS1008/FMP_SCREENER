"""Render Platform Research from a canonical artifact. No QC. No Postgres required."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from qc_research.ml_monitor_ui import render_platform_view
from qc_research.platform_ingest import monitor_view_from_artifacts, normalize_platform_file

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "qc_research" / "platform_artifacts" / "tlt_duration_momentum.json"

st.set_page_config(page_title="Platform Research preview", layout="wide")
st.header("PLATFORM RESEARCH")
st.caption("Artifact preview. This page does not call QuantConnect or PostgreSQL.")
path = Path(st.text_input("Canonical artifact", str(DEFAULT)))
if not path.is_file():
    st.error("Artifact file is missing")
    st.stop()
view = monitor_view_from_artifacts(normalize_platform_file(path))
if view is None:
    st.error("Monitor view is empty")
    st.stop()
render_platform_view(view)
