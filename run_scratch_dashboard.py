"""
Launch Scratch Dashboard with startup diagnostics and free-port selection.

Usage:
  python run_scratch_dashboard.py

Equivalent to ``streamlit run scratch_dashboard.py`` with network binding and banners.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import config

DASHBOARD_NAME = "Scratch Dashboard"
DEFAULT_PORT = 8501
MAX_PORT_TRIES = 20
SCRIPT = Path(__file__).resolve().parent / "scratch_dashboard.py"


def _active_environment() -> str:
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        return Path(venv).name
    prefix = Path(sys.prefix)
    if "conda" in str(prefix).lower():
        return f"conda ({prefix.name})"
    return f"python {sys.version.split()[0]} ({prefix})"


def _fmp_key_detected() -> bool:
    from dotenv import load_dotenv

    load_dotenv(config.PROJECT_ROOT / ".env")
    return bool((os.getenv("FMP_API_KEY") or "").strip())


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def find_free_port(start: int = DEFAULT_PORT) -> int:
    for port in range(start, start + MAX_PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port in range {start}–{start + MAX_PORT_TRIES - 1}")


def print_startup_banner(port: int) -> None:
    key_ok = _fmp_key_detected()
    local_url = f"http://localhost:{port}"
    network_url = f"http://{_local_ip()}:{port}"
    sep = "=" * 60
    print(sep, flush=True)
    print(f"  {DASHBOARD_NAME}", flush=True)
    print(sep, flush=True)
    print(f"  Environment : {_active_environment()}", flush=True)
    print(f"  Working dir : {Path.cwd().resolve()}", flush=True)
    print(f"  FMP_API_KEY : {'detected' if key_ok else 'NOT FOUND (set in .env)'}", flush=True)
    print(f"  Cache dir   : {config.CACHE_DIR.resolve()}", flush=True)
    print(f"  Local URL   : {local_url}", flush=True)
    print(f"  Network URL : {network_url}", flush=True)
    print(sep, flush=True)
    print("  Press Ctrl+C to stop.\n", flush=True)


def main() -> int:
    if not SCRIPT.is_file():
        print(f"Error: missing {SCRIPT}", file=sys.stderr)
        return 1

    port = find_free_port(DEFAULT_PORT)
    if port != DEFAULT_PORT:
        print(f"Port {DEFAULT_PORT} is in use; using {port} instead.", flush=True)

    print_startup_banner(port)

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(SCRIPT),
        "--server.port",
        str(port),
        "--server.address",
        "0.0.0.0",
    ]
    try:
        return subprocess.call(cmd, cwd=str(config.PROJECT_ROOT))
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
