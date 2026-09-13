"""Record Stage 1 production verify outcome. Never prints secrets.

Does not call QuantConnect. Does not ingest. Does not change systemd.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobs.audit_host_dashboard import write_facts
from qc_research.contracts.label_integrity import load_csfml_v1_label_integrity


DEFAULT_OUT = "/var/lib/fmp/deploy/stage1_verify.json"


def build_record(
    *,
    git_sha: str,
    code_root: str,
    verify_rc: int,
) -> dict[str, Any]:
    pin = load_csfml_v1_label_integrity()
    return {
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "git_sha": str(git_sha or "").strip(),
        "code_root": str(code_root or "").strip(),
        "stage1_verify_rc": int(verify_rc),
        "ok": int(verify_rc) == 0,
        "csfml_v1_label_integrity": pin.get("historical_v1_impact"),
        "csfml_v1_rerun_authorized": bool(pin.get("rerun_authorized")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record Stage 1 verify outcome without secrets")
    parser.add_argument("--git-sha", default="")
    parser.add_argument("--code-root", default="")
    parser.add_argument("--rc", type=int, required=True)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    record = build_record(
        git_sha=args.git_sha,
        code_root=args.code_root,
        verify_rc=args.rc,
    )
    write_facts(record, Path(args.out))
    print("stage1_verify_written={0}".format(args.out))
    print("stage1_verify_ok={0}".format(record["ok"]))
    print("stage1_verify_rc={0}".format(record["stage1_verify_rc"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
