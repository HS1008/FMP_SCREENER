"""OpenBB → Cboe options chains and VX EOD curve.

Acquisition is lazy. Importing this package must not import OpenBB or contact a provider.
"""

from market_intelligence.openbb_provider.config import (
    DEFAULT_OPTIONS_SYMBOLS,
    DEFAULT_SYMBOLS,
    ENABLE_FLAG,
    OPENBB_OPTIONS_SOURCE_ID,
    OPENBB_SOURCE_ID,
    OPENBB_VIX_SOURCE_ID,
    enabled_from_env,
    options_symbols_from_env,
    probe_fields,
    probe_openbb,
)

__all__ = [
    "DEFAULT_OPTIONS_SYMBOLS",
    "DEFAULT_SYMBOLS",
    "ENABLE_FLAG",
    "OPENBB_OPTIONS_SOURCE_ID",
    "OPENBB_SOURCE_ID",
    "OPENBB_VIX_SOURCE_ID",
    "enabled_from_env",
    "options_symbols_from_env",
    "probe_fields",
    "probe_openbb",
]
