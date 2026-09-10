"""Record live Streamlit identity and systemd path. Never prints URLs or passwords.

Does not change systemd, provision secrets, or call QuantConnect.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_PASSWORD_FILE = "/root/FMP_SCREENER/.secrets/dashboard_readonly.pw"
DEFAULT_CURRENT_LINK = "/opt/fmp/current"
DEFAULT_UNIT = "fmp-dashboard"
DEFAULT_ENV_FILE = "/etc/fmp/fmp-dashboard.env"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _assignment_value(text: str, key: str) -> str:
    prefix = key + "="
    for line in text.splitlines():
        raw = line.strip()
        if raw.startswith("export "):
            raw = raw[7:].strip()
        if raw.startswith(prefix):
            return raw.split("=", 1)[1].strip().strip("'").strip('"')
    return ""


def resolve_identity_env_file(explicit: str | None = None) -> Path | None:
    value = (explicit or os.environ.get("FMP_DASHBOARD_ENV") or "").strip()
    return Path(value) if value else None


def identity_env_from_file(path: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy process env and overlay dashboard identity from the systemd env file.

    Does not print values. Writer leftovers in the process are stripped so
    ``readonly_proven`` cannot be poisoned by a parent writer shell.
    """
    from db.dashboard_engine import WRITER_ENV_KEYS

    environ = dict(base if base is not None else os.environ)
    for key in (
        *WRITER_ENV_KEYS,
        "DASHBOARD_READONLY_URL",
        "DASHBOARD_ALLOW_WRITER_FALLBACK",
        "STREAMLIT_ALLOW_PROVIDER_FETCH",
        "FMP_STREAMLIT_READONLY",
    ):
        environ.pop(key, None)
    if not path.is_file():
        return environ
    text = path.read_text(encoding="utf-8")
    for key in (
        "DASHBOARD_READONLY_URL",
        "DASHBOARD_ALLOW_WRITER_FALLBACK",
        "STREAMLIT_ALLOW_PROVIDER_FETCH",
        "FMP_STREAMLIT_READONLY",
    ):
        value = _assignment_value(text, key)
        if value:
            environ[key] = value
    return environ


