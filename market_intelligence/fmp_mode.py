"""FMP-free / legacy-optional operating mode.

The dashboard and scheduled refresh must boot without ``FMP_API_KEY`` and without
importing legacy provider engines. Legacy bundles remain an explicit opt-in
migration reference until replacement is verified. This module never cancels the
subscription or deletes historical evidence.
"""

from __future__ import annotations

import os
from typing import Mapping


FMP_FREE_ENV = "MI_FMP_FREE"
ALLOW_LEGACY_ENV = "MI_ALLOW_LEGACY_FMP"
EQUITY_PROVIDER_ENV = "MI_EQUITY_PROVIDER"
TREASURY_ENABLED_ENV = "MI_TREASURY_ENABLED"


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def fmp_free_mode(env: Mapping[str, str] | None = None) -> bool:
    """Default on. Set ``MI_FMP_FREE=0`` only for an explicit legacy migration window."""
    environ = os.environ if env is None else env
    raw = environ.get(FMP_FREE_ENV)
    if raw is None or str(raw).strip() == "":
        return True
    return _truthy(raw)


def legacy_fmp_enabled(env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    if fmp_free_mode(environ) and not _truthy(environ.get(ALLOW_LEGACY_ENV)):
        return False
    return _truthy(environ.get(ALLOW_LEGACY_ENV))


def equity_provider_name(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    return str(environ.get(EQUITY_PROVIDER_ENV) or "").strip().lower() or "unavailable"


def treasury_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Official Treasury XML is on by default. Set ``MI_TREASURY_ENABLED=0`` to skip."""
    environ = os.environ if env is None else env
    raw = environ.get(TREASURY_ENABLED_ENV)
    if raw is None or str(raw).strip() == "":
        return True
    return _truthy(raw)


__all__ = [
    "ALLOW_LEGACY_ENV",
    "EQUITY_PROVIDER_ENV",
    "FMP_FREE_ENV",
    "TREASURY_ENABLED_ENV",
    "equity_provider_name",
    "fmp_free_mode",
    "legacy_fmp_enabled",
    "treasury_enabled",
]
