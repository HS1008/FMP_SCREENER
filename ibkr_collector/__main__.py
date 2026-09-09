"""CLI: diagnose, run, start, stop, status, install, uninstall."""

from __future__ import annotations

import argparse
import json
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibkr_collector",
        description="Windows-local read-only IBKR/TWS market-data collector (no orders, no IBKR password).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    diag = sub.add_parser("diagnose", help="Bounded read-only TWS handshake/quote/bond diagnostic")
    diag.add_argument("--host", default=None)
    diag.add_argument("--port", type=int, default=None)
    diag.add_argument("--client-id", type=int, default=None)
    run = sub.add_parser("run", help="Run the collector in the foreground")
    run.add_argument("--once-diagnose", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("start", help="Start the installed Windows logon task (or run if not installed)")
    sub.add_parser("stop", help="Stop the collector task and any tracked process")
    sub.add_parser("status", help="Show task, lock, and recent log state")
    sub.add_parser("install", help="Install/update the current-user Windows logon task")
    sub.add_parser("uninstall", help="Remove the Windows task; PostgreSQL data is preserved")
    provision = sub.add_parser("provision-token", help="Store the ingest token from a local file into Credential Manager (never prints the token)")
    provision.add_argument("--from-file", required=True, help="Path to a local 0600 file containing only the token")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "diagnose":
        from ibkr_collector.config import load_config
        from ibkr_collector.diagnostic import report_to_json, run_diagnostic
        from ibkr_collector.logging_setup import setup_logging

        cfg = load_config()
        setup_logging(cfg.log_dir)
        report = run_diagnostic(
            host=args.host or cfg.tws_host,
            port=args.port or cfg.tws_port,
            client_id=args.client_id or cfg.client_id,
        )
        sys.stdout.write(report_to_json(report) + "\n")
        return 0 if report.get("handshake", {}).get("ok") else 2
    if args.command == "run":
        from ibkr_collector.runner import run_forever

        return run_forever()
    if args.command in {"start", "stop", "status", "install", "uninstall", "provision-token"}:
        from ibkr_collector.service_windows import dispatch

        return dispatch(args.command, from_file=getattr(args, "from_file", None))
    raise SystemExit("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
