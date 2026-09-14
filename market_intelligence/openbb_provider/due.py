"""Dataset-specific due checks for Cboe options / VX_EOD. No provider calls."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from market_intelligence.calendars import NY_TZ, is_session, last_completed_session, previous_session
from market_intelligence.openbb_provider.config import intraday_snapshots_enabled


def ny_session_date(now: datetime) -> date:
    local = now.astimezone(NY_TZ) if now.tzinfo else now.replace(tzinfo=NY_TZ)
    return local.date()


def target_session(now: datetime) -> date:
    local_date = ny_session_date(now)
    if is_session(local_date, "NYSE"):
        local = now.astimezone(NY_TZ)
        # Conservative default: one post-publication snapshot after 16:00 ET.
        if local.hour > 16 or (local.hour == 16 and local.minute >= 5):
            return local_date
        return previous_session(local_date, "NYSE")
    return last_completed_session(now, "NYSE")


def options_due(
    *,
    last_published_session: date | None,
    last_attempt_status: str | None,
    now: datetime,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    wanted = target_session(now)
    if last_published_session == wanted and not intraday_snapshots_enabled(env):
        return {"due": False, "reason": "already_published_for_session", "session_date": wanted.isoformat()}
    if last_published_session == wanted and last_attempt_status in {"SUCCEEDED", "UNCHANGED"}:
        return {"due": False, "reason": "already_published_for_session", "session_date": wanted.isoformat()}
    return {"due": True, "reason": "catch_up_or_first", "session_date": wanted.isoformat()}


def vix_due(*, last_published_date: date | None, now: datetime) -> dict[str, Any]:
    wanted = target_session(now)
    if last_published_date == wanted:
        return {"due": False, "reason": "already_published_for_session", "session_date": wanted.isoformat()}
    return {"due": True, "reason": "catch_up_or_first", "session_date": wanted.isoformat()}
