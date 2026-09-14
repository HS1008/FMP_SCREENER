"""Lazy OpenBB acquisition with injectable clocks, timeouts, and bounded retries."""

from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from market_intelligence.openbb_provider.config import (
    operation_budget_s,
    retry_limit,
    timeout_s,
)

RETRYABLE = {"timeout", "rate_limited", "temporarily_unavailable", "transport"}
NON_RETRYABLE = {"access_denied", "auth", "schema", "missing_dependency", "disabled"}


class AcquisitionError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass
class RawChainFetch:
    symbol: str
    contracts: list[dict[str, Any]]
    metadata: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)
    openbb_version: str | None = None
    provider: str = "cboe"
    fetched_at: datetime | None = None


@dataclass
class RawCurveFetch:
    symbol: str
    points: list[dict[str, Any]]
    metadata: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)
    openbb_version: str | None = None
    provider: str = "cboe"
    fetched_at: datetime | None = None


def classify_error(exc: BaseException) -> str:
    text = "{0} {1}".format(type(exc).__name__, exc).lower()
    if "import" in text and "openbb" in text:
        return "missing_dependency"
    if any(token in text for token in ("401", "403", "forbidden", "unauthorized", "denied")):
        return "access_denied"
    if any(token in text for token in ("429", "rate limit", "too many")):
        return "rate_limited"
    if any(token in text for token in ("timeout", "timed out")):
        return "timeout"
    if any(token in text for token in ("schema", "validation", "keyerror", "missing")):
        return "schema"
    if any(token in text for token in ("503", "502", "temporarily", "unavailable")):
        return "temporarily_unavailable"
    return "transport"


def _sleep(seconds: float, sleeper: Callable[[float], None]) -> None:
    if seconds > 0:
        sleeper(seconds)


