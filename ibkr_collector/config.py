"""Collector configuration (no secrets). Secrets live in Windows Credential Manager."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ibkr_collector import DEFAULT_CLIENT_ID, DEFAULT_TWS_HOST, DEFAULT_TWS_PORT

DEFAULT_WATCHLIST: tuple[dict[str, str], ...] = (
    {"symbol": "SPY", "sec_type": "STK", "exchange": "SMART", "currency": "USD", "primary_exchange": "ARCA"},
    {"symbol": "QQQ", "sec_type": "STK", "exchange": "SMART", "currency": "USD", "primary_exchange": "NASDAQ"},
    {"symbol": "IWM", "sec_type": "STK", "exchange": "SMART", "currency": "USD", "primary_exchange": "ARCA"},
    {"symbol": "TLT", "sec_type": "STK", "exchange": "SMART", "currency": "USD", "primary_exchange": "NASDAQ"},
    {"symbol": "HYG", "sec_type": "STK", "exchange": "SMART", "currency": "USD", "primary_exchange": "ARCA"},
)


def default_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "FMP_SCREENER" / "ibkr-collector"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass
class CollectorConfig:
    tws_host: str = DEFAULT_TWS_HOST
    tws_port: int = DEFAULT_TWS_PORT
    client_id: int = DEFAULT_CLIENT_ID
    ingest_url: str = "http://fmp-dashboard:8771"
    collector_id: str = "harin-laptop"
    heartbeat_interval_sec: float = 15.0
    quote_interval_sec: float = 10.0
    socket_poll_sec: float = 5.0
    backoff_initial_sec: float = 2.0
    backoff_max_sec: float = 60.0
    heartbeat_stale_sec: float = 90.0
    queue_max_records: int = 20000
    watchlist: list[dict[str, str]] = field(default_factory=lambda: [dict(row) for row in DEFAULT_WATCHLIST])
    data_dir: Path = field(default_factory=default_data_dir)

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def queue_path(self) -> Path:
        return self.data_dir / "queue.sqlite"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "collector.lock"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"


def load_config(path: Path | None = None) -> CollectorConfig:
    cfg = CollectorConfig()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    target = path or cfg.config_path
    if target.is_file():
        raw = json.loads(target.read_text(encoding="utf-8"))
        _apply(cfg, raw)
    env_url = (os.environ.get("IBKR_INGEST_URL") or "").strip()
    if env_url:
        cfg.ingest_url = env_url.rstrip("/")
    if os.environ.get("IBKR_CLIENT_ID"):
        cfg.client_id = int(os.environ["IBKR_CLIENT_ID"])
    if os.environ.get("IBKR_TWS_PORT"):
        cfg.tws_port = int(os.environ["IBKR_TWS_PORT"])
    if os.environ.get("IBKR_TWS_HOST"):
        cfg.tws_host = os.environ["IBKR_TWS_HOST"].strip()
    return cfg


def _apply(cfg: CollectorConfig, raw: dict[str, Any]) -> None:
    for key in (
        "tws_host",
        "ingest_url",
        "collector_id",
    ):
        if raw.get(key):
            setattr(cfg, key, str(raw[key]))
    for key in ("tws_port", "client_id", "queue_max_records"):
        if raw.get(key) is not None:
            setattr(cfg, key, int(raw[key]))
    for key in (
        "heartbeat_interval_sec",
        "quote_interval_sec",
        "socket_poll_sec",
        "backoff_initial_sec",
        "backoff_max_sec",
        "heartbeat_stale_sec",
    ):
        if raw.get(key) is not None:
            setattr(cfg, key, float(raw[key]))
    if isinstance(raw.get("watchlist"), list) and raw["watchlist"]:
        cfg.watchlist = [dict(row) for row in raw["watchlist"]]


def write_example_config(path: Path | None = None) -> Path:
    cfg = CollectorConfig()
    target = path or cfg.config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        payload = {
            "tws_host": cfg.tws_host,
            "tws_port": cfg.tws_port,
            "client_id": cfg.client_id,
            "ingest_url": cfg.ingest_url,
            "collector_id": cfg.collector_id,
            "heartbeat_interval_sec": cfg.heartbeat_interval_sec,
            "quote_interval_sec": cfg.quote_interval_sec,
            "watchlist": cfg.watchlist,
        }
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target
