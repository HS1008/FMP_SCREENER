"""Gross Treasury bill, note, and bond issuance from the Fiscal Data auctions API.

Issuance is the gross accepted auction amount (``total_accepted``), summed by the
calendar month of ``auction_date``. It is not net issuance and not the offering
amount. TIPS, floating-rate notes, and cash-management bills are excluded even
when Treasury labels them as notes, bonds, or bills.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import text

from market_intelligence.catalog import CATALOG_VERSION, META_VALIDATED
from market_intelligence.store import ObservationInput, record_freshness, start_run, finish_run, upsert_macro_series, upsert_observations

AUCTIONS_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
AUCTION_FIELDS = (
    "auction_date,security_type,inflation_index_security,floating_rate,"
    "cash_management_bill_cmb,total_accepted"
)
SOURCE_ID = "TREASURY_FISCAL"
DATASET = "auctions_query"
PAGE_SIZE = 10000
HISTORY_START = date(2000, 1, 1)
INCREMENTAL_DAYS = 450
_ACCEPTED = {"Bill": "TREAS_GROSS_BILL", "Note": "TREAS_GROSS_NOTE", "Bond": "TREAS_GROSS_BOND"}
_YES = {"yes", "y", "true", "1"}

SERIES_NOTES = {
    "TREAS_GROSS_BILL": "Gross accepted Treasury bill auctions, dollars, monthly. Cash-management bills are excluded.",
    "TREAS_GROSS_NOTE": "Gross accepted Treasury note auctions, dollars, monthly. TIPS and floating-rate notes are excluded.",
    "TREAS_GROSS_BOND": "Gross accepted Treasury bond auctions, dollars, monthly. TIPS are excluded.",
}


def _flag(value: Any) -> bool:
    return str(value or "").strip().lower() in _YES


def classify_auction(row: Mapping[str, Any]) -> str | None:
    """Return Bill, Note, or Bond, or None when the auction is not in those groups."""
    kind = str(row.get("security_type") or "").strip()
    if kind not in _ACCEPTED:
        return None
    if _flag(row.get("inflation_index_security")) or _flag(row.get("floating_rate")):
        return None
    if kind == "Bill" and _flag(row.get("cash_management_bill_cmb")):
        return None
    return kind


def _amount(value: Any) -> Decimal | None:
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value or text_value.lower() == "null":
        return None
    try:
        return Decimal(text_value)
    except Exception:
        return None


def _month_start(day: date) -> date:
    return date(day.year, day.month, 1)


def _months_between(start: date, end: date) -> list[date]:
    months: list[date] = []
    cursor = _month_start(start)
    last = _month_start(end)
    while cursor <= last:
        months.append(cursor)
        year = cursor.year + (1 if cursor.month == 12 else 0)
        month = 1 if cursor.month == 12 else cursor.month + 1
        cursor = date(year, month, 1)
    return months


def monthly_gross_issuance(
    rows: list[Mapping[str, Any]],
    *,
    start: date,
    end: date,
) -> dict[str, list[tuple[date, Decimal]]]:
    """Sum accepted dollars by auction month.

    Months inside the completed fetch with no accepted auction of that class are
    zero. That zero is a complete count, not a filled-forward print. Dates after
    ``end`` and amounts that are null are ignored.
    """
    buckets: dict[str, dict[date, Decimal]] = {kind: {} for kind in _ACCEPTED}
    for row in rows:
        kind = classify_auction(row)
        if kind is None:
            continue
        raw_day = str(row.get("auction_date") or "")[:10]
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        if day < start or day > end:
            continue
        amount = _amount(row.get("total_accepted"))
        if amount is None:
            continue
        month = _month_start(day)
        buckets[kind][month] = buckets[kind].get(month, Decimal("0")) + amount
    series: dict[str, list[tuple[date, Decimal]]] = {}
    for kind, series_id in _ACCEPTED.items():
        series[series_id] = [(month, buckets[kind].get(month, Decimal("0"))) for month in _months_between(start, end)]
    return series


def _default_fetch(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "FMP_SCREENER treasury auctions", "Accept": "application/json"})
    with urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Treasury auctions response was not an object")
    return payload


def fetch_auctions(start: date, end: date, *, fetcher: Callable[[str], dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Page the Fiscal Data auctions endpoint for one inclusive date window."""
    fetch = fetcher or _default_fetch
    rows: list[dict[str, Any]] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        query = urlencode(
            {
                "fields": AUCTION_FIELDS,
                "filter": "auction_date:gte:{0},auction_date:lte:{1}".format(start.isoformat(), end.isoformat()),
                "page[size]": PAGE_SIZE,
                "page[number]": page,
                "sort": "auction_date",
            }
        )
        payload = fetch("{0}?{1}".format(AUCTIONS_URL, query))
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("Treasury auctions page {0} did not include a data list".format(page))
        rows.extend(item for item in data if isinstance(item, dict))
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        try:
            total_pages = max(1, int(meta.get("total-pages") or 1))
        except (TypeError, ValueError):
            total_pages = 1
        page += 1
    return rows