def _password_file_present(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def read_systemd_exec(unit: str = DEFAULT_UNIT) -> str:
    override = os.environ.get("FMP_SYSTEMD_EXEC_START")
    if override is not None:
        return override
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "ExecStart", "--value", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").strip()


def collect_facts(
    *,
    env: Mapping[str, str] | None = None,
    password_file: Path | None = None,
    current_link: Path | None = None,
    systemd_exec: str | None = None,
    verify_rc: int | None = None,
    running_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from qc_research.contracts.label_integrity import load_csfml_v1_label_integrity

    environ = env if env is not None else os.environ
    pw = Path(password_file or os.environ.get("FMP_DASHBOARD_READONLY_PW") or DEFAULT_PASSWORD_FILE)
    current = Path(current_link or os.environ.get("FMP_CURRENT_LINK") or DEFAULT_CURRENT_LINK)
    exec_start = systemd_exec if systemd_exec is not None else read_systemd_exec()
    pin = load_csfml_v1_label_integrity()
    from db.dashboard_engine import WRITER_ENV_KEYS

    writer = _truthy(environ.get("DASHBOARD_ALLOW_WRITER_FALLBACK"))
    provider = _truthy(environ.get("STREAMLIT_ALLOW_PROVIDER_FETCH"))
    streamlit_readonly = _truthy(environ.get("FMP_STREAMLIT_READONLY"))
    url_set = bool(str(environ.get("DASHBOARD_READONLY_URL") or "").strip())
    writer_env_keys_present = [
        key for key in WRITER_ENV_KEYS if str(environ.get(key) or "").strip()
    ]
    pw_present = _password_file_present(pw)
    uses_current = "/opt/fmp/current" in exec_start
    uses_root = "/root/FMP_SCREENER" in exec_start
    readonly_proven = (
        verify_rc == 0
        and url_set
        and pw_present
        and not writer
        and not provider
        and not writer_env_keys_present
        and streamlit_readonly
    )
    if running_observation is None:
        from jobs.observe_running_dashboard import observe_running_dashboard

        running_observation = observe_running_dashboard(current_link=current)
    observed = dict(running_observation)
    return {
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "dashboard_readonly_url_set": url_set,
        "dashboard_readonly_password_file_present": pw_present,
        "writer_fallback": writer,
        "writer_env_keys_present": writer_env_keys_present,
        "provider_fetch": provider,
        "streamlit_readonly": streamlit_readonly,
        "systemd_exec_contains_opt_fmp_current": uses_current,
        "systemd_exec_contains_root_checkout": uses_root,
        "immutable_current_present": current.exists() or current.is_symlink(),
        "readonly_verify_rc": verify_rc,
        "readonly_proven": readonly_proven,
        "systemd_cutover_proven": uses_current and not uses_root,
        "csfml_v1_label_integrity": pin["historical_v1_impact"],
        "csfml_v1_rerun_authorized": bool(pin["rerun_authorized"]),
        "configured_identity_source": "env_file",
        "configured_readonly_url_set": url_set,
        "observed_running_identity": observed.get("observed_running_identity") or "unproven",
        "observed_pid": observed.get("observed_pid"),
        "observed_working_directory": observed.get("observed_working_directory") or "unproven",
        "observed_executable": observed.get("observed_executable") or "unproven",
        "observed_code_sha": observed.get("observed_code_sha") or "unproven",
        "observed_env_file_paths": list(observed.get("observed_env_file_paths") or []),
        "observed_uses_opt_fmp_current": observed.get("observed_uses_opt_fmp_current"),
        "running_service_identity_proven": observed.get("observed_running_identity") == "recorded"
        and bool(observed.get("observed_pid")),
    }


def write_facts(facts: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(facts), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if "postgresql://" in text:
        raise RuntimeError("host audit refused to write a secret-bearing record")
    return path


def print_facts(facts: Mapping[str, Any]) -> None:
    for key in (
        "dashboard_readonly_url_set",
        "dashboard_readonly_password_file_present",
        "writer_fallback",
        "writer_env_keys_present",
        "provider_fetch",
        "streamlit_readonly",
        "systemd_exec_contains_opt_fmp_current",
        "systemd_exec_contains_root_checkout",
        "immutable_current_present",
        "readonly_verify_rc",
        "readonly_proven",
        "systemd_cutover_proven",
        "csfml_v1_label_integrity",
        "csfml_v1_rerun_authorized",
        "configured_readonly_url_set",
        "observed_running_identity",
        "running_service_identity_proven",
    ):
        print("{0}={1}".format(key, facts.get(key)))


def audit_exit_code(facts: Mapping[str, Any], *, require_readonly: bool) -> int:
    if facts.get("writer_fallback") or facts.get("writer_env_keys_present"):
        print("host_audit=writer_fallback_refused")
        return 4
    if facts.get("provider_fetch"):
        print("host_audit=provider_fetch_refused")
        return 5
    if require_readonly and not facts.get("readonly_proven"):
        print("host_audit=readonly_unproven")
        return 3
    if facts.get("readonly_proven"):
        print("host_audit=readonly_proven")
        return 0
    print("host_audit=recorded")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit host Streamlit identity without secrets")
    parser.add_argument("--out", default="")
    parser.add_argument("--require-readonly", action="store_true", default=False)
    parser.add_argument("--verify-rc", type=int, default=None)
    parser.add_argument("--env-file", default="")
    args = parser.parse_args(argv)
    env_file = resolve_identity_env_file(args.env_file)
    facts = collect_facts(
        env=identity_env_from_file(env_file) if env_file is not None else None,
        verify_rc=args.verify_rc,
    )
    if args.out:
        write_facts(facts, Path(args.out))
        print("host_audit_written={0}".format(args.out))
    print_facts(facts)
    return audit_exit_code(facts, require_readonly=args.require_readonly)


if __name__ == "__main__":
    raise SystemExit(main())
