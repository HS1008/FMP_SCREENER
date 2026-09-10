"""Read-only observation of the running Streamlit service.

Distinguishes configured identity (env file / verify script) from the
process that is actually running. Missing host evidence is ``unproven``.
Never prints or stores environment values, URLs, passwords, or tokens.

Does not mutate systemd, restart services, or probe production writes.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_UNIT = "fmp-dashboard"
DEFAULT_CURRENT_LINK = "/opt/fmp/current"
UNPROVEN = "unproven"
RELEASE_SHA = re.compile(r"^[0-9a-f]{40}$")
WRITER_KEY_NAMES = (
    "DATABASE_URL",
    "MARKET_INTELLIGENCE_DATABASE_URL",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
)


def unproven_observation(*, unit: str = DEFAULT_UNIT) -> dict[str, Any]:
    return {
        "observed_running_identity": UNPROVEN,
        "observed_pid": None,
        "observed_unit": unit,
        "observed_fragment_path": UNPROVEN,
        "observed_working_directory": UNPROVEN,
        "observed_executable": UNPROVEN,
        "observed_cmdline_has_streamlit": None,
        "observed_current_symlink": UNPROVEN,
        "observed_code_sha": UNPROVEN,
        "observed_env_file_paths": [],
        "observed_readonly_url_key_present": None,
        "observed_streamlit_readonly_key_present": None,
        "observed_writer_env_key_names_present": [],
        "observed_uses_opt_fmp_current": None,
        "configured_identity_source": "env_file",
        "note": (
            "A configured read-only URL can be reachable from a separate "
            "process without proving the running Streamlit service uses that "
            "code and database identity."
        ),
    }


def _systemctl_show(unit: str, properties: Sequence[str]) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=" + ",".join(properties)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    parsed: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def _read_proc(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _readlink(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return ""


def _environ_keys(raw: bytes) -> list[str]:
    keys: list[str] = []
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        keys.append(item.split(b"=", 1)[0].decode("ascii", "replace"))
    return keys


def _code_sha(current: Path) -> str:
    name = current.name if current.exists() or current.is_symlink() else ""
    if RELEASE_SHA.fullmatch(name):
        return name
    try:
        resolved = current.resolve()
    except OSError:
        return UNPROVEN
    if RELEASE_SHA.fullmatch(resolved.name):
        return resolved.name
    marker = resolved / "RELEASE_SHA"
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if RELEASE_SHA.fullmatch(text):
            return text
    return UNPROVEN


def observe_running_dashboard(
    *,
    unit: str = DEFAULT_UNIT,
    proc_root: Path | None = None,
    current_link: Path | None = None,
    systemctl_properties: Mapping[str, str] | None = None,
    environ_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Inspect the live unit if the host already exposes it.

    Inject ``systemctl_properties`` / ``environ_keys`` in tests. Production
    use never prints values from the process environment.
    """
    facts = unproven_observation(unit=unit)
    current = Path(current_link or os.environ.get("FMP_CURRENT_LINK") or DEFAULT_CURRENT_LINK)
    facts["observed_current_symlink"] = str(current) if current.is_symlink() or current.exists() else UNPROVEN
    if current.is_symlink() or current.exists():
        facts["observed_code_sha"] = _code_sha(current)
    props = dict(systemctl_properties or {})
    if systemctl_properties is None:
        props = _systemctl_show(
            unit,
            (
                "MainPID",
                "FragmentPath",
                "WorkingDirectory",
                "ExecStart",
                "EnvironmentFiles",
                "ActiveState",
            ),
        )
    if not props:
        return facts
    pid_text = str(props.get("MainPID") or "").strip()
    pid = int(pid_text) if pid_text.isdigit() and int(pid_text) > 0 else None
    facts["observed_pid"] = pid
    facts["observed_fragment_path"] = props.get("FragmentPath") or UNPROVEN
    facts["observed_working_directory"] = props.get("WorkingDirectory") or UNPROVEN
    exec_start = props.get("ExecStart") or ""
    facts["observed_uses_opt_fmp_current"] = "/opt/fmp/current" in exec_start if exec_start else None
    env_files = []
    for chunk in str(props.get("EnvironmentFiles") or "").replace("(", " ").replace(")", " ").split():
        if chunk.startswith("/"):
            env_files.append(chunk)
    facts["observed_env_file_paths"] = env_files
    root = Path(proc_root or "/proc")
    if pid:
        cwd = _readlink(root / str(pid) / "cwd")
        exe = _readlink(root / str(pid) / "exe")
        cmdline = _read_proc(root / str(pid) / "cmdline").replace("\x00", " ")
        facts["observed_working_directory"] = cwd or facts["observed_working_directory"]
        facts["observed_executable"] = exe or UNPROVEN
        facts["observed_cmdline_has_streamlit"] = "streamlit" in cmdline.lower() if cmdline else None
        keys = list(environ_keys or [])
        if environ_keys is None:
            try:
                keys = _environ_keys((root / str(pid) / "environ").read_bytes())
            except OSError:
                keys = []
        if keys:
            facts["observed_readonly_url_key_present"] = "DASHBOARD_READONLY_URL" in keys
            facts["observed_streamlit_readonly_key_present"] = "FMP_STREAMLIT_READONLY" in keys
            facts["observed_writer_env_key_names_present"] = [
                name for name in WRITER_KEY_NAMES if name in keys
            ]
        facts["observed_running_identity"] = "recorded"
    elif exec_start:
        facts["observed_running_identity"] = "recorded"
        facts["observed_executable"] = exec_start
    return facts


def print_observation(facts: Mapping[str, Any]) -> None:
    for key in (
        "configured_identity_source",
        "observed_running_identity",
        "observed_pid",
        "observed_unit",
        "observed_fragment_path",
        "observed_working_directory",
        "observed_executable",
        "observed_cmdline_has_streamlit",
        "observed_current_symlink",
        "observed_code_sha",
        "observed_env_file_paths",
        "observed_readonly_url_key_present",
        "observed_streamlit_readonly_key_present",
        "observed_writer_env_key_names_present",
        "observed_uses_opt_fmp_current",
    ):
        print("{0}={1}".format(key, facts.get(key)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observe running Streamlit identity without secrets")
    parser.add_argument("--out", default="")
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    args = parser.parse_args(argv)
    facts = observe_running_dashboard(unit=args.unit)
    if args.out:
        from jobs.audit_host_dashboard import write_facts

        write_facts(facts, Path(args.out))
        print("running_observation_written={0}".format(args.out))
    print_observation(facts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
