"""Streamlit must not make emergency provider calls.

Set STREAMLIT_ALLOW_PROVIDER_FETCH=1 only for an explicit legacy override.
Default is refuse FMP/FRED/FINRA/IBKR/QC from the UI.
"""

from __future__ import annotations

import os


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
