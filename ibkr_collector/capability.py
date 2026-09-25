"""Bounded, read-only TWS capability probe. Never places orders or dumps licensed values."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ibkr_collector import COLLECTOR_VERSION, DEFAULT_DIAGNOSTIC_CLIENT_ID, DEFAULT_TWS_HOST, DEFAULT_TWS_PORT
from ibkr_collector.diagnostic import probe_socket
from ibkr_collector.readonly_client import ReadOnlyTwsClient
from ibkr_collector.values import market_data_type_label, utcnow

HANDSHAKE_WAIT_SEC = 15.0
CONTRACT_WAIT_SEC = 8.0
STREAM_WAIT_SEC = 8.0


def _stock(symbol: str, primary: str):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    contract.primaryExchange = primary
    return contract


def _fx(symbol: str, currency: str):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = symbol
    contract.secType = "CASH"
    contract.exchange = "IDEALPRO"
    contract.currency = currency
    return contract


def _future(symbol: str, exchange: str):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = symbol
    contract.secType = "FUT"
    contract.exchange = exchange
    contract.currency = "USD"
    return contract


def _option(symbol: str, expiry: str, strike: float, right: str, exchange: str = "SMART"):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = symbol
    contract.secType = "OPT"
    contract.exchange = exchange
    contract.currency = "USD"
    contract.lastTradeDateOrContractMonth = expiry
    contract.strike = strike
    contract.right = right
    return contract


def _row(*, domain: str, contract: str, field: str, request: str, availability: str, data_mode: str = "UNKNOWN", delivery: str = "STREAMING", evidence: str, rights: str = "INTERNAL_ONLY") -> dict[str, Any]:
    return {
        "domain": domain,
        "contract": contract,
        "field": field,
        "request": request,
        "data_mode": data_mode,
        "delivery": delivery,
        "availability": availability,
        "evidence": evidence,
        "rights_activation": rights,
        "tested_api": "ibapi-10.50.1",
    }


def _qualify(client, contract, timeout: float = CONTRACT_WAIT_SEC) -> dict[str, Any]:
    req_id = client.next_req_id()
    client.wait_event(req_id, client.contract_details_done)
    client.reqContractDetails(req_id, contract)
    ok = client.contract_details_done[req_id].wait(timeout)
    details = list(client.contract_details.get(req_id) or [])
    return {"ok": ok, "details": details, "req_id": req_id}


def _stream_fields(client, contract, *, generic: str = "", wait: float = STREAM_WAIT_SEC) -> dict[str, Any]:
    req_id = client.next_req_id()
    client.reqMktData(req_id, contract, generic, False, False, [])
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        ticks = dict(client.ticks.get(req_id) or {})
        if any(ticks.get(key) is not None for key in ("bid", "ask", "last", "close")):
            break
        time.sleep(0.2)
    ticks = dict(client.ticks.get(req_id) or {})
    md_code = client.market_data_types.get(req_id, ticks.get("market_data_type"))
    try:
        client.cancelMktData(req_id)
    finally:
        client.retire_request(req_id)
    return {
        "req_id": req_id,
        "ticks": {key: ticks.get(key) for key in ("bid", "ask", "last", "close", "bid_size", "ask_size", "volume", "open_interest", "option_call_oi", "option_put_oi") if key in ticks},
        "present": sorted(key for key, value in ticks.items() if value not in (None, False, {})),
        "market_data_type": market_data_type_label(int(md_code)) if md_code is not None else ("DELAYED" if ticks.get("delayed_ticks") else "UNKNOWN"),
        "entitlement": any(row.get("req_id") == req_id and row.get("kind") == "entitlement" for row in client.errors),
        "greeks_labels": sorted((ticks.get("option_computations") or {}).keys()),
    }


def persist_sanitized_report(report: dict[str, Any]) -> str:
    from ibkr_collector.config import default_data_dir

    path = default_data_dir() / "capability_probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = dict(report)
    sanitized.pop("ticks", None)
    path.write_text(json.dumps(sanitized, indent=2, default=str), encoding="utf-8")
    return str(path)


def _index(symbol: str, exchange: str):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = symbol
    contract.secType = "IND"
    contract.exchange = exchange
    contract.currency = "USD"
    return contract


def _append_stream(report: dict[str, Any], *, domain: str, contract: str, quote: dict[str, Any], fields: tuple[str, ...], request: str) -> None:
    mode = quote["market_data_type"]
    for field in fields:
        present = quote["ticks"].get(field) is not None if field in quote["ticks"] else field in quote["present"]
        if field == "bid/ask":
            present = quote["ticks"].get("bid") is not None or quote["ticks"].get("ask") is not None
        availability = "NOT_ENTITLED" if quote["entitlement"] else ("RETURNED" if present else "NOT_RETURNED")
        report["rows"].append(
            _row(
                domain=domain,
                contract=contract,
                field=field,
                request=request,
                availability=availability,
                data_mode=mode,
                evidence="fields={0}".format(",".join(quote["present"][:12]) or "none"),
            )
        )


def _chain_def(client, *, symbol: str, sec_type: str, con_id: int, domain: str) -> dict[str, Any]:
    req_id = client.next_req_id()
    client.wait_event(req_id, client.option_params_done)
    client.reqSecDefOptParams(req_id, symbol, "", sec_type, con_id)
    ok = client.option_params_done[req_id].wait(CONTRACT_WAIT_SEC)
    params = list(client.option_params.get(req_id) or [])
    n_exp = sum(len(row.get("expirations") or []) for row in params)
    n_strike = sum(len(row.get("strikes") or []) for row in params)
    return {
        "params": params,
        "row": _row(
            domain=domain,
            contract=symbol,
            field="option_chain_definition",
            request="reqSecDefOptParams {0}".format(sec_type),
            availability="RETURNED" if params else "NOT_RETURNED",
            delivery="REFERENCE",
            evidence="ok={0} classes={1} expiries={2} strikes={3}".format(ok, len(params), n_exp, n_strike),
        ),
        "req_id": req_id,
    }


def _bounded_option_pair(client, report: dict[str, Any], params: list[dict[str, Any]], *, symbol: str) -> None:
    """Qualify one call and one put. Do not subscribe a full chain. Do not dump licensed values."""
    classes = [row for row in params if row.get("expirations") and row.get("strikes")]
    if not classes:
        report["rows"].append(
            _row(
                domain="index_etf_options",
                contract=symbol,
                field="call_put_pair",
                request="reqContractDetails OPT",
                availability="NOT_RETURNED",
                delivery="REFERENCE",
                evidence="no_chain_class",
            )
        )
        return
    klass = None
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in classes:
        exchange = str(row.get("exchange") or "").upper()
        trading_class = str(row.get("trading_class") or "").upper()
        score = 0.0
        if exchange in {"SMART", "CBOE", "CBOE2", "AMEX", "BATS"}:
            score += 10
        if trading_class == symbol.upper() or trading_class.startswith(symbol.upper()):
            score += 6
        if exchange == "NASDAQOM":
            score -= 8
        score += min(len(row.get("strikes") or []), 80) / 80.0
        ranked.append((score, row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    klass = ranked[0][1]
    expiries = sorted(str(item) for item in klass["expirations"])
    today = utcnow().strftime("%Y%m%d")
    expiry = next((item for item in expiries if item >= today), expiries[0])
    strikes = sorted(float(item) for item in klass["strikes"])
    mid = len(strikes) // 2
    sample_idx = sorted({min(max(0, mid + offset), len(strikes) - 1) for offset in (0, -1, 1, -2, 2, -8, 8, -20)})
    sample_strikes = [strikes[i] for i in sample_idx]
    exchange = "SMART"
    source_exchange = str(klass.get("exchange") or "")
    trading_class = str(klass.get("trading_class") or symbol)
    multiplier = str(klass.get("multiplier") or "").strip()
    qualified_c = None
    attempts = 0
    for strike in sample_strikes:
        attempts += 1
        contract = _option(symbol, expiry, strike, "C", exchange)
        contract.tradingClass = trading_class
        contract.multiplier = multiplier or ""
        details = _qualify(client, contract)
        if details["details"] and details["details"][0].get("con_id"):
            qualified_c = (strike, details)
            break
    report["rows"].append(
        _row(
            domain="index_etf_options",
            contract="{0} {1} C".format(symbol, expiry),
            field="contract_details",
            request="reqContractDetails OPT sampled strikes",
            availability="RETURNED" if qualified_c else "INVALID_CONTRACT",
            delivery="REFERENCE",
            evidence="source_exchange={0} attempts={1}/{2} trading_class_present={3}".format(
                source_exchange, attempts, len(sample_strikes), bool(trading_class)
            ),
        )
    )
    if not qualified_c:
        return
    strike, call_details = qualified_c
    put_contract = _option(symbol, expiry, strike, "P", exchange)
    put_contract.tradingClass = trading_class
    put_contract.multiplier = multiplier or ""
    put_details = _qualify(client, put_contract)
    report["rows"].append(
        _row(
            domain="index_etf_options",
            contract="{0} {1} P".format(symbol, expiry),
            field="contract_details",
            request="reqContractDetails OPT P",
            availability="RETURNED" if put_details["details"] else "NOT_RETURNED",
            delivery="REFERENCE",
            evidence="paired_with_qualified_call=1",
        )
    )
    from ibapi.contract import Contract

    for right, details in (("C", call_details), ("P", put_details)):
        if not (details["details"] and details["details"][0].get("con_id")):
            continue
        live = Contract()
        live.conId = int(details["details"][0]["con_id"])
        live.secType = "OPT"
        live.exchange = details["details"][0].get("exchange") or exchange
        live.currency = "USD"
        quote = _stream_fields(client, live, generic="101,221")
        _append_stream(
            report,
            domain="index_etf_options",
            contract="{0} {1} {2}".format(symbol, expiry, right),
            quote=quote,
            fields=("bid", "ask", "last"),
            request="reqMktData OPT streaming generic=101,221",
        )
        report["rows"].append(
            _row(
                domain="index_etf_options",
                contract="{0} {1} {2}".format(symbol, expiry, right),
                field="greeks_labels",
                request="tickOptionComputation",
                availability="RETURNED" if quote.get("greeks_labels") else "NOT_RETURNED",
                data_mode=quote["market_data_type"],
                delivery="STREAMING",
                evidence="labels={0}".format(",".join(quote.get("greeks_labels") or []) or "none"),
            )
        )


def run_capability_probe(*, host: str = DEFAULT_TWS_HOST, port: int = DEFAULT_TWS_PORT, client_id: int = DEFAULT_DIAGNOSTIC_CLIENT_ID) -> dict[str, Any]:
    from ibkr_collector.budget import acquire, release

    report: dict[str, Any] = {
        "collector_version": COLLECTOR_VERSION,
        "started_at": utcnow().isoformat(),
        "tws_host": host,
        "tws_port": port,
        "client_id": client_id,
        "handshake_ok": False,
        "server_time_utc": None,
        "requested_market_data_type": "DELAYED",
        "rows": [],
        "session_note": None,
        "forbidden_methods_invoked": [],
        "evidence_path": None,
    }
    budget = acquire("diagnostic", 8)
    report["budget"] = {"allowed": budget.allowed, "reason": budget.reason, "remaining": budget.remaining}
    socket_probe = probe_socket(host, port)
    if not socket_probe.get("ok"):
        report["session_note"] = "tcp port closed"
        report["finished_at"] = utcnow().isoformat()
        report["evidence_path"] = persist_sanitized_report(report)
        if budget.allowed:
            release("diagnostic", 8)
        return report

    client = ReadOnlyTwsClient()
    reader = threading.Thread(target=client.run, name="ibkr-capability-reader", daemon=True)
    try:
        client.connect(host, port, client_id)
        reader.start()
        handshake_ok = client.handshake.wait(HANDSHAKE_WAIT_SEC)
        report["handshake_ok"] = bool(handshake_ok)
        if not handshake_ok:
            report["session_note"] = "handshake timeout"
            report["finished_at"] = utcnow().isoformat()
            report["evidence_path"] = persist_sanitized_report(report)
            return report
        client.reqCurrentTime()
        if client.server_time_event.wait(5.0) and client.server_time_unix:
            report["server_time_utc"] = datetime.fromtimestamp(client.server_time_unix, tz=timezone.utc).isoformat()
        client.reqMarketDataType(3)

        spy = _qualify(client, _stock("SPY", "ARCA"))
        spy_contract = _stock("SPY", "ARCA")
        spy_con = None
        if spy["details"] and spy["details"][0].get("con_id"):
            spy_con = int(spy["details"][0]["con_id"])
            spy_contract.conId = spy_con
        spy_quote = _stream_fields(client, spy_contract)
        _append_stream(report, domain="equity_etf_current", contract="SPY", quote=spy_quote, fields=("bid", "ask", "last", "close"), request="reqMktData snapshot=False generic='' requested_type=DELAYED")

        qqq = _qualify(client, _stock("QQQ", "NASDAQ"))
        qqq_contract = _stock("QQQ", "NASDAQ")
        if qqq["details"] and qqq["details"][0].get("con_id"):
            qqq_contract.conId = int(qqq["details"][0]["con_id"])
        _append_stream(report, domain="equity_etf_current", contract="QQQ", quote=_stream_fields(client, qqq_contract), fields=("bid", "ask", "last"), request="reqMktData QQQ")

        fx = _qualify(client, _fx("EUR", "USD"))
        fx_contract = _fx("EUR", "USD")
        if fx["details"] and fx["details"][0].get("con_id"):
            fx_contract.conId = int(fx["details"][0]["con_id"])
        _append_stream(report, domain="fx", contract="EURUSD", quote=_stream_fields(client, fx_contract), fields=("bid/ask",), request="reqMktData CASH IDEALPRO")

        iwm = _qualify(client, _stock("IWM", "ARCA"))
        iwm_contract = _stock("IWM", "ARCA")
        iwm_con = None
        if iwm["details"] and iwm["details"][0].get("con_id"):
            iwm_con = int(iwm["details"][0]["con_id"])
            iwm_contract.conId = iwm_con
        _append_stream(report, domain="equity_etf_current", contract="IWM", quote=_stream_fields(client, iwm_contract), fields=("bid", "ask", "last"), request="reqMktData IWM")

        jpy = _qualify(client, _fx("USD", "JPY"))
        jpy_contract = _fx("USD", "JPY")
        if jpy["details"] and jpy["details"][0].get("con_id"):
            jpy_contract.conId = int(jpy["details"][0]["con_id"])
        _append_stream(report, domain="fx", contract="USDJPY", quote=_stream_fields(client, jpy_contract), fields=("bid/ask",), request="reqMktData CASH USDJPY")

        for root, exchange, domain in (
            ("ES", "CME", "equity_index_futures"),
            ("NQ", "CME", "equity_index_futures"),
            ("RTY", "CME", "equity_index_futures"),
            ("ZN", "CBOT", "treasury_futures"),
            ("CL", "NYMEX", "energy_futures"),
            ("GC", "COMEX", "metals_futures"),
        ):
            details = _qualify(client, _future(root, exchange))
            report["rows"].append(
                _row(
                    domain=domain,
                    contract=root,
                    field="contract_details",
                    request="reqContractDetails FUT {0}".format(exchange),
                    availability="RETURNED" if details["details"] else "NOT_RETURNED",
                    delivery="REFERENCE",
                    evidence="n_contracts={0}".format(len(details["details"])),
                )
            )
            if details["details"]:
                from ibapi.contract import Contract

                front = Contract()
                front.conId = int(details["details"][0]["con_id"])
                front.exchange = details["details"][0].get("exchange") or exchange
                front.secType = "FUT"
                front.currency = "USD"
                generic = "588" if root == "ES" else ""
                quote = _stream_fields(client, front, generic=generic)
                _append_stream(
                    report,
                    domain=domain,
                    contract=str(details["details"][0].get("local_symbol") or root),
                    quote=quote,
                    fields=("bid", "ask", "last") if root != "ES" else ("bid", "ask", "last", "open_interest"),
                    request="reqMktData streaming" + (" generic" if generic else ""),
                )

        vix = _qualify(client, _index("VIX", "CBOE"))
        report["rows"].append(
            _row(
                domain="volatility",
                contract="VIX",
                field="contract_details",
                request="reqContractDetails IND CBOE",
                availability="RETURNED" if vix["details"] else "NOT_RETURNED",
                delivery="REFERENCE",
                evidence="n_contracts={0}".format(len(vix["details"])),
            )
        )

        if spy_con:
            spy_chain = _chain_def(client, symbol="SPY", sec_type="STK", con_id=spy_con, domain="index_etf_options")
            report["rows"].append(spy_chain["row"])
            client.retire_request(spy_chain["req_id"])
            _bounded_option_pair(client, report, spy_chain["params"], symbol="SPY")

        if iwm_con:
            iwm_chain = _chain_def(client, symbol="IWM", sec_type="STK", con_id=iwm_con, domain="index_etf_options")
            report["rows"].append(iwm_chain["row"])
            client.retire_request(iwm_chain["req_id"])

        spx = _qualify(client, _index("SPX", "CBOE"))
        report["rows"].append(
            _row(
                domain="index_etf_options",
                contract="SPX",
                field="contract_details",
                request="reqContractDetails IND CBOE",
                availability="RETURNED" if spx["details"] else "NOT_RETURNED",
                delivery="REFERENCE",
                evidence="n_contracts={0}".format(len(spx["details"])),
            )
        )
        if spx["details"] and spx["details"][0].get("con_id"):
            spx_chain = _chain_def(client, symbol="SPX", sec_type="IND", con_id=int(spx["details"][0]["con_id"]), domain="index_etf_options")
            report["rows"].append(spx_chain["row"])
            client.retire_request(spx_chain["req_id"])

        weekday = utcnow().weekday()
        if weekday >= 5:
            report["session_note"] = "AWAITING_OPEN_SESSION: US cash session closed (weekend). Live SLO not claimed. NOT_RETURNED is not UNSUPPORTED."
        else:
            report["session_note"] = "weekday probe; requested DELAYED; actual mode is the marketDataType callback"
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        report["error_kinds"] = sorted({str(row.get("kind")) for row in client.errors})
        report["error_codes"] = sorted({int(row["error_code"]) for row in client.errors if row.get("error_code") is not None})
        report["finished_at"] = utcnow().isoformat()
        if budget.allowed:
            release("diagnostic", 8)
        report["evidence_path"] = persist_sanitized_report(report)
    return report