def with_retries(
    operation: Callable[[], Any],
    *,
    retries: int,
    timeout_seconds: float,
    budget_seconds: float,
    rng: random.Random | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    """Run ``operation`` in a worker thread so a hang cannot exceed the timeout.

    Retries are bounded. Auth, access-denied, schema, and missing-dependency errors
    are not retried. The whole call respects ``budget_seconds``.
    """
    started = monotonic()
    last_error: BaseException | None = None
    attempts = retries + 1
    dice = rng or random.Random()
    for attempt in range(attempts):
        remaining_budget = budget_seconds - (monotonic() - started)
        if remaining_budget <= 0:
            raise AcquisitionError("timeout", "operation budget exhausted")
        slice_timeout = min(timeout_seconds, remaining_budget)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(operation)
                return future.result(timeout=slice_timeout)
        except FuturesTimeout as exc:
            last_error = exc
            category = "timeout"
        except AcquisitionError as exc:
            last_error = exc
            category = exc.category
        except Exception as exc:  # noqa: BLE001 - classified below
            from market_intelligence.openbb_provider.errors import OpenBBAcquisitionError, classify_exception

            last_error = exc
            if isinstance(exc, OpenBBAcquisitionError):
                category = exc.category.lower()
                if not exc.retryable or attempt >= attempts - 1:
                    raise
            else:
                category, retryable = classify_exception(exc)
                category = category.lower()
                if not retryable:
                    raise OpenBBAcquisitionError(str(exc), category=category.upper(), retryable=False) from exc
        if category in NON_RETRYABLE or attempt >= attempts - 1:
            if last_error.__class__.__name__ == "OpenBBAcquisitionError":
                raise last_error
            raise AcquisitionError(category, str(last_error)) from last_error
        remaining_budget = budget_seconds - (monotonic() - started)
        if remaining_budget <= 0:
            raise AcquisitionError("timeout", "operation budget exhausted") from last_error
        backoff = min(8.0, 0.4 * (2**attempt) + dice.random() * 0.3)
        _sleep(min(backoff, remaining_budget), sleeper)
    raise AcquisitionError("transport", str(last_error)) from last_error


def _package_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("openbb")
    except Exception:  # noqa: BLE001
        return None


def _rows_from_obbject(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, list):
        rows = []
        for item in result:
            if hasattr(item, "model_dump"):
                rows.append(item.model_dump())
            elif isinstance(item, dict):
                rows.append(item)
            else:
                rows.append(dict(getattr(item, "__dict__", {})))
        return rows
    frame = getattr(result, "dataframe", None)
    if frame is not None and hasattr(frame, "to_dict"):
        return [dict(row) for row in frame.to_dict(orient="records")]
    to_df = getattr(result, "to_df", None)
    if callable(to_df):
        frame = to_df()
        return [dict(row) for row in frame.to_dict(orient="records")]
    return []


def _metadata_from_obbject(obj: Any) -> dict[str, Any]:
    extra = getattr(obj, "extra", None) or {}
    if not isinstance(extra, dict):
        extra = {}
    meta = extra.get("results_metadata") or extra.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {"value": str(meta)}
    return {key: value for key, value in meta.items() if value is not None}


class OpenBBClient:
    """Injectable Cboe acquisition. Real OpenBB is imported only inside fetch methods."""

    def __init__(
        self,
        *,
        chain_fn: Callable[[str], Any] | None = None,
        curve_fn: Callable[[], Any] | None = None,
        fetch_chains: Callable[..., Any] | None = None,
        fetch_curve: Callable[..., Any] | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
        sleeper: Callable[[float], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        retries: int | None = None,
        operation_deadline_sec: float | None = None,
        request_timeout_sec: float | None = None,
    ) -> None:
        self._chain_fn = fetch_chains or chain_fn
        self._curve_fn = fetch_curve or curve_fn
        self._env = env
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng or random.Random(0)
        self._sleeper = sleep or sleeper or time.sleep
        self.request_timeout_sec = request_timeout_sec if request_timeout_sec is not None else timeout_s(env)
        self._retries = retries if retries is not None else retry_limit(env)
        self._budget = operation_deadline_sec if operation_deadline_sec is not None else operation_budget_s(env)

    def _call(self, operation: Callable[[], Any]) -> Any:
        from market_intelligence.openbb_provider.errors import OpenBBAcquisitionError

        try:
            return with_retries(
                operation,
                retries=self._retries,
                timeout_seconds=self.request_timeout_sec,
                budget_seconds=self._budget,
                rng=self._rng,
                sleeper=self._sleeper,
            )
        except OpenBBAcquisitionError:
            raise
        except AcquisitionError as exc:
            retryable = exc.category not in {"access_denied", "auth", "schema", "missing_dependency", "disabled"}
            raise OpenBBAcquisitionError(exc.message, category=exc.category.upper(), retryable=retryable) from exc

    def options_chains(self, symbol: str) -> Any:
        return self.fetch_options_chain(symbol)

    def vix_curve(self) -> Any:
        return self.fetch_vix_curve()

    def _load_obb(self) -> Any:
        try:
            from openbb import obb  # noqa: WPS433 - lazy, acquisition path only
        except ImportError as exc:
            raise AcquisitionError("missing_dependency", "openbb is not installed") from exc
        return obb

    def fetch_options_chain(self, symbol: str) -> RawChainFetch:
        symbol = symbol.strip().upper()

        def _op() -> Any:
            if self._chain_fn is not None:
                return self._chain_fn(symbol=symbol)
            obb = self._load_obb()
            return obb.derivatives.options.chains(symbol=symbol, provider="cboe")

        obj = self._call(_op)
        if isinstance(obj, RawChainFetch):
            return obj
        results = getattr(obj, "results", obj)
        extra = getattr(obj, "extra", {}) if not isinstance(obj, dict) else obj.get("extra") or {}
        metadata = _metadata_from_obbject(obj)
        if isinstance(obj, dict) and "contracts" in obj:
            contracts = list(obj["contracts"])
            metadata = dict(obj.get("metadata") or metadata)
        else:
            contracts = _rows_from_obbject(results)
        return RawChainFetch(
            symbol=symbol,
            contracts=contracts,
            metadata=metadata,
            extra=extra if isinstance(extra, dict) else {},
            openbb_version=_package_version(),
            fetched_at=self._clock(),
        )

    def fetch_vix_curve(self) -> RawCurveFetch:
        def _op() -> Any:
            if self._curve_fn is not None:
                return self._curve_fn()
            obb = self._load_obb()
            return obb.derivatives.futures.curve(symbol="VX_EOD", provider="cboe")

        obj = self._call(_op)
        if isinstance(obj, RawCurveFetch):
            return obj
        results = getattr(obj, "results", obj)
        extra = getattr(obj, "extra", {}) if not isinstance(obj, dict) else obj.get("extra") or {}
        metadata = _metadata_from_obbject(obj)
        if isinstance(obj, dict) and "points" in obj:
            points = list(obj["points"])
            metadata = dict(obj.get("metadata") or metadata)
        else:
            points = _rows_from_obbject(results)
        return RawCurveFetch(
            symbol="VX_EOD",
            points=points,
            metadata=metadata,
            extra=extra if isinstance(extra, dict) else {},
            openbb_version=_package_version(),
            fetched_at=self._clock(),
        )
