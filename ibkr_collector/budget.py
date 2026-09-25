"""Cross-process TWS request/subscription budget. Conservative shares; never probe by exhausting TWS."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ibkr_collector.config import default_data_dir
from ibkr_collector.lock import InstanceLock

BUDGET_VERSION = "tws_shared_budget_v1"
CAPACITY = 90  # leave headroom for TWS UI and other API use
SHARES = {
    "quotes": 40,
    "eod": 25,
    "diagnostic": 15,
    "reserved": 10,
}


@dataclass
class BudgetDecision:
    allowed: bool
    process: str
    tokens: int
    remaining: int
    reason: str


def budget_path() -> Path:
    override = os.environ.get("IBKR_BUDGET_PATH")
    if override:
        return Path(override)
    return default_data_dir() / "shared_budget.json"


def _load(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    payload.setdefault("version", BUDGET_VERSION)
    payload.setdefault("capacity", CAPACITY)
    payload.setdefault("updated_at", 0.0)
    payload.setdefault("held", {})
    return payload


class _BudgetLock:
    def __init__(self, path: Path) -> None:
        self._lock = InstanceLock(path)

    def __enter__(self) -> "_BudgetLock":
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self._lock.acquire():
                return self
            time.sleep(0.05)
        raise TimeoutError("shared TWS budget lock timeout")

    def __exit__(self, exc_type, exc, tb) -> None:
        self._lock.release()


def acquire(process: str, tokens: int = 1, *, refill_per_sec: float = 1.0) -> BudgetDecision:
    share = SHARES.get(process, 5)
    path = budget_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _BudgetLock(path.with_suffix(".lock")):
        now = time.monotonic()
        payload = _load(path.read_text(encoding="utf-8") if path.exists() else "{}")
        held = dict(payload.get("held") or {})
        last = float(payload.get("updated_at") or now)
        elapsed = max(0.0, now - last)
        refill = elapsed * refill_per_sec
        used = max(0, int(sum(int(v) for v in held.values())) - int(refill))
        current = int(held.get(process, 0))
        if current + tokens > share:
            return BudgetDecision(False, process, tokens, max(0, share - current), "process_share")
        if used + tokens > CAPACITY:
            return BudgetDecision(False, process, tokens, max(0, CAPACITY - used), "global_capacity")
        held[process] = current + tokens
        payload["held"] = held
        payload["updated_at"] = now
        path.write_text(json.dumps(payload), encoding="utf-8")
        remaining = min(share - held[process], CAPACITY - int(sum(int(v) for v in held.values())))
        return BudgetDecision(True, process, tokens, remaining, "acquired")


def release(process: str, tokens: int = 1) -> None:
    path = budget_path()
    if not path.exists():
        return
    with _BudgetLock(path.with_suffix(".lock")):
        payload = _load(path.read_text(encoding="utf-8"))
        held = dict(payload.get("held") or {})
        held[process] = max(0, int(held.get(process, 0)) - tokens)
        payload["held"] = held
        payload["updated_at"] = time.monotonic()
        path.write_text(json.dumps(payload), encoding="utf-8")
