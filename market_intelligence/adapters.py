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
import os
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

ACCESS_DISABLED = "DISABLED"
ACCESS_CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
ACCESS_NOT_CONFIGURED = "NOT_CONFIGURED"
ACCESS_CONFIGURED = "CONFIGURED"
ACCESS_AVAILABLE = "AVAILABLE"
ACCESS_ENTITLEMENT_REQUIRED = "ENTITLEMENT_REQUIRED"
ACCESS_AGREEMENT_REQUIRED = "AGREEMENT_REQUIRED"
ACCESS_PROVIDER_SUPPORT_REQUIRED = "PROVIDER_SUPPORT_REQUIRED"
ACCESS_RIGHTS_PENDING = "RIGHTS_PENDING"
ACCESS_TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
ACCESS_ON_DEMAND = "ON_DEMAND"

IBKR_SOURCE_ID = "IBKR_MARKET_DATA"
TRACE_SOURCE_ID = "FINRA_TRACE"
EDGAR_SOURCE_ID = "SEC_EDGAR"
OPENFIGI_SOURCE_ID = "OPENFIGI"

EDGAR_BASE_URL = "https://data.sec.gov"
EDGAR_ALLOWED_HOSTS = frozenset({"data.sec.gov", "www.sec.gov", "sec.gov"})
# Target ~5 rps internally; never exceed the SEC fair-access ceiling of 10 rps.
EDGAR_MAX_REQUESTS_PER_SECOND = 5
EDGAR_MIN_INTERVAL_S = 1.0 / EDGAR_MAX_REQUESTS_PER_SECOND
EDGAR_MAX_ATTEMPTS = 3
EDGAR_RETRY_BUDGET_S = 30.0
_EDGAR_RATE_LOCK = threading.Lock()
_EDGAR_LAST_REQUEST_MONOTONIC = 0.0


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
    unless an operator opted in. Process-wide rate limiting prevents concurrent client instances
    from exceeding the fair-access budget.
    """

    source_id = EDGAR_SOURCE_ID
    ENABLE_FLAG = "MI_EDGAR_ENABLED"
    USER_AGENT_ENV = "SEC_USER_AGENT"
    CAPABILITIES = {
        "submissions": "available when enabled",
        "company_facts": "available when enabled",
        "bond_terms": "not derivable from EDGAR JSON; prospectus parsing is out of scope",
    }

    def __init__(
        self,
        *,
        opener=None,
        min_interval_s: float = EDGAR_MIN_INTERVAL_S,
        timeout_s: float = 20.0,
        sleeper=None,
        clock=None,
        max_attempts: int = EDGAR_MAX_ATTEMPTS,
        retry_budget_s: float = EDGAR_RETRY_BUDGET_S,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self._min_interval = max(float(min_interval_s), EDGAR_MIN_INTERVAL_S)
        self._timeout = timeout_s
        self._sleeper = sleeper or time.sleep
        self._clock = clock or time.monotonic
        self._max_attempts = max(1, int(max_attempts))
        self._retry_budget_s = float(retry_budget_s)

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        agent = str(env.get(self.USER_AGENT_ENV, "")).strip().strip('"').strip("'")
        enabled_flag = _flag(env, self.ENABLE_FLAG)
        if not agent or "@" not in agent:
            status, reason, enabled = (
                ACCESS_CONFIGURATION_REQUIRED,
                "{0} must be set to 'Org Name contact@example.com' per SEC fair-access policy. A contact email cannot be invented.".format(self.USER_AGENT_ENV),
                False,
            )
        elif not enabled_flag:
            status, reason, enabled = (
                ACCESS_ON_DEMAND,
                "User agent present. EDGAR is on-demand issuer lookup, not a scheduled market-data feed. Set {0}=1 only for an explicit bounded fetch.".format(self.ENABLE_FLAG),
                False,
            )
        else:
            status, reason, enabled = ACCESS_ON_DEMAND, "user agent present and on-demand fetch enabled", True
        return AdapterStatus(self.source_id, status, enabled, reason, (self.USER_AGENT_ENV, self.ENABLE_FLAG), dict(self.CAPABILITIES))

    @staticmethod
    def normalize_cik(cik: str | int) -> str:
        digits = "".join(ch for ch in str(cik) if ch.isdigit())
        if not digits or len(digits) > 10:
            raise ValueError("invalid CIK {0!r}".format(cik))
        return digits.zfill(10)

    @staticmethod
    def _user_agent(env: Mapping[str, str]) -> str:
        return str(env.get(EdgarAdapter.USER_AGENT_ENV, "")).strip().strip('"').strip("'")

    def _acquire_rate_slot(self) -> None:
        global _EDGAR_LAST_REQUEST_MONOTONIC
        with _EDGAR_RATE_LOCK:
            now = self._clock()
            wait = self._min_interval - (now - _EDGAR_LAST_REQUEST_MONOTONIC)
            if wait > 0:
                self._sleeper(wait)
            _EDGAR_LAST_REQUEST_MONOTONIC = self._clock()

    def _get_json(self, path: str, *, env: Mapping[str, str]) -> dict[str, Any]:
        status = self.probe(env)
        if status.access_status == ACCESS_CONFIGURATION_REQUIRED:
            raise AdapterDisabled(self.source_id, status.access_status, status.reason)
        if not status.enabled:
            raise AdapterDisabled(self.source_id, status.access_status, status.reason)
        agent = self._user_agent(env)
        url = EDGAR_BASE_URL + path
        deadline = self._clock() + self._retry_budget_s
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._acquire_rate_slot()
            request = urllib.request.Request(
                url,
                headers={"User-Agent": agent, "Accept": "application/json"},
            )
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    geturl = getattr(response, "geturl", None)
                    if callable(geturl):
                        final_host = urlparse(geturl()).hostname or ""
                        if final_host and final_host.lower() not in EDGAR_ALLOWED_HOSTS:
                            raise RuntimeError("EDGAR redirect left sec.gov hosts; identifying header not forwarded")
                    body = response.read()
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("EDGAR {0} returned invalid JSON".format(path)) from exc
                if not isinstance(payload, dict):
                    raise RuntimeError("EDGAR {0} returned unexpected JSON schema".format(path))
                return payload
            except urllib.error.HTTPError as exc:
                code = int(exc.code)
                if code in {400, 401, 403, 404}:
                    raise RuntimeError("EDGAR {0} returned HTTP {1}".format(path, code)) from None
                last_error = RuntimeError("EDGAR {0} returned HTTP {1}".format(path, code))
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = None
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = None
                if delay is None:
                    delay = min(8.0, (2 ** (attempt - 1)) + random.uniform(0, 0.25))
                if attempt >= self._max_attempts or self._clock() + delay > deadline:
                    raise last_error from None
                self._sleeper(delay)
            except urllib.error.URLError as exc:
                reason = exc.reason.__class__.__name__ if not isinstance(exc.reason, str) else exc.reason
                last_error = RuntimeError("EDGAR {0} unreachable: {1}".format(path, reason))
                delay = min(8.0, (2 ** (attempt - 1)) + random.uniform(0, 0.25))
                if attempt >= self._max_attempts or self._clock() + delay > deadline:
                    raise last_error from None
                self._sleeper(delay)
        raise last_error or RuntimeError("EDGAR {0} failed".format(path))

    def submissions(self, cik: str | int, *, env: Mapping[str, str]) -> dict[str, Any]:
        return self._get_json("/submissions/CIK{0}.json".format(self.normalize_cik(cik)), env=env)

    def company_facts(self, cik: str | int, *, env: Mapping[str, str]) -> dict[str, Any]:
        return self._get_json("/api/xbrl/companyfacts/CIK{0}.json".format(self.normalize_cik(cik)), env=env)


class OpenBBCboeOptionsAdapter:
    """Probe-only surface. Fetch lives in ``openbb_provider`` and is never imported here."""

    source_id = "OPENBB_CBOE_OPTIONS"
    CAPABILITIES = {
        "chains": "Cboe delayed-quote JSON via OpenBB; SPY/QQQ/IWM configurable",
        "coverage": "not consolidated OPRA",
        "export": "INTERNAL_ONLY; website storage needs Cboe written consent, not project governance alone",
    }

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        from market_intelligence.openbb_provider.config import probe_openbb

        return probe_openbb(env).options


class CboeAllAccessAdapter:
    """Direct Cboe LiveVol All Access. Credential presence is not activation. Streamlit never calls this."""

    source_id = "CBOE_ALL_ACCESS"
    ENABLE_FLAG = "MI_CBOE_ENABLED"
    CREDENTIAL_ENV = ("CBOE_CLIENT_ID", "CBOE_CLIENT_SECRET")
    CAPABILITIES = {
        "auth": "OAuth client_credentials at id.livevol.com",
        "vix": "underlying-quotes index levels",
        "vix_term_structure": "VIX index tenors, not futures",
        "spx_skew": "filtered SPX 25-delta snapshot",
        "iv_rv": "iv30 or VIX minus SPX RV20",
    }

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        from market_intelligence.cboe_client import probe_status

        status, reason, enabled = probe_status(env)
        return AdapterStatus(self.source_id, status, enabled, reason, self.CREDENTIAL_ENV + (self.ENABLE_FLAG,), dict(self.CAPABILITIES))


class OpenBBCboeVixAdapter:
    source_id = "OPENBB_CBOE_VIX"
    CAPABILITIES = {
        "curve": "Cboe VX_EOD 4 p.m. ET levels via OpenBB (CFE delayed quotes)",
        "labels": "not official settlement; not live quotes",
        "export": "INTERNAL_ONLY; CFE Data Agreement required before collection",
    }

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        from market_intelligence.openbb_provider.config import probe_openbb

        return probe_openbb(env).vix


class IBKROptionsAdapter:
    """Probe-only. Collection stays off until API OPRA and storage rights are both proven."""

    source_id = "IBKR_OPTIONS"
    ENABLE_FLAG = "MI_IBKR_OPTIONS_ENABLED"
    CAPABILITIES = {
        "geometry": "reqSecDefOptParams + reqContractDetails",
        "nbbo": "not observed on TWS socket API client 73 after OPRA L1 + freeze test",
        "export": "INTERNAL_ONLY; storage rights not confirmed",
    }

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        if _flag(env, self.ENABLE_FLAG):
            return AdapterStatus(
                self.source_id,
                ACCESS_PROVIDER_SUPPORT_REQUIRED,
                False,
                "MI_IBKR_OPTIONS_ENABLED is set but API OPRA NBBO is not proven. Collection stays off.",
                (self.ENABLE_FLAG,),
                dict(self.CAPABILITIES),
            )
        return AdapterStatus(
            self.source_id,
            ACCESS_PROVIDER_SUPPORT_REQUIRED,
            False,
            "Client Portal OPRA L1 is active, but TWS API client 73 still returns 354 with no bid/ask on live, frozen, and delayed. Collection off. INTERNAL_ONLY storage rights are separately pending.",
            (self.ENABLE_FLAG,),
            dict(self.CAPABILITIES),
        )


class IBKROptionsStorageAdapter:
    source_id = "IBKR_OPTIONS_STORAGE"
    CAPABILITIES = {"persistence": "blocked until IBKR/OPRA non-display snapshot rights are explicit"}

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        return AdapterStatus(
            self.source_id,
            ACCESS_RIGHTS_PENDING,
            False,
            "Paid OPRA L1 display is not treated as unlimited PostgreSQL archival. Ask IBKR whether non-pro OPRA allows private non-display snapshots.",
            (),
            dict(self.CAPABILITIES),
        )


class MsrbEmmaAdapter:
    source_id = "MSRB_EMMA"
    ENABLE_FLAG = "MI_MSRB_ENABLED"
    CREDENTIAL_ENV = ("MSRB_API_KEY", "EMMA_API_KEY")

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        has_key = any(str(env.get(name) or "").strip() for name in self.CREDENTIAL_ENV)
        if not has_key:
            return AdapterStatus(
                self.source_id,
                ACCESS_CONFIGURATION_REQUIRED,
                False,
                "No free unauthenticated EMMA API. Create an MSRB developer account and API key at https://emma.msrb.org/AboutEMMA/Developers, then set MSRB_API_KEY. Do not scrape EMMA HTML.",
                self.CREDENTIAL_ENV + (self.ENABLE_FLAG,),
                {"trades": "not configured", "curves": "not configured"},
            )
        if not _flag(env, self.ENABLE_FLAG):
            return AdapterStatus(
                self.source_id,
                ACCESS_DISABLED,
                False,
                "MSRB/EMMA credentials are present but MI_MSRB_ENABLED is off.",
                self.CREDENTIAL_ENV + (self.ENABLE_FLAG,),
                {"trades": "disabled"},
            )
        return AdapterStatus(self.source_id, ACCESS_ENTITLEMENT_REQUIRED, False, "MSRB/EMMA terms must be reviewed before ingest.", self.CREDENTIAL_ENV + (self.ENABLE_FLAG,), {"trades": "rights review required"})


class IBKRMunicipalBondsAdapter:
    source_id = "IBKR_MUNICIPAL_BONDS"
    ENABLE_FLAG = "MI_IBKR_MUNI_ENABLED"

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        return AdapterStatus(
            self.source_id,
            ACCESS_CONFIGURATION_REQUIRED,
            False,
            "Municipal cash-bond quotes need an official CUSIP/ISIN (MSRB/EMMA developer key or issuer prospectus). reqMatchingSymbols alone is insufficient. Calculator remains usable. Quote persistence stays RIGHTS_PENDING.",
            (self.ENABLE_FLAG, "CUSIP or ISIN", "IBKR bond market-data entitlement", "storage rights review"),
            {
                "discovery": "CUSIP/ISIN required; matching-symbols name-only is not enough",
                "quotes": "blocked without identifiable contract",
                "calculator": "available",
                "storage": "RIGHTS_PENDING",
            },
        )


def _bond_capability_evidence_path(env: Mapping[str, str]) -> Path:
    override = str(env.get("MI_IBKR_BOND_CAPABILITY_EVIDENCE") or "").strip()
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if local:
        return Path(local) / "FMP_SCREENER" / "ibkr_collector" / "bond_capability_latest.json"
    return Path.home() / ".fmp_screener" / "ibkr_collector" / "bond_capability_latest.json"


def _load_bond_capability(env: Mapping[str, str]) -> dict[str, Any] | None:
    path = _bond_capability_evidence_path(env)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    observed_at = str(payload.get("observed_at") or "")
    try:
        when = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    max_age_h = float(str(env.get("MI_IBKR_BOND_CAPABILITY_MAX_AGE_HOURS") or "72"))
    if datetime.now(timezone.utc) - when > timedelta(hours=max_age_h):
        return None
    return payload


class IBKRCorporateBondsAdapter:
    source_id = "IBKR_CORPORATE_BONDS"
    ENABLE_FLAG = "MI_IBKR_CORP_BONDS_ENABLED"

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        evidence = _load_bond_capability(env)
        if evidence is None:
            return AdapterStatus(
                self.source_id,
                ACCESS_ENTITLEMENT_REQUIRED,
                False,
                "No fresh local bond capability evidence. Prior CUSIP→conId resolution is documented; re-run scripts/ibkr_bond_identifier_proof.py against the Windows TWS session. PostgreSQL archival remains RIGHTS_PENDING. FINRA Query aggregates remain available.",
                (self.ENABLE_FLAG, "fresh bond capability evidence", "storage rights review"),
                {
                    "discovery": "unproven_this_session",
                    "quotes": "unproven",
                    "ratings": "unproven",
                    "storage": "RIGHTS_PENDING",
                    "aggregates": "FINRA Query",
                },
            )
        quote_class = str(evidence.get("quote_classification") or "UNKNOWN")
        rating_class = str(evidence.get("ratings_classification") or "UNKNOWN")
        discovery = str(evidence.get("discovery_classification") or "UNKNOWN")
        caps = {
            "discovery": discovery,
            "quotes": quote_class,
            "ratings": rating_class,
            "storage": "RIGHTS_PENDING",
            "aggregates": "FINRA Query",
            "observed_at": evidence.get("observed_at"),
            "market_data_type": evidence.get("market_data_type"),
        }
        if discovery.startswith("IDENTIFIER_RESOLVED") and quote_class in {"LIVE_BID_ASK", "ONE_SIDED_QUOTE"}:
            access = ACCESS_AVAILABLE
            reason = "Fresh local proof: contract resolved with current quote ticks. Subscription confirmation alone is insufficient; this status reflects observed API delivery. Storage remains RIGHTS_PENDING."
        elif discovery.startswith("IDENTIFIER_RESOLVED"):
            access = ACCESS_ENTITLEMENT_REQUIRED
            reason = "Fresh local proof: contract resolved ({0}); quotes classified {1}; ratings {2}. Storage remains RIGHTS_PENDING.".format(
                discovery, quote_class, rating_class
            )
        else:
            access = ACCESS_ENTITLEMENT_REQUIRED
            reason = "Fresh local proof did not resolve a corporate bond contract ({0}).".format(discovery)
        return AdapterStatus(
            self.source_id,
            access,
            False,
            reason,
            (self.ENABLE_FLAG, "IBKR bond market-data entitlement", "storage rights review"),
            caps,
        )


class CftcCotAdapter:
    source_id = "CFTC_COT"

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        return AdapterStatus(
            self.source_id,
            ACCESS_AVAILABLE,
            True,
            "Public CFTC SODA COT (6dca-aqww) requires no credential. Weekly as-of Tuesday, typically released Friday.",
            (),
            {"positioning": "legacy futures-only watchlist"},
        )


class EiaEnergyAdapter:
    source_id = "EIA_ENERGY"

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        has_key = bool(str(env.get("EIA_API_KEY") or "").strip())
        if not has_key:
            return AdapterStatus(
                self.source_id,
                ACCESS_CONFIGURATION_REQUIRED,
                False,
                "EIA_API_KEY is absent. Register a free key at https://www.eia.gov/opendata/ and set EIA_API_KEY on the writer host. FRED WTI/Henry Hub remain price fallbacks and must not be labeled as EIA observations.",
                ("EIA_API_KEY",),
                {"petroleum": "signup required"},
            )
        # Key presence is configuration only. Freshness/transport evidence decides health.
        return AdapterStatus(
            self.source_id,
            ACCESS_CONFIGURED,
            True,
            "EIA_API_KEY present on writer env. Credential presence is not READY; weekly petroleum stocks / working-gas storage require a successful ingest with non-empty observations.",
            ("EIA_API_KEY",),
            {"petroleum": "configured_pending_evidence"},
        )


class OpenFIGIAdapter:
    source_id = OPENFIGI_SOURCE_ID
    ENABLE_FLAG = "MI_OPENFIGI_ENABLED"
    KEY_ENV = "OPENFIGI_API_KEY"
    CAPABILITIES = {
        "mapping": "bounded identifier mapping only",
        "quotes": "not a quote feed",
        "ratings": "not provided",
    }

    def probe(self, env: Mapping[str, str]) -> AdapterStatus:
        from market_intelligence.openfigi_client import api_key_from_env, enabled_from_env

        key = api_key_from_env(env)
        flag = enabled_from_env(env)
        if not key:
            return AdapterStatus(self.source_id, ACCESS_CONFIGURATION_REQUIRED, False, "OPENFIGI_API_KEY absent", (self.KEY_ENV, self.ENABLE_FLAG), dict(self.CAPABILITIES))
        if not flag:
            return AdapterStatus(
                self.source_id,
                ACCESS_ON_DEMAND,
                False,
                "OPENFIGI_API_KEY present. Identifier mapping is on-demand reference data; set MI_OPENFIGI_ENABLED=1 for an explicit bounded batch.",
                (self.KEY_ENV, self.ENABLE_FLAG),
                dict(self.CAPABILITIES),
            )
        return AdapterStatus(self.source_id, ACCESS_ON_DEMAND, True, "key present and on-demand mapping enabled", (self.KEY_ENV, self.ENABLE_FLAG), dict(self.CAPABILITIES))


ADAPTERS = (
    IBKRMarketDataAdapter(),
    TraceAdapter(),
    EdgarAdapter(),
    OpenFIGIAdapter(),
    OpenBBCboeOptionsAdapter(),
    OpenBBCboeVixAdapter(),
    CboeAllAccessAdapter(),
    IBKROptionsAdapter(),
    IBKROptionsStorageAdapter(),
    MsrbEmmaAdapter(),
    IBKRMunicipalBondsAdapter(),
    IBKRCorporateBondsAdapter(),
    CftcCotAdapter(),
    EiaEnergyAdapter(),
)


def probe_all(env: Mapping[str, str]) -> dict[str, AdapterStatus]:
    """Access status per external source, keyed by source_id (used by the refresh job and docs)."""
    return {adapter.source_id: adapter.probe(env) for adapter in ADAPTERS}


__all__ = [
    "ACCESS_AVAILABLE",
    "ACCESS_ON_DEMAND",
    "ACCESS_CONFIGURATION_REQUIRED",
    "ACCESS_CONFIGURED",
    "ACCESS_AGREEMENT_REQUIRED",
    "ACCESS_DISABLED",
    "ACCESS_ENTITLEMENT_REQUIRED",
    "ACCESS_NOT_CONFIGURED",
    "ACCESS_PROVIDER_SUPPORT_REQUIRED",
    "ACCESS_RIGHTS_PENDING",
    "ACCESS_TEMPORARILY_UNAVAILABLE",
    "ADAPTERS",
    "AdapterDisabled",
    "AdapterStatus",
    "CftcCotAdapter",
    "EdgarAdapter",
    "EiaEnergyAdapter",
    "IBKRCorporateBondsAdapter",
    "IBKRMarketDataAdapter",
    "IBKRMunicipalBondsAdapter",
    "IBKROptionsAdapter",
    "IBKROptionsStorageAdapter",
    "MsrbEmmaAdapter",
    "OpenBBCboeOptionsAdapter",
    "OpenBBCboeVixAdapter",
    "CboeAllAccessAdapter",
    "OpenFIGIAdapter",
    "TraceAdapter",
    "probe_all",
]
