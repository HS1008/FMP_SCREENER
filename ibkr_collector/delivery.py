"""HTTP delivery to the private IBKR ingest API over Tailscale."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("ibkr_collector.delivery")


class DeliveryError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class IngestClient:
    def __init__(self, base_url: str, token: str | None, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise DeliveryError("ingest token missing", retryable=True)
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={
                "Authorization": "Bearer {0}".format(self.token),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code >= 500 or exc.code in {401, 503}
            raise DeliveryError("HTTP {0}".format(exc.code), retryable=retryable) from None
        except urllib.error.URLError as exc:
            raise DeliveryError("unreachable:{0}".format(exc.reason.__class__.__name__), retryable=True) from None
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def send_heartbeat(self, payload: dict[str, Any]) -> None:
        self._post("/v1/heartbeat", payload)

    def send_quotes(self, collector_id: str, quotes: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post("/v1/quotes", {"collector_id": collector_id, "quotes": quotes})

    def send_equity_bars(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/equity_bars", payload)

    def finalize_equity_bars(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/equity_bars/finalize", payload)
