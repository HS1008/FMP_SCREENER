"""Fetch outside DB transactions; publish each symbol atomically."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping

from market_intelligence.calendars import last_completed_session
from market_intelligence.openbb_provider.client import AcquisitionError, OpenBBClient, RawChainFetch, RawCurveFetch
from market_intelligence.openbb_provider.config import (
    OPENBB_OPTIONS_SOURCE_ID,
    OPENBB_VIX_SOURCE_ID,
    options_enabled_from_env,
    options_symbols_from_env,
    probe_fields,
    vix_enabled_from_env,
)
from market_intelligence.openbb_provider.errors import OpenBBAcquisitionError
from market_intelligence.openbb_provider.normalize import NormalizedChain, normalize_chain
from market_intelligence.openbb_provider.store import current_complete_snapshot, ensure_source, publish_chain, publish_curve
from market_intelligence.openbb_provider.vix import NormalizedCurve, normalize_curve
from market_intelligence.store import RUN_FAILED, RUN_PARTIAL, RUN_SKIPPED, RUN_SUCCEEDED, finish_run, start_run

_FETCH_ERRORS = (AcquisitionError, OpenBBAcquisitionError)


def _error_fields(exc: BaseException) -> tuple[str, str]:
    category = getattr(exc, "category", exc.__class__.__name__)
    message = getattr(exc, "message", str(exc))
    return str(category), str(message)


@dataclass
class OpenBBIngestReport:
    status: str = RUN_SUCCEEDED
    failed: bool = False
    symbols: dict[str, Any] = field(default_factory=dict)
    vix: dict[str, Any] | None = None
    skipped_due: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "failed": self.failed,
            "symbols": self.symbols,
            "vix": self.vix,
            "skipped_due": self.skipped_due,
            "error": self.error,
        }


def chain_due(conn, symbol: str, session_date) -> bool:
    current = current_complete_snapshot(conn, dataset="options_chain", underlying=symbol)
    if current is None:
        return True
    return str(current["session_date"]) != str(session_date)


def curve_due(conn, session_date) -> bool:
    current = current_complete_snapshot(conn, dataset="vix_eod_curve", underlying="VX")
    if current is None:
        return True
    return str(current["session_date"]) != str(session_date)


def ingest_openbb(
    engine,
    client: OpenBBClient | None = None,
    *,
    parent_run_id: str | None = None,
    env: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
    force: bool = False,
    session_date=None,
    today=None,
    include_options: bool = True,
    include_vix: bool = True,
) -> OpenBBIngestReport:
    report = OpenBBIngestReport()
    probe = probe_fields(env)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    options_on = include_options and (options_enabled_from_env(env) or probe["enabled"])
    vix_on = include_vix and vix_enabled_from_env(env)
    parent_source = OPENBB_OPTIONS_SOURCE_ID if options_on or not vix_on else OPENBB_VIX_SOURCE_ID
    if not options_on and not vix_on:
        report.status = RUN_SKIPPED
        report.error = probe["reason"]
        with engine.begin() as conn:
            ensure_source(conn, enabled=False, access=probe["access_status"])
            rid = start_run(conn, source_id=parent_source, dataset="openbb_cboe", parent_run_id=parent_run_id)
            finish_run(conn, rid, status=RUN_SKIPPED, details={"reason": probe["reason"]})
        return report

    client = client or OpenBBClient(env=env, clock=clock)
    symbols = options_symbols_from_env(env) if include_options else ()
    target_session = session_date or today or last_completed_session(now)
    if isinstance(target_session, datetime):
        target_session = last_completed_session(target_session)
    elif isinstance(target_session, date) and session_date is None and today is not None:
        target_session = today

    with engine.begin() as conn:
        ensure_source(conn, enabled=True, access="CONFIGURED")
        parent = start_run(conn, source_id=parent_source, dataset="openbb_cboe", parent_run_id=parent_run_id)

    failures = 0
    for symbol in symbols:
        with engine.begin() as conn:
            due = force or chain_due(conn, symbol, target_session)
        if not due:
            report.skipped_due.append(symbol)
            report.symbols[symbol] = {"status": RUN_SKIPPED, "reason": "already_have_session"}
            continue
        try:
            raw = client.fetch_options_chain(symbol)
            chain = normalize_chain(raw, clock=now)
        except _FETCH_ERRORS as exc:
            failures += 1
            category, message = _error_fields(exc)
            report.symbols[symbol] = {"status": RUN_FAILED, "error": category, "detail": message}
            with engine.begin() as conn:
                rid = start_run(conn, source_id=OPENBB_OPTIONS_SOURCE_ID, dataset="options_chain:{0}".format(symbol), parent_run_id=parent)
                empty = NormalizedChain(
                    underlying=symbol,
                    session_date=target_session,
                    session_basis="fetch_last_completed_session",
                    observation_time_utc=None,
                    observation_precision="unknown",
                    collected_at=now,
                    source_timestamp_utc=None,
                    underlying_price=None,
                    contracts=[],
                    rejected=[],
                    metadata={},
                    quality={"error": category},
                    content_hash="failed_{0}_{1}".format(symbol, now.isoformat()),
                    openbb_version=None,
                )
                published = publish_chain(conn, empty, run_id=rid, failed=True, error=category)
                finish_run(conn, rid, status=RUN_FAILED, details=published)
            continue
        except Exception as exc:  # noqa: BLE001
            failures += 1
            report.symbols[symbol] = {"status": RUN_FAILED, "error": exc.__class__.__name__}
            continue
        with engine.begin() as conn:
            rid = start_run(conn, source_id=OPENBB_OPTIONS_SOURCE_ID, dataset="options_chain:{0}".format(symbol), parent_run_id=parent)
            published = publish_chain(conn, chain, run_id=rid)
            finish_run(conn, rid, status=RUN_SUCCEEDED if published["status"] in {"COMPLETE", "REPLAY"} else RUN_PARTIAL, details=published)
        report.symbols[symbol] = published
        if published["status"] == "FAILED":
            failures += 1

    if include_vix and vix_enabled_from_env(env):
        with engine.begin() as conn:
            due_vix = force or curve_due(conn, target_session)
        if not due_vix:
            report.vix = {"status": RUN_SKIPPED, "reason": "already_have_session"}
            report.skipped_due.append("VX")
        else:
            try:
                raw_curve = client.fetch_vix_curve()
                curve = normalize_curve(raw_curve, clock=now)
                with engine.begin() as conn:
                    rid = start_run(conn, source_id=OPENBB_VIX_SOURCE_ID, dataset="vix_eod_curve", parent_run_id=parent)
                    published = publish_curve(conn, curve, run_id=rid)
                    finish_run(conn, rid, status=RUN_SUCCEEDED if published["status"] in {"COMPLETE", "REPLAY"} else RUN_PARTIAL, details=published)
                report.vix = published
            except _FETCH_ERRORS as exc:
                failures += 1
                category, _message = _error_fields(exc)
                report.vix = {"status": RUN_FAILED, "error": category}
                with engine.begin() as conn:
                    rid = start_run(conn, source_id=OPENBB_VIX_SOURCE_ID, dataset="vix_eod_curve", parent_run_id=parent)
                    empty = NormalizedCurve(
                        session_date=target_session,
                        observation_date=None,
                        observation_precision="unknown",
                        level_type="CBOE_VX_EOD_4PM_ET",
                        collected_at=now,
                        points=[],
                        rejected=[],
                        content_hash="failed_vx_{0}".format(now.isoformat()),
                        quality={"error": category},
                        openbb_version=None,
                    )
                    published = publish_curve(conn, empty, run_id=rid, failed=True, error=category)
                    finish_run(conn, rid, status=RUN_FAILED, details=published)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                report.vix = {"status": RUN_FAILED, "error": exc.__class__.__name__}

    report.status = RUN_SUCCEEDED if failures == 0 else (RUN_FAILED if not report.symbols and report.vix and report.vix.get("status") == RUN_FAILED else RUN_PARTIAL)
    report.failed = failures > 0
    with engine.begin() as conn:
        finish_run(conn, parent, status=report.status, details=report.as_dict())
    return report


# Silence unused import warnings for type checkers that want Raw* in this module.
_ = (RawChainFetch, RawCurveFetch)
