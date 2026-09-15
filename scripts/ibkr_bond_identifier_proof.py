"""Bounded READ-ONLY IBKR bond identifier proof.

Resolves known official CUSIP/ISIN via reqContractDetails (not reqMatchingSymbols),
optionally requests market data, and classifies the outcome. No orders. No persistence.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ibapi.contract import Contract

from ibkr_collector.readonly_client import ReadOnlyTwsClient
from ibkr_collector.values import finite_or_none, market_data_type_label

logger = logging.getLogger("ibkr_bond_id_proof")

HANDSHAKE_WAIT_SEC = 15.0
CONTRACT_WAIT_SEC = 12.0
QUOTE_WAIT_SEC = 8.0


def _attempts(cusip: str | None, isin: str | None) -> list[tuple[str, Contract]]:
    out: list[tuple[str, Contract]] = []
    if cusip:
        c1 = Contract()
        c1.secType = "BOND"
        c1.currency = "USD"
        c1.exchange = "SMART"
        c1.secIdType = "CUSIP"
        c1.secId = cusip
        out.append(("secIdType=CUSIP", c1))

        c2 = Contract()
        c2.secType = "BOND"
        c2.currency = "USD"
        c2.exchange = "SMART"
        c2.symbol = cusip
        out.append(("symbol=CUSIP", c2))

        c3 = Contract()
        c3.secType = "BOND"
        c3.currency = "USD"
        c3.exchange = "SMART"
        c3.cusip = cusip
        out.append(("contract.cusip", c3))
    if isin:
        c4 = Contract()
        c4.secType = "BOND"
        c4.currency = "USD"
        c4.exchange = "SMART"
        c4.secIdType = "ISIN"
        c4.secId = isin
        out.append(("secIdType=ISIN", c4))
    return out


def _sanitize_ticks(ticks: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("bid", "ask", "last", "close", "bid_size", "ask_size", "yield"):
        if key not in ticks:
            continue
        out[key] = finite_or_none(ticks.get(key))
    return out


def _classify_quote(ticks: dict[str, Any], md_label: str | None, errors: list[dict[str, Any]]) -> str:
    entitlement_codes = {354, 10168, 10089, 10197, 162, 10167}
    entitlement = [
        e
        for e in errors
        if e.get("kind") == "entitlement" or int(e.get("error_code") or 0) in entitlement_codes
    ]
    bid = ticks.get("bid")
    ask = ticks.get("ask")
    last = ticks.get("last")
    close = ticks.get("close")
    if bid is not None and ask is not None:
        return "LIVE_BID_ASK" if (md_label or "").upper() in {"LIVE", "REALTIME", "REAL_TIME"} else "DELAYED_BID_ASK"
    if bid is not None or ask is not None:
        return "ONE_SIDED_QUOTE"
    if last is not None and close is None:
        return "LAST_ONLY"
    if close is not None and bid is None and ask is None and last is None:
        return "HISTORICAL_CLOSE_ONLY"
    if entitlement:
        return "ENTITLEMENT_NO_QUOTE"
    if (md_label or "").upper() in {"FROZEN", "DELAYED_FROZEN"}:
        return "FROZEN_NO_QUOTE"
    return "NO_QUOTE"


def _classify_ratings(details: list[dict[str, Any]]) -> tuple[str, Any]:
    ratings = []
    for row in details:
        value = row.get("ratings")
        if value not in (None, "", []):
            ratings.append(value)
    if not ratings:
        return "RATINGS_ABSENT", None
    return "RATINGS_PRESENT", ratings[0]


def _classify(details: list[dict[str, Any]], ticks: dict[str, Any], errors: list[dict[str, Any]]) -> str:
    entitlement_codes = {354, 10168, 10089, 10197, 162}
    entitlement = [
        e
        for e in errors
        if e.get("kind") == "entitlement" or int(e.get("error_code") or 0) in entitlement_codes
    ]
    con_ids = [int(d.get("con_id") or 0) for d in details if int(d.get("con_id") or 0) > 0]
    if not con_ids:
        return "CONTRACT_NOT_FOUND"
    has_quote = any(ticks.get(k) is not None for k in ("bid", "ask", "last", "close"))
    if has_quote:
        return "IDENTIFIER_RESOLVED_QUOTE_AVAILABLE"
    if entitlement:
        return "IDENTIFIER_RESOLVED_ENTITLEMENT_REQUIRED"
    return "IDENTIFIER_RESOLVED_NO_QUOTE"


def _capability_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if local:
        return Path(local) / "FMP_SCREENER" / "ibkr_collector" / "bond_capability_latest.json"
    return Path.home() / ".fmp_screener" / "ibkr_collector" / "bond_capability_latest.json"


def _write_capability_evidence(report: dict[str, Any]) -> str:
    import os

    evidence = {
        "observed_at": report.get("finished_at") or datetime.now(timezone.utc).isoformat(),
        "label": report.get("label"),
        "asset_class": report.get("asset_class"),
        "discovery_classification": report.get("classification"),
        "quote_classification": report.get("quote_classification"),
        "ratings_classification": report.get("ratings_classification"),
        "ratings_sample_present": report.get("ratings_classification") == "RATINGS_PRESENT",
        "market_data_type": report.get("market_data_type_label"),
        "con_id": (report.get("contract_details") or [{}])[0].get("con_id") if report.get("contract_details") else None,
        "resolved_via": report.get("resolved_via"),
        "storage_rights": "RIGHTS_PENDING",
        "orders_attempted": False,
    }
    path = _capability_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    return str(path)


def run_proof(
    *,
    host: str,
    port: int,
    client_id: int,
    label: str,
    asset_class: str,
    cusip: str | None,
    isin: str | None,
    request_quote: bool,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "asset_class": asset_class,
        "identifier": {"cusip": cusip, "isin": isin},
        "host": host,
        "port": port,
        "client_id": client_id,
        "orders_attempted": False,
        "forbidden_methods_invoked": [],
        "persistence_attempted": False,
        "storage_rights": "RIGHTS_PENDING",
        "attempts": [],
    }
    client = ReadOnlyTwsClient()
    reader = threading.Thread(target=client.run, name="ibkr-bond-proof-reader", daemon=True)
    subscribed: list[int] = []
    try:
        client.connect(host, port, clientId=client_id)
        reader.start()
        if not client.handshake.wait(HANDSHAKE_WAIT_SEC):
            report["handshake_ok"] = False
            report["classification"] = "PROVIDER_SUPPORT_REQUIRED"
            report["fatal_error"] = "handshake_timeout"
            return report
        report["handshake_ok"] = True
        # Market-data type is chosen per quote attempt below (live then delayed).
        resolved_details: list[dict[str, Any]] = []
        for method, contract in _attempts(cusip, isin):
            before_errors = len(client.errors)
            detail_req = client.next_req_id()
            client.wait_event(detail_req, client.contract_details_done)
            client.reqContractDetails(detail_req, contract)
            done = client.contract_details_done[detail_req].wait(CONTRACT_WAIT_SEC)
            details = list(client.contract_details.get(detail_req, []))
            new_errors = list(client.errors[before_errors:])
            attempt = {
                "method": method,
                "completed": done,
                "detail_count": len(details),
                "details": details[:2],
                "errors": new_errors[:8],
            }
            report["attempts"].append(attempt)
            if details and int(details[0].get("con_id") or 0) > 0:
                resolved_details = details
                report["resolved_via"] = method
                break
            time.sleep(0.35)

        ticks: dict[str, Any] = {}
        if request_quote and resolved_details and int(resolved_details[0].get("con_id") or 0) > 0:
            resolved = Contract()
            resolved.conId = int(resolved_details[0]["con_id"])
            resolved.exchange = str(resolved_details[0].get("exchange") or "SMART") or "SMART"
            resolved.currency = "USD"
            resolved.secType = "BOND"
            for md_type in (1, 3):
                try:
                    client.reqMarketDataType(md_type)
                    report["requested_market_data_type"] = md_type
                except Exception as exc:  # noqa: BLE001
                    report["requested_market_data_type_error"] = exc.__class__.__name__
                quote_req = client.next_req_id()
                subscribed.append(quote_req)
                client.ticks.setdefault(quote_req, {})
                before_errors = len(client.errors)
                client.reqMktData(quote_req, resolved, "", False, False, [])
                time.sleep(QUOTE_WAIT_SEC)
                ticks = dict(client.ticks.get(quote_req, {}))
                report["market_data_type_code"] = client.market_data_types.get(quote_req)
                try:
                    client.cancelMktData(quote_req)
                except Exception:  # noqa: BLE001
                    logger.debug("cancelMktData failed", exc_info=True)
                clean = _sanitize_ticks(ticks)
                if any(clean.get(k) is not None for k in ("bid", "ask", "last", "close")):
                    break
                new_entitlement = [
                    e
                    for e in client.errors[before_errors:]
                    if e.get("kind") == "entitlement" or int(e.get("error_code") or 0) in {354, 10168, 10089, 10197, 2186}
                ]
                if not new_entitlement:
                    break
                ticks = {}

        report["contract_details"] = resolved_details[:3]
        clean_ticks = _sanitize_ticks(ticks)
        md_code = report.get("market_data_type_code")
        md_label = market_data_type_label(int(md_code)) if md_code is not None else None
        report["market_data_type_label"] = md_label
        report["quote_ticks"] = clean_ticks
        report["errors"] = list(client.errors)[:40]
        report["classification"] = _classify(resolved_details, clean_ticks, list(client.errors))
        report["quote_classification"] = (
            _classify_quote(clean_ticks, md_label, list(client.errors)) if resolved_details else "NO_CONTRACT"
        )
        ratings_class, ratings_value = _classify_ratings(resolved_details)
        report["ratings_classification"] = ratings_class
        report["ratings_value_present"] = ratings_class == "RATINGS_PRESENT"
        # Do not echo raw rating text into stdout by default; presence only.
        report["ratings_observed"] = bool(ratings_value)
    except Exception as exc:  # noqa: BLE001
        report["handshake_ok"] = bool(report.get("handshake_ok"))
        report["fatal_error"] = "{0}: {1}".format(exc.__class__.__name__, exc)
        report["classification"] = report.get("classification") or "PROVIDER_SUPPORT_REQUIRED"
    finally:
        for req_id in subscribed:
            try:
                client.cancelMktData(req_id)
            except Exception:  # noqa: BLE001
                pass
        try:
            if client.isConnected():
                client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            report["capability_evidence_path"] = _write_capability_evidence(report)
        except Exception as exc:  # noqa: BLE001
            report["capability_evidence_error"] = exc.__class__.__name__
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7496)
    parser.add_argument("--client-id", type=int, default=74)
    parser.add_argument("--label", required=True)
    parser.add_argument("--asset-class", choices=["corporate", "municipal", "treasury", "other"], required=True)
    parser.add_argument("--cusip", default=None)
    parser.add_argument("--isin", default=None)
    parser.add_argument("--no-quote", action="store_true")
    parser.add_argument("--out", default=None)
    ns = parser.parse_args(argv)
    if not ns.cusip and not ns.isin:
        parser.error("provide --cusip and/or --isin")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = run_proof(
        host=ns.host,
        port=ns.port,
        client_id=ns.client_id,
        label=ns.label,
        asset_class=ns.asset_class,
        cusip=ns.cusip,
        isin=ns.isin,
        request_quote=not ns.no_quote,
    )
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if ns.out:
        path = Path(ns.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
