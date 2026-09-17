"""Source-aware due evaluation for the weekday 10-minute catch-up window.

Mon..Fri 09:15–18:30 America/New_York: a lightweight heartbeat may run every 10 minutes.
Provider calls happen only when a dataset/series is independently due.

Final catch-up grace: 18:30:00–18:39:59 ET so systemd RandomizedDelaySec / coalescing
of the 18:30 oneshot still runs; this is not an extra ordinary polling cycle.

Outcomes map onto existing run/freshness vocabulary via outcome labels:
SKIPPED_NOT_DUE, SKIPPED_ALREADY_CURRENT, SUCCESS_NEW_DATA, SUCCESS_NO_CHANGE,
FAILED_TRANSPORT, FAILED_PARSE, UNAVAILABLE.

Due-state keys:
  - FRED: per series_id (dataset ``series:<ID>``) using SERIES_POLICIES / catalog
  - other sources: source_id + dataset (or source-level fallback)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from market_intelligence.catalog import CATALOG, CATALOG_BY_ID
from market_intelligence.freshness import (
    AWAITING_RELEASE,
    assess_freshness,
    expected_latest_published,
    policy_for,
)
from market_intelligence.live_session import (
    YAHOO_LIVE_FALLBACK_ENV,
    in_regular_trading_hours,
    live_session_pair,
    yahoo_eod_fallback_enabled,
    yahoo_live_fallback_enabled,
)

ET = ZoneInfo("America/New_York")
WINDOW_START = time(9, 15)
# Inclusive end of ordinary scheduled polling (last OnCalendar tick is 18:30).
NORMAL_WINDOW_END = time(18, 30)
# Exclusive end of final-catch-up grace (survives systemd RandomizedDelaySec=60).
FINAL_GRACE_END = time(18, 40)
FINAL_CATCHUP = time(18, 30)
# Back-compat alias used by docs/tests that referred to the scheduled end.
WINDOW_END = NORMAL_WINDOW_END

TREASURY_DATASET = "daily_treasury_xml"
EQUITY_DATASET = "equity_etf_daily_bars"


@dataclass(frozen=True)
class DueDecision:
    step: str
    source_id: str
    due: bool
    reason: str
    outcome_if_skip: str = "SKIPPED_NOT_DUE"
    details: Mapping[str, Any] | None = None
    dataset: str | None = None
    series_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "source_id": self.source_id,
            "due": self.due,
            "reason": self.reason,
            "outcome_if_skip": self.outcome_if_skip,
            "details": dict(self.details or {}),
            "dataset": self.dataset,
            "series_id": self.series_id,
        }


def in_catchup_window(now: datetime | None = None) -> bool:
    """True on Mon–Fri from 09:15 through the final-catch-up grace (< 18:40 ET)."""
    now_et = (now or datetime.now(tz=ET)).astimezone(ET)
    if now_et.weekday() >= 5:
        return False
    t = now_et.timetz().replace(tzinfo=None)
    return WINDOW_START <= t < FINAL_GRACE_END


def in_normal_polling_window(now: datetime | None = None) -> bool:
    """Ordinary catch-up ticks: 09:15 through 18:30:00 inclusive (pre-grace)."""
    now_et = (now or datetime.now(tz=ET)).astimezone(ET)
    if now_et.weekday() >= 5:
        return False
    t = now_et.timetz().replace(tzinfo=None)
    return WINDOW_START <= t <= NORMAL_WINDOW_END


def is_final_catchup(now: datetime | None = None) -> bool:
    """18:30:00–18:39:59 ET weekday — final catch-up including systemd jitter grace."""
    now_et = (now or datetime.now(tz=ET)).astimezone(ET)
    if now_et.weekday() >= 5:
        return False
    t = now_et.timetz().replace(tzinfo=None)
    return FINAL_CATCHUP <= t < FINAL_GRACE_END


def _obs_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def fred_dataset_key(series_id: str) -> str:
    return "series:{0}".format(series_id)


def build_freshness_index(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], date | None]:
    """Index mi_data_freshness rows as (source_id, dataset) -> latest_observation_date."""
    out: dict[tuple[str, str], date | None] = {}
    for row in rows:
        source_id = str(row.get("source_id") or "")
        dataset = str(row.get("dataset") or "")
        if not source_id or not dataset:
            continue
        out[(source_id, dataset)] = _obs_date(row.get("latest_observation_date"))
    return out


def latest_for_dataset(
    index: Mapping[tuple[str, str], date | None],
    *,
    source_id: str,
    dataset: str,
) -> date | None:
    return index.get((source_id, dataset))


def latest_for_fred_series(
    index: Mapping[tuple[str, str], date | None],
    series_id: str,
) -> date | None:
    return index.get(("FRED", fred_dataset_key(series_id)))


def latest_for_source(
    index: Mapping[tuple[str, str], date | None],
    source_id: str,
) -> date | None:
    """Max observation date across all datasets for a source (single-dataset fallback)."""
    dates = [d for (sid, _), d in index.items() if sid == source_id and d is not None]
    return max(dates) if dates else None


def daily_source_due(
    *,
    step: str,
    source_id: str,
    cadence: str,
    latest_observation: date | None,
    now: datetime,
    series_id: str | None = None,
    same_day_available: bool = False,
) -> DueDecision:
    """Daily/business-day sources: due until today's expected observation is stored."""
    del same_day_available
    policy = policy_for(series_id=series_id, source_id=source_id, cadence=cadence)
    if policy is None:
        if latest_observation is not None and latest_observation >= now.astimezone(ET).date():
            return DueDecision(step, source_id, False, "already_current", "SKIPPED_ALREADY_CURRENT", series_id=series_id)
        return DueDecision(step, source_id, True, "no_policy_conservative_check", details={"cadence": cadence}, series_id=series_id)
    expected = expected_latest_published(policy=policy, now=now)
    assessment = assess_freshness(
        latest_observation,
        cadence=cadence,
        series_id=series_id,
        source_id=source_id,
        now=now,
        transport_status="OK",
    )
    if latest_observation is not None and latest_observation >= expected:
        return DueDecision(
            step,
            source_id,
            False,
            "already_current",
            "SKIPPED_ALREADY_CURRENT",
            {"expected": expected.isoformat(), "latest": latest_observation.isoformat()},
            series_id=series_id,
        )
    if assessment.status == AWAITING_RELEASE and not is_final_catchup(now):
        return DueDecision(
            step,
            source_id,
            False,
            "awaiting_release",
            "SKIPPED_NOT_DUE",
            {"expected": expected.isoformat(), "status": assessment.status},
            series_id=series_id,
        )
    return DueDecision(
        step,
        source_id,
        True,
        "catch_up_or_missing",
        details={
            "expected": expected.isoformat(),
            "latest": latest_observation.isoformat() if latest_observation else None,
            "status": assessment.status,
        },
        series_id=series_id,
    )


