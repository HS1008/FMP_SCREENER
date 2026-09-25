"""Hard-capped Cboe live probe: at most 3 data GETs. No UI/schema changes.

Uses writer-host credentials (same auth path that already works for AAPL).
Records endpoint, parameters, HTTP status, response shape, and client point delta.

Verdict printed as verdict=SUPPORTED|WRONG_SYMBOL/ENDPOINT|NOT_ENTITLED/NO_DATA

Never prints credentials or tokens.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any

from market_intelligence.cboe_analytics import expiry_window
from market_intelligence.cboe_client import (
    API_ROOT_DELAYED,
    OPTION_QUOTES,
    UNDERLYING_QUOTES,
    USER_AGENT,
    CboeClient,
    credentials_from_env,
    prior_weekdays,
    session_date,
)

MAX_DATA_GETS = 3


def _shape(payload: Any, raw: bytes) -> str:
    if raw is None or len(raw) == 0:
        return "empty"
    if isinstance(payload, list):
        if not payload:
            return "list:0"
        first = payload[0]
        if isinstance(first, dict):
            keys = ",".join(sorted(str(k) for k in list(first.keys())[:12]))
            return "list:{0}:keys={1}".format(len(payload), keys)
        return "list:{0}:item={1}".format(len(payload), type(first).__name__)
    if isinstance(payload, dict):
        keys = ",".join(sorted(str(k) for k in list(payload.keys())[:12]))
        return "object:keys={0}".format(keys)
    return type(payload).__name__


def _get(client: CboeClient, path: str, params: dict[str, str], *, historical: bool, budget: dict[str, int]) -> dict[str, Any]:
    if budget["n"] >= MAX_DATA_GETS:
        return {"skipped": True, "reason": "max_data_gets_reached"}
    budget["n"] += 1
    before = client.points_used
    token = client.access_token()
    url = client.api_root + path + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    http_status = None
    content_type = ""
    raw = b""
    payload: Any = None
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            http_status = int(getattr(response, "status", None) or response.getcode() or 0)
            headers = getattr(response, "headers", None)
            if headers is not None:
                content_type = str(headers.get("Content-Type") or headers.get("content-type") or "")
            raw = response.read() or b""
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        try:
            raw = exc.read() or b""
        except Exception:
            raw = b""
        content_type = str(getattr(exc, "headers", {}) and exc.headers.get("Content-Type") or "")
    except Exception as exc:  # noqa: BLE001
        return {
            "endpoint": path,
            "params": params,
            "http_status": None,
            "error": type(exc).__name__,
            "response_shape": "error",
            "client_points_delta": 0,
            "client_points_after": client.points_used,
            "bytes": 0,
            "historical": historical,
        }
    if raw.strip():
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            payload = None
    # Mirror production client: only count documented points on successful JSON body.
    consumed = False
    if http_status == 200 and payload is not None:
        cost = client._point_cost(path, historical=historical)
        client.points_used += cost
        client.requests_made += 1
        consumed = True
    return {
        "endpoint": path,
        "params": params,
        "http_status": http_status,
        "content_type": (content_type or "")[:80],
        "response_shape": _shape(payload, raw),
        "client_points_delta": client.points_used - before,
        "client_points_after": client.points_used,
        "points_consumed_by_client_budget": consumed,
        "bytes": len(raw),
        "historical": historical,
        "api_root": client.api_root,
    }


def _verdict(results: list[dict[str, Any]]) -> str:
    usable = [r for r in results if not r.get("skipped")]
    if any(r.get("http_status") == 200 and r.get("bytes", 0) > 0 and "list:" in str(r.get("response_shape")) and not str(r.get("response_shape")).startswith("list:0") for r in usable):
        return "SUPPORTED"
    statuses = {r.get("http_status") for r in usable}
    if statuses & {400, 404, 422}:
        return "WRONG_SYMBOL/ENDPOINT"
    if statuses <= {204, 403, None} or 204 in statuses or 403 in statuses:
        return "NOT_ENTITLED/NO_DATA"
    if statuses & {401}:
        return "WRONG_SYMBOL/ENDPOINT"
    return "NOT_ENTITLED/NO_DATA"


def main() -> int:
    client_id, client_secret = credentials_from_env()
    if not client_id or not client_secret:
        print("auth=CONFIGURATION_REQUIRED")
        return 3
    as_of = session_date()
    recent = prior_weekdays(as_of, 1)[0] if prior_weekdays(as_of, 1) else as_of - timedelta(days=1)
    os.environ.setdefault("CBOE_API_MODE", "delayed")
    client = CboeClient(client_id, client_secret, point_budget=40, api_root=API_ROOT_DELAYED)
    try:
        token = client.access_token()
        print("auth=SUCCEEDED" if token else "auth=FAILED")
    except Exception as exc:  # noqa: BLE001
        print("auth=FAILED:{0}".format(type(exc).__name__))
        return 2
    print("session_date={0}".format(as_of.isoformat()))
    print("quote_date={0}".format(recent.isoformat()))
    print("max_data_gets={0}".format(MAX_DATA_GETS))
    budget = {"n": 0}
    results: list[dict[str, Any]] = []

    # Shot 1: documented SPX option-and-underlying-quotes (historical EOD).
    lo, hi = expiry_window(recent)
    r1 = _get(
        client,
        OPTION_QUOTES,
        {
            "symbol": "SPX",
            "root": "SPX",
            "date": recent.isoformat(),
            "min_expiry": lo.isoformat(),
            "max_expiry": hi.isoformat(),
            "seq_no": "0",
        },
        historical=True,
        budget=budget,
    )
    results.append(r1)
    print("shot=1:" + json.dumps(r1, sort_keys=True, default=str))

    # Shot 2 only if needed: alternate documented index form for underlying-quotes.
    # LiveVol index underlyings are usually bare tickers; OCC-style cash-index form SPX.XO
    # is the common alternate when bare SPX returns empty.
    need_more = r1.get("http_status") != 200 or r1.get("bytes", 0) == 0 or str(r1.get("response_shape", "")).startswith("list:0") or r1.get("response_shape") == "empty"
    if need_more and budget["n"] < MAX_DATA_GETS:
        r2 = _get(
            client,
            UNDERLYING_QUOTES,
            {"symbols": "SPX.XO", "date": recent.isoformat(), "seq_no": "0"},
            historical=True,
            budget=budget,
        )
        results.append(r2)
        print("shot=2:" + json.dumps(r2, sort_keys=True, default=str))
        need_more = r2.get("http_status") != 200 or r2.get("bytes", 0) == 0 or str(r2.get("response_shape", "")).startswith("list:0") or r2.get("response_shape") == "empty"

    # Shot 3 only if needed: bare SPX underlying on the same historical date that works for AAPL.
    if need_more and budget["n"] < MAX_DATA_GETS:
        r3 = _get(
            client,
            UNDERLYING_QUOTES,
            {"symbols": "SPX", "date": recent.isoformat(), "seq_no": "0"},
            historical=True,
            budget=budget,
        )
        results.append(r3)
        print("shot=3:" + json.dumps(r3, sort_keys=True, default=str))

    print("data_gets_used={0}".format(budget["n"]))
    print("client_points_used={0}".format(client.points_used))
    verdict = _verdict(results)
    print("verdict={0}".format(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
