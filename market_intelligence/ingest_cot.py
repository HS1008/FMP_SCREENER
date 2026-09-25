"""Persist CFTC futures-only COT extracts. Combined reports are never mixed in."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy import text

from market_intelligence.cot_client import COT_DATASETS, COT_SOURCE_ID, COTDataset
from market_intelligence.store import RUN_FAILED, RUN_SUCCEEDED, finish_run, record_freshness, start_run, utcnow

TFF_CATEGORIES = {
    "Dealer": ("dealer_positions_long_all", "dealer_positions_short_all", "dealer_positions_spread_all"),
    "AssetMgr": ("asset_mgr_positions_long", "asset_mgr_positions_short", "asset_mgr_positions_spread"),
    "LevMoney": ("lev_money_positions_long", "lev_money_positions_short", "lev_money_positions_spread"),
    "OtherRept": ("other_rept_positions_long", "other_rept_positions_short", "other_rept_positions_spread"),
    "NonRept": ("nonrept_positions_long", "nonrept_positions_short", None),
}

DISAGG_CATEGORIES = {
    "ProdMerc": ("prod_merc_positions_long", "prod_merc_positions_short", None),
    "Swap": ("swap_positions_long_all", "swap_positions_short_all", "swap_positions_spread_all"),
    "MMoney": ("m_money_positions_long", "m_money_positions_short", "m_money_positions_spread"),
    "OtherRept": ("other_rept_positions_long", "other_rept_positions_short", "other_rept_positions_spread"),
    "NonRept": ("nonrept_positions_long_all", "nonrept_positions_short_all", None),
}


def _num(value: Any) -> Decimal | None:
    if value in (None, "", "."):
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _date(value: Any) -> date | None:
    raw = str(value or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def explode_row(dataset: COTDataset, row: Mapping[str, Any]) -> list[dict[str, Any]]:
    categories = TFF_CATEGORIES if dataset.report_family == "TFF" else DISAGG_CATEGORIES
    code = str(row.get("cftc_contract_market_code") or row.get("cftc_commodity_code") or "").strip()
    position_date = _date(row.get("report_date_as_yyyy_mm_dd") or row.get("report_date_as_mm_dd_yyyy"))
    open_interest = _num(row.get("open_interest_all") or row.get("open_interest"))
    payload_hash = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    out = []
    for category, fields in categories.items():
        long_f, short_f, spread_f = fields
        out.append(
            {
                "report_family": dataset.report_family,
                "futonly_or_combined": dataset.futonly_or_combined,
                "contract_code": code,
                "position_date": position_date,
                "trader_category": category,
                "long_position": _num(row.get(long_f)),
                "short_position": _num(row.get(short_f)),
                "spreading_position": _num(row.get(spread_f)) if spread_f else None,
                "open_interest": open_interest,
                "payload_hash": payload_hash,
                "market_name": row.get("market_and_exchange_names"),
            }
        )
    return out


def persist_rows(conn, dataset: COTDataset, rows: list[dict[str, Any]], *, run_id: str) -> int:
    retrieved = utcnow()
    written = 0
    for raw in rows:
        for item in explode_row(dataset, raw):
            if not item["contract_code"] or item["position_date"] is None:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO mi_cot_contracts (contract_code, market_name, report_family, futonly_or_combined)
                    VALUES (:code, :name, :family, :kind)
                    ON CONFLICT (contract_code) DO UPDATE SET market_name = COALESCE(EXCLUDED.market_name, mi_cot_contracts.market_name)
                    """
                ),
                {"code": item["contract_code"], "name": item["market_name"], "family": dataset.report_family, "kind": dataset.futonly_or_combined},
            )
            conn.execute(
                text(
                    """
                    UPDATE mi_cot_positions SET is_current = FALSE
                    WHERE report_family = :family AND futonly_or_combined = :kind AND contract_code = :code
                      AND position_date = :pdt AND trader_category = :cat AND is_current
                    """
                ),
                {
                    "family": item["report_family"],
                    "kind": item["futonly_or_combined"],
                    "code": item["contract_code"],
                    "pdt": item["position_date"],
                    "cat": item["trader_category"],
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mi_cot_positions (
                        report_family, futonly_or_combined, contract_code, position_date, trader_category,
                        long_position, short_position, spreading_position, open_interest, revision_seq, is_current,
                        available_at, available_at_basis, retrieved_at, ingestion_run_id, payload_hash
                    ) VALUES (
                        :family, :kind, :code, :pdt, :cat, :long, :short, :spread, :oi,
                        COALESCE((SELECT MAX(revision_seq) FROM mi_cot_positions
                                  WHERE report_family = :family AND futonly_or_combined = :kind AND contract_code = :code
                                    AND position_date = :pdt AND trader_category = :cat), 0) + 1,
                        TRUE, :retrieved, 'FIRST_SEEN', :retrieved, :run_id, :hash
                    )
                    """
                ),
                {
                    "family": item["report_family"],
                    "kind": item["futonly_or_combined"],
                    "code": item["contract_code"],
                    "pdt": item["position_date"],
                    "cat": item["trader_category"],
                    "long": item["long_position"],
                    "short": item["short_position"],
                    "spread": item["spreading_position"],
                    "oi": item["open_interest"],
                    "retrieved": retrieved,
                    "run_id": run_id,
                    "hash": item["payload_hash"],
                },
            )
            written += 1
    return written


def net_and_pct(long_pos: Decimal | None, short_pos: Decimal | None, open_interest: Decimal | None) -> dict[str, Any]:
    if long_pos is None or short_pos is None:
        return {"net": None, "net_oi": None, "status": "UNAVAILABLE", "reason": "missing_category"}
    net = long_pos - short_pos
    if open_interest is None:
        return {"net": net, "net_oi": None, "status": "UNAVAILABLE", "reason": "missing_oi"}
    if open_interest == 0:
        return {"net": net, "net_oi": None, "status": "UNAVAILABLE", "reason": "zero_oi"}
    return {"net": net, "net_oi": net / open_interest, "status": "OK", "reason": None}


def ingest_cot(engine, client, *, parent_run_id: str | None = None, since: str | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {"source_id": COT_SOURCE_ID, "failed": False, "datasets": []}
    with engine.begin() as conn:
        run_id = start_run(conn, source_id=COT_SOURCE_ID, dataset="cot_futures_only", parent_run_id=parent_run_id)
        try:
            for dataset in COT_DATASETS:
                rows = client.fetch_pages(dataset, since=since)
                written = persist_rows(conn, dataset, rows, run_id=run_id)
                latest = None
                if rows:
                    latest = _date(rows[-1].get("report_date_as_yyyy_mm_dd"))
                record_freshness(
                    conn,
                    source_id=COT_SOURCE_ID,
                    dataset="{0}_{1}".format(dataset.report_family, dataset.futonly_or_combined),
                    cadence="W",
                    transport_status="OK",
                    latest_observation=latest,
                    success=True,
                    error_redacted=None,
                    run_id=run_id,
                )
                report["datasets"].append({"dataset_id": dataset.dataset_id, "written": written})
            finish_run(conn, run_id, status=RUN_SUCCEEDED)
        except Exception as exc:  # noqa: BLE001
            finish_run(conn, run_id, status=RUN_FAILED, error_redacted=exc.__class__.__name__)
            report["failed"] = True
            report["error"] = exc.__class__.__name__
    return report