def release_calendar_due(
    *,
    step: str,
    source_id: str,
    cadence: str,
    latest_observation: date | None,
    now: datetime,
    series_id: str | None = None,
) -> DueDecision:
    """Weekly/monthly/release-based: poll only when a new publication is expected and missing."""
    policy = policy_for(series_id=series_id, source_id=source_id, cadence=cadence)
    if policy is None:
        return DueDecision(step, source_id, False, "no_policy_skip", "SKIPPED_NOT_DUE", series_id=series_id)
    expected = expected_latest_published(policy=policy, now=now)
    if latest_observation is not None and latest_observation >= expected:
        return DueDecision(step, source_id, False, "already_current", "SKIPPED_ALREADY_CURRENT", series_id=series_id)
    assessment = assess_freshness(
        latest_observation,
        cadence=cadence,
        series_id=series_id,
        source_id=source_id,
        now=now,
        transport_status="OK",
    )
    if assessment.status == AWAITING_RELEASE and not is_final_catchup(now):
        return DueDecision(step, source_id, False, "awaiting_release", "SKIPPED_NOT_DUE", series_id=series_id)
    return DueDecision(
        step,
        source_id,
        True,
        "release_catch_up",
        details={
            "expected": expected.isoformat(),
            "latest": latest_observation.isoformat() if latest_observation else None,
        },
        series_id=series_id,
    )


def series_unit_due(
    *,
    step: str,
    source_id: str,
    series_id: str,
    latest_observation: date | None,
    now: datetime,
    dataset: str | None = None,
) -> DueDecision:
    """Evaluate one series/dataset unit using its freshness policy."""
    policy = policy_for(series_id=series_id, source_id=source_id)
    if policy is not None:
        cadence = policy.cadence
    elif series_id in CATALOG_BY_ID:
        cadence = CATALOG_BY_ID[series_id].expected_frequency
    else:
        cadence = "D"
    dataset = dataset or fred_dataset_key(series_id)
    if policy is not None and policy.cadence in {"W", "M", "Q", "BW", "SA", "A"}:
        decision = release_calendar_due(
            step=step,
            source_id=source_id,
            cadence=policy.cadence,
            latest_observation=latest_observation,
            now=now,
            series_id=series_id,
        )
    else:
        decision = daily_source_due(
            step=step,
            source_id=source_id,
            cadence=str(cadence),
            latest_observation=latest_observation,
            now=now,
            series_id=series_id,
        )
    return DueDecision(
        step=decision.step,
        source_id=decision.source_id,
        due=decision.due,
        reason=decision.reason,
        outcome_if_skip=decision.outcome_if_skip,
        details=decision.details,
        dataset=dataset,
        series_id=series_id,
    )


