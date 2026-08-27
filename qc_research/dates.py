"""QuantConnect date fields and equity-chart request bounds.

Official BacktestResult uses backtestStart / backtestEnd / created.
`created` is when the backtest was launched, not the simulation start.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_qc_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 1e12:
            number = number / 1000.0
        if number > 1e9:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
    ):
        try:
            parsed = datetime.strptime(str(value).strip().replace("Z", ""), fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def qc_simulation_dates(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Prefer official backtestStart/backtestEnd. Never treat created as start."""
    detail = detail or {}
    start_raw = (
        detail.get("backtestStart")
        or detail.get("backtest_start")
        or detail.get("startDate")
        or detail.get("start_date")
    )
    end_raw = (
        detail.get("backtestEnd")
        or detail.get("backtest_end")
        or detail.get("endDate")
        or detail.get("end_date")
    )
    created_raw = detail.get("created") or detail.get("created_at")
    start = parse_qc_datetime(start_raw)
    end = parse_qc_datetime(end_raw)
    created = parse_qc_datetime(created_raw)
    start_source = None
    if detail.get("backtestStart") or detail.get("backtest_start"):
        start_source = "backtestStart"
    elif detail.get("startDate") or detail.get("start_date"):
        start_source = "startDate"
    end_source = None
    if detail.get("backtestEnd") or detail.get("backtest_end"):
        end_source = "backtestEnd"
    elif detail.get("endDate") or detail.get("end_date"):
        end_source = "endDate"
    return {
        "backtest_start": start,
        "backtest_end": end,
        "created": created,
        "backtest_start_source": start_source,
        "backtest_end_source": end_source,
        "created_is_not_simulation_start": True,
    }


def chart_request_window(
    detail: dict[str, Any] | None = None,
    *,
    existing_row: dict[str, Any] | None = None,
    now: float | None = None,
    count: int = 1000,
) -> dict[str, int]:
    """Unix start/end for /backtests/chart/read.

    Uses historical backtestStart/backtestEnd when available. If those
    dates are missing, uses start=0 and end=now. Never uses `created`.
    """
    import time

    payload = dict(existing_row or {})
    if detail:
        payload.update(detail)
    dates = qc_simulation_dates(payload)
    start = dates["backtest_start"]
    end = dates["backtest_end"]
    now_ts = int(now if now is not None else time.time())
    if start is not None:
        start_unix = int(start.timestamp())
    else:
        start_unix = 0
    if end is not None:
        end_unix = int(end.timestamp())
    else:
        end_unix = now_ts
    if end_unix < start_unix:
        end_unix = now_ts
    return {
        "start": int(start_unix),
        "end": int(end_unix),
        "count": min(int(count), 1000),
    }


def created_unix(detail: dict[str, Any] | None) -> int | None:
    created = qc_simulation_dates(detail or {}).get("created")
    if created is None:
        return None
    return int(created.timestamp())
