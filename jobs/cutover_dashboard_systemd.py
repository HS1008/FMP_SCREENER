"""Fail-closed systemd cutover readiness. Default is dry-run.

Does not switch the live unit. Does not invoke systemd.
Does not print URLs or passwords. Does not call QuantConnect.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jobs.audit_host_dashboard import collect_facts, identity_env_from_file, write_facts
from scripts.update_protected_env import STREAMLIT_WRITER_KEYS


DEFAULT_ENV_FILE = "/etc/fmp/fmp-dashboard.env"
DEFAULT_CURRENT_LINK = "/opt/fmp/current"
DEFAULT_OUT = "/var/lib/fmp/deploy/cutover_readiness.json"
RELEASE_SHA = re.compile(r"^[0-9a-f]{40}$")
ASSIGNMENT = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.M)
READONLY_ASSIGNED = re.compile(r"^DASHBOARD_READONLY_URL=\S+", re.M)

PROPOSED_UNIT = """# Generated proposed Streamlit unit. Not installed by this tool.
# Live hosts stay on the git-pull checkout until a human installs this unit.

[Unit]
Description=FMP Streamlit dashboard (read-only PostgreSQL identity)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/fmp/current
EnvironmentFile=-/etc/fmp/fmp-dashboard.env
Environment=FMP_STREAMLIT_READONLY=1
ExecStart=/opt/fmp/current/venv/bin/streamlit run dashboard.py --server.address 127.0.0.1 --server.headless true
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/fmp /var/log/fmp /opt/fmp/current/outputs

