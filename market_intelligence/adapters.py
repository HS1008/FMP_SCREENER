"""External adapters that are present but not active: IBKR, FINRA TRACE, SEC EDGAR.

Each adapter exposes ``probe(env)`` (a machine-readable access status that the refresh job
writes to ``mi_source_registry.access_status``) and a ``fetch`` surface that raises
``AdapterDisabled`` unless the adapter is both configured and explicitly enabled for this
process. None of these adapters is invoked by the scheduled refresh; they exist so the Data
Health page and capability matrix report the truth (disabled/configuration required) instead of
implying coverage that does not exist. There is no order-routing surface anywhere in here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

ACCESS_DISABLED = "DISABLED"
ACCESS_CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
ACCESS_CONFIGURED = "CONFIGURED"
ACCESS_AVAILABLE = "AVAILABLE"
ACCESS_ENTITLEMENT_REQUIRED = "ENTITLEMENT_REQUIRED"
ACCESS_TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"

IBKR_SOURCE_ID = "IBKR_MARKET_DATA"
TRACE_SOURCE_ID = "FINRA_TRACE"
EDGAR_SOURCE_ID = "SEC_EDGAR"

EDGAR_BASE_URL = "https://data.sec.gov"
EDGAR_MAX_REQUESTS_PER_SECOND = 10  # SEC fair-access ceiling; we stay well under it.


class AdapterDisabled(RuntimeError):
    """Raised when a fetch is attempted against an adapter that is not enabled."""

    def __init__(self, source_id: str, status: str, reason: str) -> None:
        super().__init__("{0}: {1} ({2})".format(source_id, status, reason))
        self.source_id = source_id
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class AdapterStatus:
    source_id: str
    access_status: str
    enabled: bool
    reason: str
    required_configuration: tuple[str, ...] = ()
    capabilities: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["required_configuration"] = list(self.required_configuration)
        return payload


def _flag(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


# ---- IBKR ----------------------------------------------------------------------------------------------


class IBKRMarketDataAdapter:
    """Interface only. Quotes would need a running TWS/Gateway session and market-data entitlements.

    Deliberately has no order, account, or position methods.
    """

    source_id = IBKR_SOURCE_ID
    ENABLE_FLAG = "MI_IBKR_MARKET_DATA_ENABLED"
    CAPABILITIES = {"quotes": "windows-local delayed/live snapshot via TWS", "bars": "daily EOD via Windows collector fetch-eod (ADJUSTED_LAST); not a server TWS socket", "orders": "never"}

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        return AdapterStatus(
            source_id=self.source_id,
            access_status=ACCESS_DISABLED,
            enabled=False,
            reason="server-side TWS fetch is disabled; quotes are produced by the Windows-local collector against the user's existing TWS session. This host never opens a TWS socket. Orders are never implemented.",
            required_configuration=("existing TWS session on the collector host", "market data entitlement", "IBKR ingest API"),
            capabilities=dict(self.CAPABILITIES),
        )

    def fetch_quotes(self, symbols: list[str], *, env: Mapping[str, str]) -> list[dict[str, Any]]:
        status = self.probe(env)
        raise AdapterDisabled(self.source_id, status.access_status, status.reason)


# ---- FINRA TRACE -------------------------------------------------------------------------------------------


class TraceAdapter:
    """FINRA Query API aggregates plus honest status for individual TRACE trades.

    Individual TRACE prints are a separate TRAQS/TRACE API product and are not fetched here.
    """

    source_id = TRACE_SOURCE_ID
    ENABLE_FLAG = "MI_TRACE_ENABLED"
    QUERY_ENABLE_FLAG = "MI_FINRA_ENABLED"
    CREDENTIAL_ENV = ("FINRA_CLIENT_ID", "FINRA_CLIENT_SECRET", "FINRA_API_CLIENT_ID", "FINRA_API_CLIENT_SECRET")
    CAPABILITIES = {
        "aggregates": "FINRA Query API corporateMarketBreadth/Sentiment and corporatesAndAgenciesCappedVolume",
        "trades": "not available on Query API; TRACE API/TRAQS is a separate entitlement",
        "volume_caps": "preserved as reported/capped source fields",
        "corrections": "aggregate revisions stored; individual prints not ingested",
    }

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        from market_intelligence.finra_client import configured_from_env, enabled_from_env

        query_configured = configured_from_env(env)
        query_enabled = enabled_from_env(env) or query_configured
        missing = []
        if not env.get("FINRA_CLIENT_ID") and not env.get("FINRA_API_CLIENT_ID"):
            missing.append("FINRA_CLIENT_ID or FINRA_API_CLIENT_ID")
        if not env.get("FINRA_CLIENT_SECRET") and not env.get("FINRA_API_CLIENT_SECRET"):
            missing.append("FINRA_CLIENT_SECRET or FINRA_API_CLIENT_SECRET")
        if not query_configured:
            if not _flag(env, self.ENABLE_FLAG) and not _flag(env, self.QUERY_ENABLE_FLAG):
                status, reason, enabled = ACCESS_DISABLED, "FINRA Query API not enabled and credentials are absent", False
            else:
                status, reason, enabled = ACCESS_CONFIGURATION_REQUIRED, "missing {0}".format(", ".join(missing)), False
        else:
            status, reason, enabled = ACCESS_CONFIGURED, "Query API credentials present; dataset entitlement is recorded per-dataset after a probe/ingest", query_enabled
        return AdapterStatus(self.source_id, status, enabled, reason, ("FINRA_CLIENT_ID", "FINRA_CLIENT_SECRET", self.ENABLE_FLAG, self.QUERY_ENABLE_FLAG), dict(self.CAPABILITIES))

    def fetch_trades(self, cusips: list[str], *, env: Mapping[str, str]) -> list[dict[str, Any]]:
        status = self.probe(env)
        raise AdapterDisabled(self.source_id, ACCESS_ENTITLEMENT_REQUIRED, "individual TRACE transactions are not a Query API dataset; TRAQS/TRACE API is out of this adapter")


# ---- SEC EDGAR -----------------------------------------------------------------------------------------------


class EdgarAdapter:
    """Reference-data adapter for SEC EDGAR JSON endpoints (submissions / company facts).

    Public and free, but the SEC requires a descriptive ``User-Agent`` with contact details and
    caps request rate. Fetching is gated behind ``MI_EDGAR_ENABLED`` so nothing touches sec.gov
    unless an operator opted in; the scheduled refresh never calls this adapter.
    """

    source_id = EDGAR_SOURCE_ID
    ENABLE_FLAG = "MI_EDGAR_ENABLED"
    USER_AGENT_ENV = "SEC_USER_AGENT"
    CAPABILITIES = {"submissions": "available when enabled", "company_facts": "available when enabled", "bond_terms": "not derivable from EDGAR JSON; prospectus parsing is out of scope"}

    def __init__(self, *, opener=None, min_interval_s: float = 1.0 / EDGAR_MAX_REQUESTS_PER_SECOND * 2, timeout_s: float = 20.0) -> None:
        self._opener = opener or urllib.request.urlopen
        self._min_interval = min_interval_s
        self._timeout = timeout_s
        self._last_request = 0.0

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        agent = str(env.get(self.USER_AGENT_ENV, "")).strip()
        enabled_flag = _flag(env, self.ENABLE_FLAG)
        if not agent or "@" not in agent:
            status, reason, enabled = ACCESS_CONFIGURATION_REQUIRED, "{0} must be set to 'Org Name contact@example.com' per SEC fair-access policy".format(self.USER_AGENT_ENV), False
        elif not enabled_flag:
            status, reason, enabled = ACCESS_DISABLED, "{0} not set; EDGAR reference fetches are opt-in".format(self.ENABLE_FLAG), False
        else:
            status, reason, enabled = ACCESS_CONFIGURED, "user agent present and adapter enabled", True
        return AdapterStatus(self.source_id, status, enabled, reason, (self.USER_AGENT_ENV, self.ENABLE_FLAG), dict(self.CAPABILITIES))

    @staticmethod
    def normalize_cik(cik: str | int) -> str:
        digits = "".join(ch for ch in str(cik) if ch.isdigit())
        if not digits or len(digits) > 10:
            raise ValueError("invalid CIK {0!r}".format(cik))
        return digits.zfill(10)

    def _get_json(self, path: str, *, env: Mapping[str, str]) -> dict[str, Any]:
        status = self.probe(env)
        if not status.enabled:
            raise AdapterDisabled(self.source_id, status.access_status, status.reason)
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(EDGAR_BASE_URL + path, headers={"User-Agent": env[self.USER_AGENT_ENV], "Accept": "application/json", "Host": "data.sec.gov"})
        self._last_request = time.monotonic()
        try:
            with self._opener(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError("EDGAR {0} returned HTTP {1}".format(path, exc.code)) from None
        except urllib.error.URLError as exc:
            raise RuntimeError("EDGAR {0} unreachable: {1}".format(path, exc.reason.__class__.__name__ if not isinstance(exc.reason, str) else exc.reason)) from None
        return json.loads(body)

    def submissions(self, cik: str | int, *, env: Mapping[str, str]) -> dict[str, Any]:
        return self._get_json("/submissions/CIK{0}.json".format(self.normalize_cik(cik)), env=env)

    def company_facts(self, cik: str | int, *, env: Mapping[str, str]) -> dict[str, Any]:
        return self._get_json("/api/xbrl/companyfacts/CIK{0}.json".format(self.normalize_cik(cik)), env=env)


ADAPTERS = (IBKRMarketDataAdapter(), TraceAdapter(), EdgarAdapter())


def probe_all(env: Mapping[str, str]) -> dict[str, AdapterStatus]:
    """Access status per external source, keyed by source_id (used by the refresh job and docs)."""
    return {adapter.source_id: adapter.probe(env) for adapter in ADAPTERS}


__all__ = [
    "ACCESS_AVAILABLE",
    "ACCESS_CONFIGURATION_REQUIRED",
    "ACCESS_CONFIGURED",
    "ACCESS_DISABLED",
    "ACCESS_ENTITLEMENT_REQUIRED",
    "ACCESS_TEMPORARILY_UNAVAILABLE",
    "ADAPTERS",
    "AdapterDisabled",
    "AdapterStatus",
    "EdgarAdapter",
    "IBKRMarketDataAdapter",
    "TraceAdapter",
    "probe_all",
]
