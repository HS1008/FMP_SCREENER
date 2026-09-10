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


def build_record(*, sha: str, checkout: str, mode: str, immutable_rc: int) -> dict[str, object]:
    return {
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "git_sha": sha,
        "checkout": checkout,
        "deploy_mode": mode,
        "immutable_release_rc": immutable_rc,
        "systemd_still_git_pull": mode == "git_pull",
        "dashboard_readonly_url_set": bool((os.environ.get("DASHBOARD_READONLY_URL") or "").strip()),
        "writer_fallback": (os.environ.get("DASHBOARD_ALLOW_WRITER_FALLBACK") or "").strip().lower()
        in {"1", "true", "yes", "on"},
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
    args = parser.parse_args(argv)
    path = write_record(
        build_record(
            sha=args.sha,
            checkout=args.checkout,
            mode=args.mode,
            immutable_rc=args.immutable_rc,
        )
    )
    print("deploy_identity_written={0}".format(path))
    print("git_sha={0}".format(args.sha))
    print("deploy_mode={0}".format(args.mode))
    print("immutable_release_rc={0}".format(args.immutable_rc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
