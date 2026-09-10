"""Write deploy identity JSON. Never prints URLs or passwords."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_STATE = "/var/lib/fmp/deploy/current.json"


def state_path() -> Path:
    return Path(os.environ.get("FMP_DEPLOY_STATE") or DEFAULT_STATE)


def build_record(
    *,
    sha: str,
    checkout: str,
    mode: str,
    immutable_rc: int,
    verify_rc: int | None = None,
    env_file: Path | str | None = None,
) -> dict[str, object]:
    from jobs.audit_host_dashboard import (
        collect_facts,
        identity_env_from_file,
        resolve_identity_env_file,
    )

    resolved = resolve_identity_env_file(str(env_file) if env_file else "")
    facts = collect_facts(
        env=identity_env_from_file(resolved) if resolved is not None else None,
        verify_rc=verify_rc,
    )
    return {
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "git_sha": sha,
        "checkout": checkout,
        "deploy_mode": mode,
        "immutable_release_rc": immutable_rc,
        "systemd_still_git_pull": mode == "git_pull",
        "dashboard_readonly_url_set": facts["dashboard_readonly_url_set"],
        "dashboard_readonly_password_file_present": facts["dashboard_readonly_password_file_present"],
        "writer_fallback": facts["writer_fallback"],
        "writer_env_keys_present": facts["writer_env_keys_present"],
        "provider_fetch": facts["provider_fetch"],
        "streamlit_readonly": facts["streamlit_readonly"],
        "immutable_current_present": facts["immutable_current_present"],
        "systemd_cutover_proven": facts["systemd_cutover_proven"],
        "readonly_verify_rc": facts["readonly_verify_rc"],
        "readonly_proven": facts["readonly_proven"],
        "csfml_v1_label_integrity": facts["csfml_v1_label_integrity"],
        "csfml_v1_rerun_authorized": facts["csfml_v1_rerun_authorized"],
    }


def write_record(record: dict[str, object], path: Path | None = None) -> Path:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(target, 0o644)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record deploy identity without secrets")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--checkout", default="/root/FMP_SCREENER")
    parser.add_argument("--mode", default="git_pull")
    parser.add_argument("--immutable-rc", type=int, default=0)
    parser.add_argument("--verify-rc", type=int, default=None)
    parser.add_argument("--env-file", default="")
    args = parser.parse_args(argv)
    record = build_record(
        sha=args.sha,
        checkout=args.checkout,
        mode=args.mode,
        immutable_rc=args.immutable_rc,
        verify_rc=args.verify_rc,
        env_file=args.env_file,
    )
    path = write_record(record)
    print("deploy_identity_written={0}".format(path))
    print("git_sha={0}".format(args.sha))
    print("deploy_mode={0}".format(args.mode))
    print("immutable_release_rc={0}".format(args.immutable_rc))
    print("readonly_verify_rc={0}".format(args.verify_rc))
    print("readonly_proven={0}".format(record["readonly_proven"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