def evaluate_fred_catalog_due(
    *,
    now: datetime,
    freshness: Mapping[tuple[str, str], date | None],
    series_ids: Sequence[str] | None = None,
) -> DueDecision:
    """Per-series FRED due evaluation; returns aggregate step with due_series list."""
    ids = list(series_ids) if series_ids is not None else [spec.series_id for spec in CATALOG]
    due_series: list[str] = []
    skipped_current: list[str] = []
    skipped_not_due: list[str] = []
    for sid in ids:
        unit = series_unit_due(
            step="fred",
            source_id="FRED",
            series_id=sid,
            latest_observation=latest_for_fred_series(freshness, sid),
            now=now,
        )
        if unit.due:
            due_series.append(sid)
        elif unit.outcome_if_skip == "SKIPPED_ALREADY_CURRENT":
            skipped_current.append(sid)
        else:
            skipped_not_due.append(sid)
    if due_series:
        return DueDecision(
            step="fred",
            source_id="FRED",
            due=True,
            reason="series_subset_due",
            details={
                "due_series": due_series,
                "due_count": len(due_series),
                "skipped_current_count": len(skipped_current),
                "skipped_not_due_count": len(skipped_not_due),
                "catalog_count": len(ids),
            },
        )
    if skipped_current and not skipped_not_due:
        outcome = "SKIPPED_ALREADY_CURRENT"
        reason = "all_series_already_current"
    elif skipped_current:
        outcome = "SKIPPED_NOT_DUE"
        reason = "no_series_due_some_current"
    else:
        outcome = "SKIPPED_NOT_DUE"
        reason = "no_series_due"
    return DueDecision(
        step="fred",
        source_id="FRED",
        due=False,
        reason=reason,
        outcome_if_skip=outcome,
        details={
            "due_series": [],
            "due_count": 0,
            "skipped_current_count": len(skipped_current),
            "skipped_not_due_count": len(skipped_not_due),
            "catalog_count": len(ids),
        },
    )


def yahoo_live_due(*, enabled: bool, now: datetime) -> DueDecision:
    if not enabled:
        return DueDecision("yahoo_live", "YAHOO_LIVE", False, "{0}_off".format(YAHOO_LIVE_FALLBACK_ENV), "SKIPPED_NOT_DUE")
    pair = live_session_pair(now)
    if pair is None:
        return DueDecision("yahoo_live", "YAHOO_LIVE", False, "non_nyse_session", "SKIPPED_NOT_DUE")
    if not in_regular_trading_hours(now):
        return DueDecision(
            "yahoo_live",
            "YAHOO_LIVE",
            False,
            "outside_rth",
            "SKIPPED_NOT_DUE",
            details={"current_session": pair.current_session.isoformat()},
        )
    return DueDecision(
        "yahoo_live",
        "YAHOO_LIVE",
        True,
        "rth_watchdog",
        details={"current_session": pair.current_session.isoformat(), "baseline_session": pair.baseline_session.isoformat()},
    )


def yahoo_eod_prior_due(*, enabled: bool, now: datetime) -> DueDecision:
    if not enabled:
        return DueDecision("yahoo_eod", "YAHOO_EOD", False, "yahoo_eod_fallback_off", "SKIPPED_NOT_DUE")
    pair = live_session_pair(now)
    if pair is None:
        return DueDecision("yahoo_eod", "YAHOO_EOD", False, "non_nyse_session", "SKIPPED_NOT_DUE")
    return DueDecision(
        "yahoo_eod",
        "YAHOO_EOD",
        True,
        "prior_close_gap_check",
        details={"baseline_session": pair.baseline_session.isoformat()},
    )


