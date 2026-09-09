"""Bounded, read-only TWS diagnostic: handshake, server time, SPY quote, bond discovery."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ibkr_collector import COLLECTOR_VERSION, DEFAULT_CLIENT_ID, DEFAULT_TWS_HOST, DEFAULT_TWS_PORT
from ibkr_collector.readonly_client import ReadOnlyTwsClient
from ibkr_collector.values import market_data_type_label, utcnow

logger = logging.getLogger("ibkr_collector.diagnostic")

SPY_GENERIC_TICKS = ""  # default bid/ask/last/close only
QUOTE_WAIT_SEC = 12.0
HANDSHAKE_WAIT_SEC = 15.0
CONTRACT_WAIT_SEC = 10.0
SYMBOL_WAIT_SEC = 8.0
BOND_PATTERN = "US-T"  # documented matching-symbols search; not a full market scan


def probe_socket(host: str, port: int, timeout: float = 2.0) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {
                "ok": True,
                "host": host,
                "port": port,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "error": None,
            }
    except OSError as exc:
        return {
            "ok": False,
            "host": host,
            "port": port,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": "{0}: {1}".format(exc.__class__.__name__, exc),
        }


def _stock_contract(symbol: str, *, primary: str | None = None):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    if primary:
        contract.primaryExchange = primary
    return contract


def _cancel_our_subscriptions(client: ReadOnlyTwsClient, req_ids: list[int]) -> None:
    for req_id in req_ids:
        try:
            client.cancelMktData(req_id)
        except Exception:
            logger.debug("cancelMktData req=%s failed during cleanup", req_id, exc_info=True)


def run_diagnostic(
    *,
    host: str = DEFAULT_TWS_HOST,
    port: int = DEFAULT_TWS_PORT,
    client_id: int = DEFAULT_CLIENT_ID,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "collector_version": COLLECTOR_VERSION,
        "started_at": utcnow().isoformat(),
        "tws_host": host,
        "tws_port": port,
        "client_id": client_id,
        "socket": None,
        "handshake": None,
        "server_time": None,
        "spy": None,
        "bond_discovery": None,
        "errors": [],
        "cleanup": None,
    }
    socket_probe = probe_socket(host, port)
    report["socket"] = socket_probe
    if not socket_probe["ok"]:
        report["handshake"] = {"ok": False, "state": "WAITING_FOR_TWS", "detail": "tcp port closed"}
        report["finished_at"] = utcnow().isoformat()
        return report

    client = ReadOnlyTwsClient()
    reader = threading.Thread(target=client.run, name="ibkr-diag-reader", daemon=True)
    subscribed: list[int] = []
    try:
        client.connect(host, port, client_id)
        reader.start()
        handshake_ok = client.handshake.wait(HANDSHAKE_WAIT_SEC)
        report["handshake"] = {
            "ok": handshake_ok,
            "state": "API_AUTHENTICATED" if handshake_ok else "SOCKET_OPEN_HANDSHAKE_TIMEOUT",
            "next_valid_id_present": client.next_valid_id is not None,
            "is_connected": bool(client.isConnected()),
        }
        if not handshake_ok:
            report["errors"] = list(client.errors)
            report["finished_at"] = utcnow().isoformat()
            return report

        client.reqCurrentTime()
        if client.server_time_event.wait(5.0) and client.server_time_unix:
            report["server_time"] = {
                "unix": client.server_time_unix,
                "utc": datetime.fromtimestamp(client.server_time_unix, tz=timezone.utc).isoformat(),
            }
        else:
            report["server_time"] = {"unix": None, "utc": None, "error": "timeout"}

        # Type 3 = delayed if unentitled; TWS still returns live when the session has it.
        client.reqMarketDataType(3)
        spy_req = client.next_req_id()
        client.wait_event(spy_req, client.contract_details_done)
        client.reqContractDetails(spy_req, _stock_contract("SPY", primary="ARCA"))
        details_ok = client.contract_details_done[spy_req].wait(CONTRACT_WAIT_SEC)
        spy_details = client.contract_details.get(spy_req, [])
        quote_req = client.next_req_id()
        subscribed.append(quote_req)
        spy_contract = _stock_contract("SPY", primary="ARCA")
        if spy_details:
            qualified = spy_details[0]
            if qualified.get("con_id"):
                spy_contract.conId = int(qualified["con_id"])
        client.reqMktData(quote_req, spy_contract, SPY_GENERIC_TICKS, False, False, [])
        deadline = time.monotonic() + QUOTE_WAIT_SEC
        while time.monotonic() < deadline:
            ticks = dict(client.ticks.get(quote_req) or {})
            if any(ticks.get(k) is not None for k in ("bid", "ask", "last", "close")):
                break
            time.sleep(0.25)
        ticks = dict(client.ticks.get(quote_req) or {})
        md_code = client.market_data_types.get(quote_req, ticks.get("market_data_type"))
        if md_code is None and ticks.get("delayed_ticks"):
            md_label = "DELAYED"
        elif md_code is None and any(ticks.get(k) is not None for k in ("bid", "ask", "last", "close")):
            md_label = "LIVE"  # default stream without an explicit type callback
        elif md_code is None:
            md_label = "UNAVAILABLE"
        else:
            md_label = market_data_type_label(int(md_code))
        spy_entitlement = [
            e for e in client.errors if e.get("req_id") in {spy_req, quote_req} and e.get("kind") == "entitlement"
        ]
        report["spy"] = {
            "contract_details_ok": details_ok,
            "contract_fields": spy_details[:1],
            "quote_req_id": quote_req,
            "ticks": {k: ticks.get(k) for k in ("bid", "ask", "last", "close", "bid_size", "ask_size", "last_size", "last_timestamp")},
            "returned_tick_keys": sorted(ticks.keys()),
            "raw_tick_count": len(client.raw_ticks),
            "raw_ticks_sample": list(client.raw_ticks)[:12],
            "market_data_type": md_label,
            "market_data_type_code": md_code,
            "entitlement_errors": spy_entitlement,
        }

        time.sleep(1.05)  # documented 1s pacing between reqMatchingSymbols calls
        match_req = client.next_req_id()
        client.wait_event(match_req, client.symbol_samples_done)
        client.reqMatchingSymbols(match_req, BOND_PATTERN)
        match_ok = client.symbol_samples_done[match_req].wait(SYMBOL_WAIT_SEC)
        samples = client.symbol_samples.get(match_req, [])
        bonds = [row for row in samples if str(row.get("sec_type") or "").upper() == "BOND"]
        identifiable = [row for row in bonds if int(row.get("con_id") or 0) > 0]
        bond_details: list[dict[str, Any]] = []
        if identifiable:
            first = identifiable[0]
            detail_req = client.next_req_id()
            from ibapi.contract import Contract

            contract = Contract()
            contract.conId = int(first["con_id"])
            contract.exchange = "SMART"
            client.wait_event(detail_req, client.contract_details_done)
            client.reqContractDetails(detail_req, contract)
            client.contract_details_done[detail_req].wait(CONTRACT_WAIT_SEC)
            bond_details = client.contract_details.get(detail_req, [])
        report["bond_discovery"] = {
            "method": "reqMatchingSymbols",
            "pattern": BOND_PATTERN,
            "completed": match_ok,
            "sample_count": len(samples),
            "sample_sec_types": sorted({str(r.get("sec_type")) for r in samples}),
            "samples": samples[:16],
            "bond_matches": bonds[:8],
            "identifiable_bond_matches": identifiable[:8],
            "bond_contract_fields": bond_details[:1],
            "note": (
                "Matching returned BOND issuer names with conId=0 (not a tradeable identifier). "
                "Cash-bond qualification needs CUSIP/ISIN/conId; no such identifier was returned, "
                "so no cash-bond market-data subscription was opened."
                if bonds and not identifiable
                else None
            ),
        }
        report["errors"] = list(client.errors)
    finally:
        _cancel_our_subscriptions(client, subscribed)
        try:
            if client.isConnected():
                client.disconnect()
        except Exception:
            logger.debug("disconnect failed", exc_info=True)
        report["cleanup"] = {
            "cancelled_mkt_data_req_ids": subscribed,
            "disconnected": True,
        }
        report["finished_at"] = utcnow().isoformat()
    return report


def report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