def _publish_series(conn) -> None:
    for series_id, notes in SERIES_NOTES.items():
        upsert_macro_series(
            conn,
            series_id=series_id,
            source_id=SOURCE_ID,
            provider_series_id=series_id,
            spec_fields={
                "category": "fiscal",
                "subcategory": "issuance",
                "catalog_version": CATALOG_VERSION,
                "source_url": AUCTIONS_URL,
                "notes": notes,
                "export_scope": "ATTRIBUTION_REQUIRED",
                "catalog_units": "dollars",
                "catalog_label": series_id,
                "aggregation": "period_total",
                "expected_frequency": "M",
            },
            meta={
                "title": notes,
                "units": "Dollars",
                "units_short": "$",
                "frequency": "Monthly",
                "frequency_short": "M",
                "seasonal_adjustment": "Not Seasonally Adjusted",
                "seasonal_adjustment_short": "NSA",
            },
            metadata_status=META_VALIDATED,
            mismatches=[],
        )


def ingest_treasury_auctions(
    engine,
    *,
    parent_run_id: str | None = None,
    today: date | None = None,
    mode: str = "incremental",
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Backfill or refresh monthly gross issuance. Revisions upsert in place."""
    today = today or datetime.now(timezone.utc).date()
    if mode != "max":
        with engine.connect() as conn:
            if auctions_already_refreshed_today(conn, today):
                return {"status": "SKIPPED", "reason": "already_refreshed_today", "mode": mode}
    start = HISTORY_START if mode == "max" else max(HISTORY_START, today - timedelta(days=INCREMENTAL_DAYS))
    rows = fetch_auctions(start, today, fetcher=fetcher)
    monthly = monthly_gross_issuance(rows, start=start, end=today)
    retrieved_at = datetime.now(timezone.utc)
    written: dict[str, int] = {}
    with engine.begin() as conn:
        run_id = start_run(conn, source_id=SOURCE_ID, dataset=DATASET, parent_run_id=parent_run_id)
        _publish_series(conn)
        latest: date | None = None
        for series_id, points in monthly.items():
            observations = [
                ObservationInput(observation_date=day, raw_value=format(amount, "f"))
                for day, amount in points
                if day <= today
            ]
            counts = upsert_observations(conn, series_id=series_id, rows=observations, retrieved_at=retrieved_at, run_id=run_id, today=today)
            written[series_id] = counts.inserted + counts.revised + counts.unchanged
            if points:
                latest = points[-1][0] if latest is None else max(latest, points[-1][0])
        record_freshness(
            conn,
            source_id=SOURCE_ID,
            dataset=DATASET,
            cadence="M",
            transport_status="OK",
            latest_observation=today,
            success=True,
            error_redacted=None,
            run_id=run_id,
            today=today,
            metadata_status=META_VALIDATED,
            latest_observation_retrieved_at=retrieved_at,
            series_id="TREAS_GROSS_BILL",
            coverage_status="OK",
        )
        finish_run(conn, run_id, status="SUCCEEDED", counts={"inserted": sum(written.values())}, details={"mode": mode, "auctions": len(rows), "start": start.isoformat()})
    return {"status": "SUCCEEDED", "mode": mode, "start": start.isoformat(), "auctions": len(rows), "series": written, "latest_month": None if latest is None else latest.isoformat()}


def auctions_already_refreshed_today(conn, today: date) -> bool:
    row = conn.execute(
        text(
            """
            SELECT latest_observation_date
            FROM mi_data_freshness
            WHERE source_id = :source_id AND dataset = :dataset
            """
        ),
        {"source_id": SOURCE_ID, "dataset": DATASET},
    ).first()
    if row is None or row[0] is None:
        return False
    return row[0] >= today
