"""Server-side Yahoo quotes and incremental daily history. Streamlit does not run this module.

    python -m jobs.yahoo_dashboard_quotes

One process at a time (file lock plus the cron flock). The cron is every 15 minutes.
Closed sessions still run on that schedule and skip a history download when the
stored sessions are already complete.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from market_intelligence.writer_db import WriterConfigurationError, writer_engine
from market_intelligence.yahoo_dashboard import POLL_SECONDS, backoff_seconds, collect_once, should_poll
from market_intelligence.yahoo_price_history import ingest_price_history

logger = logging.getLogger("jobs.yahoo_dashboard_quotes")
LOCK_NAME = "yahoo_dashboard_quotes.lock"
STATE_NAME = "yahoo_dashboard_quotes.json"


def _state_dir() -> Path:
    root = Path(os.environ.get("FMP_YAHOO_QUOTE_STATE_DIR") or "outputs")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_lock(lock_path: Path) -> int | None:
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(lock_path.read_text(encoding="ascii").strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                return None
            try:
                lock_path.unlink()
            except OSError:
                return None
            continue
        os.write(fd, str(os.getpid()).encode("ascii"))
        return fd
    return None


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    state_path = _state_dir() / STATE_NAME
    lock_path = _state_dir() / LOCK_NAME
    fd = _acquire_lock(lock_path)
    if fd is None:
        logger.info("yahoo quote collector already running")
        return 0
    try:
        state = _load_state(state_path)
        now = datetime.now(timezone.utc)
        last_success = _parse_ts(state.get("last_success_at"))
        if not should_poll(now, last_success):
            logger.info("yahoo quotes idle; last success %s", state.get("last_success_at"))
            return 0
        failures = int(state.get("consecutive_failures") or 0)
        if failures:
            wait = backoff_seconds(failures - 1)
            last_attempt = _parse_ts(state.get("last_attempt_at"))
            if last_attempt is not None and (now - last_attempt).total_seconds() < wait:
                logger.info("yahoo quotes backing off %.0fs", wait)
                return 0
        try:
            engine = writer_engine()
        except WriterConfigurationError as exc:
            logger.error("yahoo quotes have no writer database: %s", exc.__class__.__name__)
            return 3
        state["last_attempt_at"] = now.isoformat()
        try:
            with engine.begin() as conn:
                result = collect_once(conn, now=now, cached_opens=state.get("opens") or {})
        except Exception:
            logger.exception("yahoo quote collection failed")
            state["consecutive_failures"] = failures + 1
            state_path.write_text(json.dumps(state), encoding="utf-8")
            return 2
        if result.get("status") == "ERROR":
            state["consecutive_failures"] = failures + 1
            state["last_error"] = result.get("error")
            state_path.write_text(json.dumps(state), encoding="utf-8")
            logger.warning("yahoo download failed: %s", result.get("error"))
            return 2
        state["consecutive_failures"] = 0
        state["last_error"] = None
        state["last_success_at"] = now.isoformat()
        state["priced"] = result.get("priced")
        state["symbols"] = result.get("symbols")
        state["missing"] = result.get("missing") or []
        state["opens"] = result.get("opens") or state.get("opens") or {}
        state["poll_seconds"] = POLL_SECONDS
        state["history_last_attempt_at"] = now.isoformat()
        try:
            with engine.begin() as conn:
                history = ingest_price_history(conn, now=now, progress=state.get("instruments") or {})
        except Exception:
            logger.exception("yahoo price history failed")
            state["history_error"] = "history_failed"
            state_path.write_text(json.dumps(state), encoding="utf-8")
        else:
            state["history_error"] = None
            state["history_last_success_at"] = now.isoformat()
            state["history_written"] = history.get("written")
            state["instruments"] = history.get("instruments") or {}
            logger.info(
                "yahoo history written=%s skipped=%s errors=%s",
                history.get("written"),
                len(history.get("skipped") or []),
                len(history.get("errors") or {}),
            )
        state_path.write_text(json.dumps(state), encoding="utf-8")
        logger.info(
            "yahoo quotes priced=%s/%s inserted=%s missing=%s",
            result.get("priced"),
            result.get("symbols"),
            result.get("inserted"),
            ",".join(result.get("missing") or []) or "-",
        )
        return 0
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(run())
