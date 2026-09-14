"""Flags, versions, and permitted-use notes for the OpenBB Cboe slice."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any, Mapping

from market_intelligence.catalog import EXPORT_INTERNAL_ONLY

OPENBB_SOURCE_ID = "OPENBB_CBOE"
OPENBB_OPTIONS_SOURCE_ID = "OPENBB_CBOE_OPTIONS"
OPENBB_VIX_SOURCE_ID = "OPENBB_CBOE_VIX"
ENABLE_FLAG = "MI_OPENBB_ENABLED"
OPTIONS_ENABLE_FLAG = "MI_OPENBB_OPTIONS_ENABLED"
VIX_ENABLE_FLAG = "MI_OPENBB_VIX_ENABLED"
OPTIONS_RIGHTS_ACK_FLAG = "MI_OPENBB_OPTIONS_RIGHTS_ACK"
VIX_RIGHTS_ACK_FLAG = "MI_OPENBB_VIX_RIGHTS_ACK"
# Legacy umbrella. Does not authorize OPTIONS or VIX. Kept so old env files fail closed.
LEGACY_RIGHTS_ACK_FLAG = "MI_OPENBB_CBOE_RIGHTS_ACK"
RIGHTS_ACK_FLAG = LEGACY_RIGHTS_ACK_FLAG
SYMBOLS_ENV = "MI_OPENBB_OPTIONS_SYMBOLS"
TIMEOUT_ENV = "MI_OPENBB_TIMEOUT_SEC"
TIMEOUT_ENV_ALIASES = ("MI_OPENBB_TIMEOUT_S",)
BUDGET_ENV = "MI_OPENBB_DEADLINE_SEC"
BUDGET_ENV_ALIASES = ("MI_OPENBB_OPERATION_BUDGET_S",)
RETRIES_ENV = "MI_OPENBB_MAX_RETRIES"
RETRIES_ENV_ALIASES = ("MI_OPENBB_RETRIES",)
INTRADAY_FLAG = "MI_OPENBB_INTRADAY_SNAPSHOTS"
RETENTION_CONTRACTS_ENV = "MI_OPENBB_KEEP_CONTRACT_SNAPSHOTS"
RETENTION_SUMMARY_ENV = "MI_OPENBB_KEEP_SUMMARY_SNAPSHOTS"

DEFAULT_OPTIONS_SYMBOLS = ("SPY", "QQQ", "IWM")
DEFAULT_SYMBOLS = DEFAULT_OPTIONS_SYMBOLS
DEFAULT_TIMEOUT_S = 45.0
DEFAULT_OPERATION_BUDGET_S = 180.0
DEFAULT_RETRIES = 2
KEEP_CONTRACT_SNAPSHOTS = 30
KEEP_SUMMARY_SNAPSHOTS = 400

NORMALIZATION_VERSION = "openbb_cboe_options_v1"
ANALYTICS_VERSION = "options_vol_metrics_v1"
GEX_METHOD_ID = "GEX_PROXY"
GEX_METHOD_VERSION = "gex_proxy_v1"
GEX_SIGN_CONVENTION = "CALL_PLUS_PUT_MINUS_V1"
GAMMA_CONVENTION = "GAMMA_PER_ONE_DOLLAR_V1"
MULTIPLIER_RULE = "STANDARD_EQUITY_100_V1"
ATM_RULE = "SPOT_ATM_ABS_LOG_MONEYNESS_V1"
IV_TIME_BASIS = "CALENDAR_365_25_V1"
VIX_ANALYTICS_VERSION = "vx_eod_front_curve_v1"
FLAT_TOLERANCE_POINTS = 0.05

PROVIDER_ID = "cboe"
CHAIN_ENDPOINT = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
VIX_LEVEL_TYPE = "CBOE_VX_EOD_4PM_ET"
COVERAGE_NOTE = (
    "Cboe delayed-quote JSON via OpenBB. Not consolidated real-time OPRA coverage. "
    "Not an official settlement feed."
)

ATTRIBUTION = (
    "Options and VIX EOD curve retrieved through OpenBB from Cboe delayed / EOD public "
    "endpoints. Delayed data. Not a live quote and not labelled as official settlement."
)
CBOE_ATTRIBUTION = ATTRIBUTION
TERMS_NOTES = (
    "Cboe website Terms (https://www.cboe.com/en/terms/, last updated 2022-11-16) allow viewing "
    "and downloading one copy for personal non-commercial use. They prohibit storing Materials in "
    "an electronic retrieval system and creating derivative works without prior written consent via "
    "https://www.cboe.com/en/use-of-content/ (permissions@cboe.com). Project governance is not a "
    "Cboe licence. OPTIONS collection requires MI_OPENBB_OPTIONS_RIGHTS_ACK after Cboe consent; "
    "VIX/CFE collection requires MI_OPENBB_VIX_RIGHTS_ACK after a CFE Data Agreement. No API key "
    "does not make this PUBLIC."
)
CBOE_TERMS_NOTES = TERMS_NOTES

SOURCE_REGISTRY_ENTRY = {
    "source_id": OPENBB_OPTIONS_SOURCE_ID,
    "provider": "OpenBB / Cboe delayed quotes",
    "dataset": "cboe_delayed_options_chains",
    "source_url": "https://www.cboe.com/delayed_quotes/",
    "expected_cadence": "D",
    "usage_scope": EXPORT_INTERNAL_ONLY,
    "attribution": ATTRIBUTION,
    "terms_notes": TERMS_NOTES,
    "units_metadata": {"iv": "decimal", "greeks": "decimal", "gex_proxy": "delta_notional_per_1pct"},
}


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    import os

    return os.environ if env is None else env


def options_rights_acked(env: Mapping[str, str] | None = None) -> bool:
    """Product-specific OPTIONS consent only. The legacy Cboe umbrella does not count."""
    return _truthy(_env(env).get(OPTIONS_RIGHTS_ACK_FLAG))


def vix_rights_acked(env: Mapping[str, str] | None = None) -> bool:
    """Product-specific CFE/VIX agreement only. OPTIONS consent does not count."""
    return _truthy(_env(env).get(VIX_RIGHTS_ACK_FLAG))


def rights_acked(env: Mapping[str, str] | None = None) -> bool:
    """Deprecated. The umbrella flag never authorizes a dataset."""
    _ = env
    return False


def enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    """True only when the umbrella collection flag is on and BOTH product rights exist.

    Prefer the dataset flags. This must not enable one sibling from the other.
    """
    environ = _env(env)
    return _truthy(environ.get(ENABLE_FLAG)) and options_rights_acked(environ) and vix_rights_acked(environ)


def options_enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    environ = _env(env)
    if not options_rights_acked(environ):
        return False
    if _truthy(environ.get(OPTIONS_ENABLE_FLAG)):
        return True
    if _truthy(environ.get(VIX_ENABLE_FLAG)):
        return False
    return _truthy(environ.get(ENABLE_FLAG))


def vix_enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    environ = _env(env)
    if not vix_rights_acked(environ):
        return False
    if _truthy(environ.get(VIX_ENABLE_FLAG)):
        return True
    if _truthy(environ.get(OPTIONS_ENABLE_FLAG)):
        return False
    return _truthy(environ.get(ENABLE_FLAG))


def options_symbols_from_env(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    raw = str(_env(env).get(SYMBOLS_ENV) or "").strip()
    if not raw:
        return DEFAULT_SYMBOLS
    symbols = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    return symbols or DEFAULT_SYMBOLS


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _first_named(env: Mapping[str, str], names: tuple[str, ...], default: float | int, parser):
    for name in names:
        raw = str(env.get(name) or "").strip()
        if raw:
            return parser(env, name, default)
    return default


def timeout_s(env: Mapping[str, str] | None = None) -> float:
    return _first_named(_env(env), (TIMEOUT_ENV,) + TIMEOUT_ENV_ALIASES, DEFAULT_TIMEOUT_S, _float_env)


def operation_budget_s(env: Mapping[str, str] | None = None) -> float:
    return _first_named(_env(env), (BUDGET_ENV,) + BUDGET_ENV_ALIASES, DEFAULT_OPERATION_BUDGET_S, _float_env)


def retry_limit(env: Mapping[str, str] | None = None) -> int:
    return _first_named(_env(env), (RETRIES_ENV,) + RETRIES_ENV_ALIASES, DEFAULT_RETRIES, _int_env)


def keep_contract_snapshots(env: Mapping[str, str] | None = None) -> int:
    return _int_env(_env(env), RETENTION_CONTRACTS_ENV, KEEP_CONTRACT_SNAPSHOTS)


def keep_summary_snapshots(env: Mapping[str, str] | None = None) -> int:
    return _int_env(_env(env), RETENTION_SUMMARY_ENV, KEEP_SUMMARY_SNAPSHOTS)


def intraday_snapshots_enabled(env: Mapping[str, str] | None = None) -> bool:
    return _truthy(_env(env).get(INTRADAY_FLAG))


def openbb_installed() -> bool:
    return importlib.util.find_spec("openbb") is not None


def openbb_importable() -> bool:
    return openbb_installed()


def _dataset_requested(env: Mapping[str, str], dataset_flag: str, sibling_flag: str) -> bool:
    """Dataset flags are independent. An explicit sibling-only enable must not light this source."""
    if _truthy(env.get(dataset_flag)):
        return True
    if _truthy(env.get(sibling_flag)):
        return False
    return _truthy(env.get(ENABLE_FLAG))


def _status(
    source_id: str,
    env: Mapping[str, str],
    *,
    dataset_flag: str,
    sibling_flag: str,
    rights_flag: str,
    rights_ok: bool,
    missing_access: str,
    missing_reason: str,
    capabilities: dict[str, str],
):
    from market_intelligence.adapters import (
        ACCESS_CONFIGURATION_REQUIRED,
        ACCESS_CONFIGURED,
        ACCESS_DISABLED,
        AdapterStatus,
    )

    dataset_on = _dataset_requested(env, dataset_flag, sibling_flag)
    required = (dataset_flag, rights_flag, "requirements-openbb.txt")
    if not rights_ok:
        return AdapterStatus(source_id, missing_access, False, missing_reason, required, capabilities)
    if not dataset_on:
        return AdapterStatus(source_id, ACCESS_DISABLED, False, "{0} is off. Product rights are recorded; recurring collection stays disabled.".format(dataset_flag), required, capabilities)
    if not openbb_installed():
        return AdapterStatus(source_id, ACCESS_CONFIGURATION_REQUIRED, False, "OpenBB is not installed. Install requirements-openbb.txt in the ingestion runtime.", required, capabilities)
    return AdapterStatus(source_id, ACCESS_CONFIGURED, True, "OpenBB present, rights ack recorded, {0}=1. Delayed Cboe data; INTERNAL_ONLY export.".format(dataset_flag), required, capabilities)


@dataclass(frozen=True)
class OpenBBProbe:
    options: Any
    vix: Any


def probe_openbb(env: Mapping[str, str] | None = None) -> OpenBBProbe:
    environ = _env(env)
    return OpenBBProbe(
        options=_status(
            OPENBB_OPTIONS_SOURCE_ID,
            environ,
            dataset_flag=OPTIONS_ENABLE_FLAG,
            sibling_flag=VIX_ENABLE_FLAG,
            rights_flag=OPTIONS_RIGHTS_ACK_FLAG,
            rights_ok=options_rights_acked(environ),
            missing_access="ENTITLEMENT_REQUIRED",
            missing_reason=(
                "{0} is not set. Cboe website Terms require prior written consent before storing "
                "delayed-quotes JSON in PostgreSQL or creating derived analytics. Project governance "
                "is not that consent."
            ).format(OPTIONS_RIGHTS_ACK_FLAG),
            capabilities={"options_chains": "cboe delayed quotes", "export": "INTERNAL_ONLY"},
        ),
        vix=_status(
            OPENBB_VIX_SOURCE_ID,
            environ,
            dataset_flag=VIX_ENABLE_FLAG,
            sibling_flag=OPTIONS_ENABLE_FLAG,
            rights_flag=VIX_RIGHTS_ACK_FLAG,
            rights_ok=vix_rights_acked(environ),
            missing_access="AGREEMENT_REQUIRED",
            missing_reason=(
                "{0} is not set. CFE delayed data requires a Cboe Data Agreement before collection. "
                "OPTIONS consent and project governance do not cover VIX futures."
            ).format(VIX_RIGHTS_ACK_FLAG),
            capabilities={"vix_curve": "cboe VX_EOD", "export": "INTERNAL_ONLY"},
        ),
    )


def probe_fields(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    probe = probe_openbb(env)
    return {
        "access_status": probe.options.access_status,
        "enabled": bool(probe.options.enabled),
        "reason": probe.options.reason,
        "required_configuration": probe.options.required_configuration,
        "capabilities": probe.options.capabilities,
    }


options_enabled = options_enabled_from_env
vix_enabled = vix_enabled_from_env
rights_acknowledged = rights_acked
OPTIONS_ENABLE_ENV = OPTIONS_ENABLE_FLAG
VIX_ENABLE_ENV = VIX_ENABLE_FLAG
OPTIONS_RIGHTS_ACK_ENV = OPTIONS_RIGHTS_ACK_FLAG
VIX_RIGHTS_ACK_ENV = VIX_RIGHTS_ACK_FLAG
RIGHTS_ACK_ENV = RIGHTS_ACK_FLAG
configured_symbols = options_symbols_from_env