def evaluate_due_steps(
    *,
    now: datetime,
    env: Mapping[str, str],
    freshness: Mapping[tuple[str, str], date | None] | None = None,
    latest_by_source: Mapping[str, date | None] | None = None,
    configured_steps: Sequence[str],
    fred_series_ids: Sequence[str] | None = None,
) -> list[DueDecision]:
    """Return due decisions for configured refresh steps inside the catch-up window.

    Prefer ``freshness`` keyed by (source_id, dataset). ``latest_by_source`` remains as a
    compatibility fallback for single-dataset sources in older call sites/tests.
    """
    if not in_catchup_window(now):
        return [
            DueDecision(step, step.upper(), False, "outside_catchup_window", "SKIPPED_NOT_DUE")
            for step in configured_steps
        ]
    index = dict(freshness or {})
    if latest_by_source:
        for source_id, obs in latest_by_source.items():
            index.setdefault((source_id, "_source"), obs)

    def _source_latest(source_id: str, dataset: str | None = None) -> date | None:
        if dataset:
            key = (source_id, dataset)
            if key in index:
                return index[key]
        return latest_for_source(index, source_id) or index.get((source_id, "_source"))

    out: list[DueDecision] = []
    for step in configured_steps:
        if step == "fred":
            out.append(
                evaluate_fred_catalog_due(
                    now=now,
                    freshness=index,
                    series_ids=fred_series_ids,
                )
            )
        elif step == "treasury":
            out.append(
                daily_source_due(
                    step=step,
                    source_id="TREASURY",
                    cadence="D",
                    latest_observation=_source_latest("TREASURY", TREASURY_DATASET),
                    now=now,
                    series_id="DGS10",
                )
            )
        elif step == "finra":
            out.append(
                daily_source_due(
                    step=step,
                    source_id="FINRA_QUERY",
                    cadence="D",
                    latest_observation=_source_latest("FINRA_QUERY"),
                    now=now,
                )
            )
        elif step == "equity":
            now_et = now.astimezone(ET)
            after_close = now_et.hour > 16 or (now_et.hour == 16 and now_et.minute >= 5)
            if not after_close and not is_final_catchup(now):
                out.append(
                    DueDecision(
                        step,
                        "EQUITY_EOD",
                        False,
                        "before_equity_eod_window",
                        "SKIPPED_NOT_DUE",
                        dataset=EQUITY_DATASET,
                    )
                )
            else:
                out.append(
                    daily_source_due(
                        step=step,
                        source_id="EQUITY_EOD",
                        cadence="D",
                        latest_observation=_source_latest("EQUITY_EOD", EQUITY_DATASET),
                        now=now,
                    )
                )
        elif step == "cftc":
            out.append(
                release_calendar_due(
                    step=step,
                    source_id="CFTC_COT",
                    cadence="W",
                    latest_observation=_source_latest("CFTC_COT"),
                    now=now,
                )
            )
        elif step == "eia":
            out.append(
                release_calendar_due(
                    step=step,
                    source_id="EIA_ENERGY",
                    cadence="W",
                    latest_observation=_source_latest("EIA_ENERGY"),
                    now=now,
                )
            )
        elif step in {"options", "vix", "openfigi", "edgar", "legacy_sector"}:
            out.append(DueDecision(step, step.upper(), False, "not_in_catchup_poll_set", "SKIPPED_NOT_DUE"))
        elif step == "yahoo_live":
            out.append(yahoo_live_due(enabled=yahoo_live_fallback_enabled(env), now=now))
        elif step == "yahoo_eod":
            out.append(yahoo_eod_prior_due(enabled=yahoo_eod_fallback_enabled(env), now=now))
        else:
            out.append(DueDecision(step, step.upper(), False, "unknown_step", "SKIPPED_NOT_DUE"))
    return out


def catchup_calendar_lines() -> list[str]:
    """systemd OnCalendar lines: every 10 minutes Mon..Fri 09:15–18:30 America/New_York."""
    lines: list[str] = []
    start = 9 * 60 + 15
    end = 18 * 60 + 30
    for total in range(start, end + 1, 10):
        hour, minute = divmod(total, 60)
        lines.append("OnCalendar=Mon..Fri {0:02d}:{1:02d} America/New_York".format(hour, minute))
    final = "OnCalendar=Mon..Fri 18:30 America/New_York"
    if final not in lines:
        lines.append(final)
    return lines


__all__ = [
    "DueDecision",
    "EQUITY_DATASET",
    "FINAL_CATCHUP",
    "FINAL_GRACE_END",
    "NORMAL_WINDOW_END",
    "TREASURY_DATASET",
    "WINDOW_END",
    "WINDOW_START",
    "build_freshness_index",
    "catchup_calendar_lines",
    "daily_source_due",
    "evaluate_due_steps",
    "evaluate_fred_catalog_due",
    "fred_dataset_key",
    "in_catchup_window",
    "in_normal_polling_window",
    "is_final_catchup",
    "latest_for_dataset",
    "latest_for_fred_series",
    "latest_for_source",
    "release_calendar_due",
    "series_unit_due",
    "yahoo_eod_prior_due",
    "yahoo_live_due",
]
