"""Persist sanitized deploy identity to PostgreSQL. Never prints URLs or passwords.

Uses the writer identity in a deploy subshell. Streamlit reads the latest row
only through mi_v_ops_status. Does not call QuantConnect or change systemd.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text

from db.connection import STREAMLIT_READONLY_ENV, streamlit_readonly_active


DEFAULT_STATE = "/var/lib/fmp/deploy/current.json"
GIT_SHA = re.compile(r"^[0-9a-fA-F]{6,64}$")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
SECRET_MARKERS = (
    "postgresql://",
    "postgres://",
    "://",
    "password=",
    "PASSWORD=",
)
INSERT_SQL = """
INSERT INTO mi_deploy_host_identity (
    recorded_at,
    git_sha,
    checkout,
    deploy_mode,
    immutable_release_rc,
    immutable_current_present,
    systemd_still_git_pull,
    systemd_cutover_proven,
    readonly_verify_rc,
    readonly_proven,
    dashboard_readonly_url_set,
    dashboard_readonly_password_file_present,
    writer_fallback,
    writer_env_keys_present,
    provider_fetch,
    streamlit_readonly,
    csfml_v1_label_integrity,
    csfml_v1_rerun_authorized
) VALUES (
    COALESCE(CAST(:recorded_at AS TIMESTAMPTZ), NOW()),
    :git_sha,
    :checkout,
    :deploy_mode,
    :immutable_release_rc,
    :immutable_current_present,
    :systemd_still_git_pull,
    :systemd_cutover_proven,
    :readonly_verify_rc,
    :readonly_proven,
    :dashboard_readonly_url_set,
    :dashboard_readonly_password_file_present,
    :writer_fallback,
    :writer_env_keys_present,
    :provider_fetch,
    :streamlit_readonly,
    :csfml_v1_label_integrity,
    :csfml_v1_rerun_authorized
)
"""


class SecretBearingIdentity(RuntimeError):
    """Refused a deploy-identity record that looks like it contains a secret."""


def state_path() -> Path:
    return Path(os.environ.get("FMP_DEPLOY_STATE") or DEFAULT_STATE)


def _as_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _writer_keys(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        keys = [part.strip() for part in value.split(",") if part.strip()]
    else:
        keys = [str(part).strip() for part in value if str(part).strip()]
    for key in keys:
        if not ENV_KEY.fullmatch(key):
            raise SecretBearingIdentity("writer_env_keys_present must be env key names only")
    return ",".join(keys) if keys else None


def _reject_secrets(value: Any) -> None:
    if value is None:
        return
    text_value = value if isinstance(value, str) else json.dumps(value)
    for marker in SECRET_MARKERS:
        if marker in text_value:
            raise SecretBearingIdentity("deploy identity refused a secret-bearing field")


def sanitize_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    sha = str(raw.get("git_sha") or "").strip()
    if not GIT_SHA.fullmatch(sha):
        raise ValueError("git_sha is required and must be a hex SHA")
    record = {
        "recorded_at": raw.get("recorded_at") or None,
        "git_sha": sha,
        "checkout": str(raw.get("checkout") or "") or None,
        "deploy_mode": str(raw.get("deploy_mode") or "") or None,
        "immutable_release_rc": _as_int(raw.get("immutable_release_rc")),
        "immutable_current_present": _as_bool(raw.get("immutable_current_present")),
        "systemd_still_git_pull": _as_bool(raw.get("systemd_still_git_pull")),
        "systemd_cutover_proven": _as_bool(raw.get("systemd_cutover_proven")),
        "readonly_verify_rc": _as_int(raw.get("readonly_verify_rc")),
        "readonly_proven": _as_bool(raw.get("readonly_proven")),
        "dashboard_readonly_url_set": _as_bool(raw.get("dashboard_readonly_url_set")),
        "dashboard_readonly_password_file_present": _as_bool(
            raw.get("dashboard_readonly_password_file_present")
        ),
        "writer_fallback": _as_bool(raw.get("writer_fallback")),
        "writer_env_keys_present": _writer_keys(raw.get("writer_env_keys_present")),
        "provider_fetch": _as_bool(raw.get("provider_fetch")),
        "streamlit_readonly": _as_bool(raw.get("streamlit_readonly")),
        "csfml_v1_label_integrity": str(raw.get("csfml_v1_label_integrity") or "") or None,
        "csfml_v1_rerun_authorized": _as_bool(raw.get("csfml_v1_rerun_authorized")),
    }
    for value in record.values():
        _reject_secrets(value)
    return record


def load_record(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("deploy identity JSON must be an object")
    return sanitize_record(raw)


def insert_record(record: Mapping[str, Any], *, engine) -> None:
    if streamlit_readonly_active():
        raise RuntimeError("writer deploy-identity insert is refused in the Streamlit process")
    with engine.begin() as conn:
        conn.execute(text(INSERT_SQL), dict(record))


def print_record(record: Mapping[str, Any]) -> None:
    for key in (
        "git_sha",
        "deploy_mode",
        "readonly_proven",
        "streamlit_readonly",
        "systemd_still_git_pull",
        "systemd_cutover_proven",
        "immutable_current_present",
        "csfml_v1_label_integrity",
        "csfml_v1_rerun_authorized",
    ):
        print("{0}={1}".format(key, record.get(key)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist sanitized deploy identity to PostgreSQL")
    parser.add_argument("--from", dest="source", default="")
    args = parser.parse_args(argv)
    if streamlit_readonly_active() or (os.environ.get(STREAMLIT_READONLY_ENV) or "").strip():
        print("FAIL: record_deploy_identity_db is not a Streamlit path")
        return 4
    path = Path(args.source) if args.source else state_path()
    if not path.is_file():
        print("FAIL: deploy identity JSON missing at {0}".format(path))
        return 2
    try:
        record = load_record(path)
    except SecretBearingIdentity as exc:
        print("FAIL: {0}".format(exc))
        return 4
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("FAIL: {0}".format(exc))
        return 2
    from db.connection import get_engine

    insert_record(record, engine=get_engine())
    print("deploy_identity_db_written=mi_deploy_host_identity")
    print_record(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