[Install]
WantedBy=multi-user.target
"""


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def scan_systemd_env_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "present": False,
            "writer_keys_present": [],
            "readonly_url_assignment": False,
        }
    text = path.read_text(encoding="utf-8")
    names = set(ASSIGNMENT.findall(text))
    writer = [key for key in STREAMLIT_WRITER_KEYS if key in names]
    return {
        "present": True,
        "writer_keys_present": writer,
        "readonly_url_assignment": bool(READONLY_ASSIGNED.search(text)),
    }


def inspect_current_link(current: Path) -> dict[str, Any]:
    present = current.exists() or current.is_symlink()
    resolved = ""
    try:
        if present:
            resolved = str(current.resolve())
    except OSError:
        resolved = ""
    sha = Path(resolved).name if resolved else ""
    root = Path(resolved) if resolved else current
    return {
        "present": present,
        "is_symlink": current.is_symlink(),
        "release_sha_ok": bool(RELEASE_SHA.fullmatch(sha)),
        "verify_script_present": (root / "scripts" / "verify_dashboard_identity.sh").is_file(),
        "dashboard_py_present": (root / "dashboard.py").is_file(),
    }


def evaluate_cutover(
    *,
    env: Mapping[str, str] | None = None,
    password_file: Path | None = None,
    current_link: Path | None = None,
    systemd_exec: str | None = None,
    systemd_env_file: Path | None = None,
    verify_rc: int | None = None,
    apply: bool = False,
    allow_cutover: bool | None = None,
) -> dict[str, Any]:
    env_file = Path(systemd_env_file or (env or os.environ).get("FMP_DASHBOARD_ENV") or DEFAULT_ENV_FILE)
    if env is not None:
        environ = dict(env)
    else:
        environ = identity_env_from_file(env_file)
    current = Path(current_link or environ.get("FMP_CURRENT_LINK") or DEFAULT_CURRENT_LINK)
    facts = collect_facts(
        env=environ,
        password_file=password_file,
        current_link=current,
        systemd_exec=systemd_exec,
        verify_rc=verify_rc,
    )
    env_scan = scan_systemd_env_file(env_file)
    layout = inspect_current_link(current)
    blockers: list[str] = []
    if facts.get("writer_fallback") or facts.get("writer_env_keys_present"):
        blockers.append("writer_identity")
    if env_scan["writer_keys_present"]:
        blockers.append("writer_keys_in_systemd_env")
    if facts.get("provider_fetch"):
        blockers.append("provider_fetch")
    if not facts.get("readonly_proven"):
        blockers.append("readonly_unproven")
    if not layout["present"]:
        blockers.append("immutable_current_missing")
    elif not layout["is_symlink"]:
        blockers.append("current_not_symlink")
    if not layout["release_sha_ok"]:
        blockers.append("current_not_sha_release")
    if not layout["verify_script_present"]:
        blockers.append("verify_script_missing")
    if not layout["dashboard_py_present"]:
        blockers.append("dashboard_py_missing")
    if not env_scan["present"]:
        blockers.append("systemd_env_missing")
    elif not env_scan["readonly_url_assignment"]:
        blockers.append("readonly_url_missing_in_env")

    allowed = _truthy(environ.get("FMP_ALLOW_SYSTEMD_CUTOVER")) if allow_cutover is None else allow_cutover
    apply_status = "not_requested"
    if apply:
        if not allowed:
            apply_status = "refused_allow_flag"
        elif blockers:
            apply_status = "refused_not_ready"
        else:
            apply_status = "refused_no_systemd_mutate"

    return {
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "mode": "apply" if apply else "dry_run",
        "ready": not blockers,
        "blockers": blockers,
        "apply_status": apply_status,
        "systemd_mutated": False,
        "proposed_exec_uses_opt_fmp_current_only": (
            "/opt/fmp/current" in PROPOSED_UNIT and "/root/FMP_SCREENER" not in PROPOSED_UNIT
        ),
        "dashboard_readonly_url_set": facts["dashboard_readonly_url_set"],
        "dashboard_readonly_password_file_present": facts["dashboard_readonly_password_file_present"],
        "writer_fallback": facts["writer_fallback"],
        "writer_env_keys_present": facts["writer_env_keys_present"],
        "writer_keys_in_systemd_env": env_scan["writer_keys_present"],
        "provider_fetch": facts["provider_fetch"],
        "streamlit_readonly": facts["streamlit_readonly"],
        "readonly_proven": facts["readonly_proven"],
        "readonly_verify_rc": facts["readonly_verify_rc"],
        "immutable_current_present": facts["immutable_current_present"],
        "current_is_symlink": layout["is_symlink"],
        "current_release_sha_ok": layout["release_sha_ok"],
        "verify_script_present": layout["verify_script_present"],
        "dashboard_py_present": layout["dashboard_py_present"],
        "systemd_env_present": env_scan["present"],
        "systemd_env_readonly_url_assignment": env_scan["readonly_url_assignment"],
        "systemd_still_git_pull": facts["systemd_exec_contains_root_checkout"]
        and not facts["systemd_cutover_proven"],
        "systemd_cutover_proven": facts["systemd_cutover_proven"],
        "csfml_v1_label_integrity": facts["csfml_v1_label_integrity"],
        "csfml_v1_rerun_authorized": facts["csfml_v1_rerun_authorized"],
    }


def cutover_exit_code(
    report: Mapping[str, Any],
    *,
    require_ready: bool,
    apply: bool,
) -> int:
    if report.get("writer_fallback") or report.get("writer_env_keys_present") or report.get(
        "writer_keys_in_systemd_env"
    ):
        print("cutover=writer_identity_refused")
        return 4
    if report.get("provider_fetch"):
        print("cutover=provider_fetch_refused")
        return 5
    if apply and report.get("apply_status") == "refused_allow_flag":
        print("cutover=apply_refused_allow_flag")
        return 7
    if apply and report.get("apply_status") == "refused_not_ready":
        print("cutover=apply_refused_not_ready")
        return 8
    if require_ready and not report.get("ready"):
        print("cutover=not_ready")
        return 8
    if report.get("ready"):
        print("cutover=ready_not_applied")
        return 0
    print("cutover=recorded")
    return 0


def print_report(report: Mapping[str, Any]) -> None:
    for key in (
        "mode",
        "ready",
        "blockers",
        "apply_status",
        "systemd_mutated",
        "readonly_proven",
        "writer_env_keys_present",
        "writer_keys_in_systemd_env",
        "immutable_current_present",
        "current_release_sha_ok",
        "systemd_still_git_pull",
        "systemd_cutover_proven",
        "csfml_v1_label_integrity",
        "csfml_v1_rerun_authorized",
    ):
        print("{0}={1}".format(key, report.get(key)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run systemd cutover readiness without secrets")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--verify-rc", type=int, default=None)
    parser.add_argument("--env-file", default="")
    parser.add_argument("--write-unit", default="")
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--require-ready", action="store_true", default=False)
    args = parser.parse_args(argv)
    report = evaluate_cutover(
        verify_rc=args.verify_rc,
        systemd_env_file=Path(args.env_file) if args.env_file else None,
        apply=args.apply,
    )
    if args.out:
        write_facts(report, Path(args.out))
        print("cutover_readiness_written={0}".format(args.out))
    if args.write_unit:
        if report.get("writer_keys_in_systemd_env") or report.get("writer_env_keys_present"):
            print("cutover=refused_to_write_unit_with_writer_identity")
            print_report(report)
            return 4
        path = Path(args.write_unit)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PROPOSED_UNIT, encoding="utf-8")
        if "/root/FMP_SCREENER" in path.read_text(encoding="utf-8"):
            raise RuntimeError("proposed unit must not point at the git-pull checkout")
        print("cutover_unit_written={0}".format(path))
    print_report(report)
    return cutover_exit_code(report, require_ready=args.require_ready, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
