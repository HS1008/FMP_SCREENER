"""CLI for the bounded IBKR options prototype. Never POSTs. Never places orders."""

from __future__ import annotations

import json
import sys
from datetime import date
from typing import Any

from ibkr_collector import DEFAULT_OPTIONS_CLIENT_ID
from ibkr_collector.config import load_config
from ibkr_collector.logging_setup import setup_logging
from ibkr_collector.options import (
    ATM_STRIKES_EACH_SIDE,
    MAX_CONCURRENT_LINES,
    MAX_EXPIRATIONS,
    collect_bounded_chain,
    connect_options_session,
    result_summary,
)


def run_fetch_options(args: Any) -> int:
    cfg = load_config()
    setup_logging(cfg.log_dir)
    host = args.host or cfg.tws_host
    port = args.port or cfg.tws_port
    client_id = args.client_id or DEFAULT_OPTIONS_CLIENT_ID
    symbols = [part.strip().upper() for part in str(args.symbols or "SPY").split(",") if part.strip()]
    client, error = connect_options_session(host=host, port=port, client_id=client_id)
    if error or client is None:
        sys.stdout.write(json.dumps({"ok": False, "error": error or "connect_failed", "export_scope": "INTERNAL_ONLY"}, sort_keys=True) + "\n")
        return 2
    reports = []
    try:
        for symbol in symbols:
            kwargs: dict[str, Any] = {
                "max_expirations": args.max_expirations or MAX_EXPIRATIONS,
                "atm_strikes_each_side": args.atm_strikes or ATM_STRIKES_EACH_SIDE,
                "max_concurrent": args.max_lines or MAX_CONCURRENT_LINES,
            }
            if args.quote_wait is not None:
                kwargs["quote_wait_sec"] = args.quote_wait
            if args.generic_ticks is not None:
                ticks = str(args.generic_ticks)
                if ticks.lower() in {"", "none", "off", "-"}:
                    ticks = ""
                kwargs["generic_ticks"] = ticks
            if args.snapshot:
                kwargs["snapshot"] = True
            if args.as_of:
                kwargs["as_of"] = date.fromisoformat(str(args.as_of))
            if args.market_data_type is not None:
                kwargs["market_data_type"] = args.market_data_type
            result = collect_bounded_chain(client, symbol, **kwargs)
            reports.append(result_summary(result, include_quotes=bool(args.prove)))
    finally:
        try:
            if client.isConnected():
                client.disconnect()
        except Exception:
            pass
    payload = {
        "ok": True,
        "posted": False,
        "production_enabled": False,
        "session": {
            "client_id": client_id,
            "host": host,
            "port": port,
            "managed_account_count": len(getattr(client, "managed_accounts", []) or []),
            "managed_account_suffixes": [str(item)[-4:] for item in (getattr(client, "managed_accounts", []) or [])],
        },
        "results": reports,
    }
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if all(row.get("quality", {}).get("status") == "OK" for row in reports) else 1
