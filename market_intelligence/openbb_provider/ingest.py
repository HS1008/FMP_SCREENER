"""Fetch outside DB transactions; publish each symbol atomically."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping

from market_intelligence.openbb_provider.client import AcquisitionError, OpenBBClient, RawChainFetch, RawCurveFetch
from market_intelligence.openbb_provider.config import (
    OPENBB_OPTIONS_SOURCE_ID,
    OPENBB_VIX_SOURCE_ID,
    options_enabled_from_env,
    options_symbols_from_env,
    probe_openbb,
    vix_enabled_from_env,
)
from market_intelligence.openbb_provider.due import options_due, target_session, vix_due
from market_intelligence.openbb_provider.errors import OpenBBAcquisitionError
from market_intelligence.openbb_provider.normalize import NormalizedChain, normalize_chain
from market_intelligence.openbb_provider.store import (
    current_complete_snapshot,
    publish_chain,
    publish_curve,
    sync_registry,
)
from market_intelligence.openbb_provider.vix import NormalizedCurve, normalize_curve
from market_intelligence.store import RUN_FAILED, RUN_PARTIAL, RUN_SKIPPED, RUN_SUCCEEDED, finish_run, start_run

_FETCH_ERRORS = (AcquisitionError, OpenBBAcquisitionError)


def _root_exception_class(exc: BaseException) -> str:
    origin = exc
    seen: set[int] = set()
    while origin.__cause__ is not None and id(origin.__cause__) not in seen:
        seen.add(id(origin))
        origin = origin.__cause__
    return origin.__class__.__name__


def _sanitized_error(exc: BaseException) -> dict[str, str]:
    """Category + exception class only. Never persist messages (they can contain secrets)."""
    category = getattr(exc, "category", None)
    if not category:
        category = exc.__class__.__name__
    return {"error": str(category), "exception_class": _root_exception_class(exc)}


def _failed_chain(symbol: str, session_date: date, now: datetime, quality: Mapping[str, Any]) -> NormalizedChain:
    return NormalizedChain(
        underlying=symbol,
        session_date=session_date,
        session_basis="fetch_last_completed_session",
        observation_time_utc=None,
        observation_precision="unknown",
        collected_at=now,
        source_timestamp_utc=None,
        underlying_price=None,
        contracts=[],
        rejected=[],
        metadata={},
        quality=dict(quality),
        content_hash="failed_{0}_{1}".format(symbol, now.isoformat()),
        openbb_version=None,
    )


def _failed_curve(session_date: date, now: datetime, quality: Mapping[str, Any]) -> NormalizedCurve:
    return NormalizedCurve(
        session_date=session_date,
        observation_date=None,
        observation_precision="unknown",
        level_type="CBOE_VX_EOD_4PM_ET",
        collected_at=now,
        points=[],
        rejected=[],
        content_hash="failed_vx_{0}".format(now.isoformat()),
        quality=dict(quality),
        openbb_version=None,
    )


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


def _persist_failed_chain(engine, *, symbol: str, session_date: date, now: datetime, parent: str, quality: Mapping[str, Any]) -> dict[str, Any]:
    empty = _failed_chain(symbol, session_date, now, quality)
    category = str(quality.get("error") or "UNKNOWN")
    with engine.begin() as conn:
        rid = start_run(conn, source_id=OPENBB_OPTIONS_SOURCE_ID, dataset="options_chain:{0}".format(symbol), parent_run_id=parent)
        published = publish_chain(conn, empty, run_id=rid, failed=True, error=category)
        finish_run(
            conn,
            rid,
            status=RUN_FAILED,
            details={"status": RUN_FAILED, "error": category, "exception_class": quality.get("exception_class"), "snapshot_id": published.get("snapshot_id")},
        )
    return published


def _persist_failed_curve(engine, *, session_date: date, now: datetime, parent: str, quality: Mapping[str, Any]) -> dict[str, Any]:
    empty = _failed_curve(session_date, now, quality)
    category = str(quality.get("error") or "UNKNOWN")
    with engine.begin() as conn:
        rid = start_run(conn, source_id=OPENBB_VIX_SOURCE_ID, dataset="vix_eod_curve", parent_run_id=parent)
        published = publish_curve(conn, empty, run_id=rid, failed=True, error=category)
        finish_run(
            conn,
            rid,
            status=RUN_FAILED,
            details={"status": RUN_FAILED, "error": category, "exception_class": quality.get("exception_class"), "snapshot_id": published.get("snapshot_id")},
        )
    return published


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
    probe = probe_openbb(env)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    options_on = include_options and options_enabled_from_env(env)
    vix_on = include_vix and vix_enabled_from_env(env)
    parent_source = OPENBB_OPTIONS_SOURCE_ID if options_on or not vix_on else OPENBB_VIX_SOURCE_ID
    if not options_on and not vix_on:
        report.status = RUN_SKIPPED
        report.error = probe.options.reason if include_options else probe.vix.reason
        with engine.begin() as conn:
            sync_registry(conn, env)
            rid = start_run(conn, source_id=parent_source, dataset="openbb_cboe", parent_run_id=parent_run_id)
            finish_run(
                conn,
                rid,
                status=RUN_SKIPPED,
                details={"reason": report.error, "options": probe.options.access_status, "vix": probe.vix.access_status},
            )
        return report

    client = client or OpenBBClient(env=env, clock=clock)
    symbols = options_symbols_from_env(env) if options_on else ()
    wanted = session_date or today or target_session(now)
    if isinstance(wanted, datetime):
        wanted = target_session(wanted)

    with engine.begin() as conn:
        sync_registry(conn, env)
        parent = start_run(conn, source_id=parent_source, dataset="openbb_cboe", parent_run_id=parent_run_id)

    failures = 0
    for symbol in symbols:
        with engine.begin() as conn:
            current = current_complete_snapshot(conn, dataset="options_chain", underlying=symbol)
            last_session = current["session_date"] if current else None
            verdict = options_due(last_published_session=last_session, last_attempt_status=None, now=now, env=env, wanted=wanted)
            due = force or verdict["due"]
        if not due:
            report.skipped_due.append(symbol)
            report.symbols[symbol] = {"status": RUN_SKIPPED, "reason": verdict["reason"]}
            continue
        try:
            raw = client.fetch_options_chain(symbol)
            chain = normalize_chain(raw, clock=now)
        except _FETCH_ERRORS as exc:
            failures += 1
            quality = _sanitized_error(exc)
            published = _persist_failed_chain(engine, symbol=symbol, session_date=wanted, now=now, parent=parent, quality=quality)
            report.symbols[symbol] = {"status": RUN_FAILED, "error": quality["error"], "exception_class": quality["exception_class"], "snapshot_id": published.get("snapshot_id")}
            continue
        except Exception as exc:  # noqa: BLE001
            failures += 1
            quality = _sanitized_error(exc)
            published = _persist_failed_chain(engine, symbol=symbol, session_date=wanted, now=now, parent=parent, quality=quality)
            report.symbols[symbol] = {"status": RUN_FAILED, "error": quality["error"], "exception_class": quality["exception_class"], "snapshot_id": published.get("snapshot_id")}
            continue
        with engine.begin() as conn:
            rid = start_run(conn, source_id=OPENBB_OPTIONS_SOURCE_ID, dataset="options_chain:{0}".format(symbol), parent_run_id=parent)
            published = publish_chain(conn, chain, run_id=rid)
            finish_run(conn, rid, status=RUN_SUCCEEDED if published["status"] in {"COMPLETE", "REPLAY"} else RUN_PARTIAL, details=published)
        report.symbols[symbol] = published
        if published["status"] == "FAILED":
            failures += 1

    if vix_on:
        with engine.begin() as conn:
            current_vix = current_complete_snapshot(conn, dataset="vix_eod_curve", underlying="VX")
            last_vix = current_vix["session_date"] if current_vix else None
            verdict_vix = vix_due(last_published_date=last_vix, now=now, wanted=wanted)
            due_vix = force or verdict_vix["due"]
        if not due_vix:
            report.vix = {"status": RUN_SKIPPED, "reason": verdict_vix["reason"]}
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
                quality = _sanitized_error(exc)
                published = _persist_failed_curve(engine, session_date=wanted, now=now, parent=parent, quality=quality)
                report.vix = {"status": RUN_FAILED, "error": quality["error"], "exception_class": quality["exception_class"], "snapshot_id": published.get("snapshot_id")}
            except Exception as exc:  # noqa: BLE001
                failures += 1
                quality = _sanitized_error(exc)
                published = _persist_failed_curve(engine, session_date=wanted, now=now, parent=parent, quality=quality)
                report.vix = {"status": RUN_FAILED, "error": quality["error"], "exception_class": quality["exception_class"], "snapshot_id": published.get("snapshot_id")}

    report.status = RUN_SUCCEEDED if failures == 0 else (RUN_FAILED if not report.symbols and report.vix and report.vix.get("status") == RUN_FAILED else RUN_PARTIAL)
    report.failed = failures > 0
    with engine.begin() as conn:
        finish_run(conn, parent, status=report.status, details=report.as_dict())
    return report


_ = (RawChainFetch, RawCurveFetch)
