"""Streamlit must not make emergency provider calls or mutate the tree.

Set STREAMLIT_ALLOW_PROVIDER_FETCH=1 only for an explicit legacy override.
Default is refuse FMP/FRED/FINRA/IBKR/QC from the UI.

``FMP_STREAMLIT_READONLY=1`` (systemd dashboard identity) skips cache mkdir/writes
so the Streamlit process cannot mutate the checkout or release tree. CLI nightly
refresh does not set that flag and still writes caches.
"""

from __future__ import annotations

import os
from pathlib import Path

STREAMLIT_READONLY_ENV = "FMP_STREAMLIT_READONLY"


def provider_fetch_allowed() -> bool:
    return os.environ.get("STREAMLIT_ALLOW_PROVIDER_FETCH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def refuse_provider_fetch(provider: str) -> None:
    if provider_fetch_allowed():
        return
    raise RuntimeError(
        "{0} fetch from Streamlit is disabled. Backend ingest writes PostgreSQL; "
        "the UI shows stored or precomputed data with freshness metadata.".format(provider)
    )


def streamlit_filesystem_write_allowed() -> bool:
    """False in the dashboard process; True for CLI ingest and cache refresh."""
    return os.environ.get(STREAMLIT_READONLY_ENV, "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def ensure_streamlit_cache_dir(path: Path) -> Path:
    """Return ``path``; create it only when the Streamlit process is allowed to write."""
    if streamlit_filesystem_write_allowed():
        path.mkdir(parents=True, exist_ok=True)
    return path
