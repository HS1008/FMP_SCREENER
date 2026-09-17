"""Source-aware due evaluation for the weekday 10-minute catch-up window.

Mon..Fri 09:15–18:30 America/New_York: a lightweight heartbeat may run every 10 minutes.
Provider calls happen only when a dataset is independently due.

Outcomes map onto existing run/freshness vocabulary via outcome labels:
SKIPPED_NOT_DUE, SKIPPED_ALREADY_CURRENT, SUCCESS_NEW_DATA, SUCCESS_NO_CHANGE,
FAILED_TRANSPORT, FAILED_PARSE, UNAVAILABLE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

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
WINDOW_END = time(18, 30)
FINAL_CATCHUP = time(18, 30)


@dataclass(frozen=True)
class DueDecision:
    step: str
    source_id: str
    due: bool
    reason: str
    outcome_if_skip: str = "SKIPPED_NOT_DUE"
    details: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "source_id": self.source_id,
            "due": self.due,
            "reason": self.reason,
            "outcome_if_skip": self.outcome_if_skip,
            "details": dict(self.details or {}),
        }


def in_catchup_window(now: datetime | None = None) -> bool:
    """True on Mon–Fri between 09:15 and 18:30 America/New_York (DST-safe)."""
    now_et = (now or datetime.now(tz=ET)).astimezone(ET)
    if now_et.weekday() >= 5:
        return False
    t = now_et.timetz().replace(tzinfo=None)
    return WINDOW_START <= t <= WINDOW_END


def is_final_catchup(now: datetime | None = None) -> bool:
    now_et = (now or datetime.now(tz=ET)).astimezone(ET)
    if now_et.weekday() >= 5:
        return False
    t = now_et.timetz().replace(tzinfo=None)
    return t.hour == FINAL_CATCHUP.hour and t.minute >= FINAL_CATCHUP.minute and t.minute < FINAL_CATCHUP.minute + 10


def _obs_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


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
        # Conservative: still check when we have no policy, unless already same-day.
        if latest_observation is not None and latest_observation >= now.astimezone(ET).date():
            return DueDecision(step, source_id, False, "already_current", "SKIPPED_ALREADY_CURRENT")
        return DueDecision(step, source_id, True, "no_policy_conservative_check", details={"cadence": cadence})
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
        )
    if assessment.status == AWAITING_RELEASE and not is_final_catchup(now):
        return DueDecision(
            step,
            source_id,
            False,
            "awaiting_release",
            "SKIPPED_NOT_DUE",
            {"expected": expected.isoformat(), "status": assessment.status},
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
        return DueDecision(step, source_id, False, "no_policy_skip", "SKIPPED_NOT_DUE")
    expected = expected_latest_published(policy=policy, now=now)
    if latest_observation is not None and latest_observation >= expected:
        return DueDecision(step, source_id, False, "already_current", "SKIPPED_ALREADY_CURRENT")
    assessment = assess_freshness(
        latest_observation,
        cadence=cadence,
        series_id=series_id,
        source_id=source_id,
        now=now,
        transport_status="OK",
    )
    if assessment.status == AWAITING_RELEASE and not is_final_catchup(now):
        return DueDecision(step, source_id, False, "awaiting_release", "SKIPPED_NOT_DUE")
    return DueDecision(
        step,
        source_id,
        True,
        "release_catch_up",
        details={
            "expected": expected.isoformat(),
            "latest": latest_observation.isoformat() if latest_observation else None,
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
    # RTH only: fill missing/stale IBKR quotes. Cadence follows the 10-minute catch-up heartbeat
    # (within the ~15-minute freshness window). Symbol selection stays IBKR-stale-only in the writer.
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
    latest_by_source: Mapping[str, date | None],
    configured_steps: Sequence[str],
) -> list[DueDecision]:
    """Return due decisions for configured refresh steps inside the catch-up window."""
    if not in_catchup_window(now):
        return [
            DueDecision(step, step.upper(), False, "outside_catchup_window", "SKIPPED_NOT_DUE")
            for step in configured_steps
        ]
    out: list[DueDecision] = []
    for step in configured_steps:
        if step == "fred":
            out.append(
                daily_source_due(
                    step=step,
                    source_id="FRED",
                    cadence="MIXED",
                    latest_observation=latest_by_source.get("FRED"),
                    now=now,
                )
            )
        elif step == "treasury":
            out.append(
                daily_source_due(
                    step=step,
                    source_id="TREASURY",
                    cadence="D",
                    latest_observation=latest_by_source.get("TREASURY"),
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
                    latest_observation=latest_by_source.get("FINRA_QUERY"),
                    now=now,
                )
            )
        elif step == "equity":
            # IBKR collector publishes EOD; DO refresh reports state — due after close / final catch-up.
            now_et = now.astimezone(ET)
            after_close = now_et.hour > 16 or (now_et.hour == 16 and now_et.minute >= 5)
            if not after_close and not is_final_catchup(now):
                out.append(DueDecision(step, "EQUITY_EOD", False, "before_equity_eod_window", "SKIPPED_NOT_DUE"))
            else:
                out.append(
                    daily_source_due(
                        step=step,
                        source_id="EQUITY_EOD",
                        cadence="D",
                        latest_observation=latest_by_source.get("EQUITY_EOD"),
                        now=now,
                    )
                )
        elif step == "cftc":
            out.append(
                release_calendar_due(
                    step=step,
                    source_id="CFTC_COT",
                    cadence="W",
                    latest_observation=latest_by_source.get("CFTC_COT"),
                    now=now,
                )
            )
        elif step == "eia":
            out.append(
                release_calendar_due(
                    step=step,
                    source_id="EIA_ENERGY",
                    cadence="W",
                    latest_observation=latest_by_source.get("EIA_ENERGY"),
                    now=now,
                )
            )
        elif step in {"options", "vix", "openfigi", "edgar", "legacy_sector"}:
            # Keep on-demand / rights-gated / low-frequency out of blind 10-minute polling.
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
    "FINAL_CATCHUP",
    "WINDOW_END",
    "WINDOW_START",
    "catchup_calendar_lines",
    "daily_source_due",
    "evaluate_due_steps",
    "in_catchup_window",
    "is_final_catchup",
    "release_calendar_due",
    "yahoo_eod_prior_due",
    "yahoo_live_due",
]
