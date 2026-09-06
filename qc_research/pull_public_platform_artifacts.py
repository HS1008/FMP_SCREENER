"""Pull complete public QS artifacts when cross-repo dispatch is unavailable.

Uses GitHub public contents/raw URLs. Never prints tokens. Does not call QuantConnect.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from qc_research.fetch_remote_artifact import fetch_remote_artifact, github_contents_url


def _is_complete_canonical(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("schema_version") or "") in {"platform_artifact_v1", "platform_v1", "stage2_ml_v1"}:
        return False
    if not payload.get("strategy_id"):
        return False
    windows = payload.get("official_windows") or (payload.get("aggregate") or {}).get("windows")
    return isinstance(windows, list) and bool(windows)

DEFAULT_REPO = "hs1008/quant-strategies"
DEFAULT_PATH = "research/platform_smokes"


def _request_json(url: str) -> list[dict] | dict:
    import urllib.request

    headers = {"User-Agent": "fmp-platform-ingest", "Accept": "application/vnd.github+json"}
    token = (
        os.environ.get("QS_READ_TOKEN")
        or os.environ.get("CROSS_REPO_DISPATCH_TOKEN")
        or os.environ.get("GH_PAT")
        or ""
    )
    if token:
        headers["Authorization"] = "Bearer {0}".format(token)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def list_remote_json_paths(repo: str, path: str, ref: str = "") -> list[str]:
    payload = _request_json(github_contents_url(repo, ref, path))
    if not isinstance(payload, list):
        raise RuntimeError("GitHub contents listing is not an array")
    found = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        item_path = str(item.get("path") or name)
        if item.get("type") == "file" and name.endswith(".json"):
            found.append(item_path)
        elif item.get("type") == "dir":
            found.extend(list_remote_json_paths(repo, item_path, ref=ref))
    return found


def pull_complete_artifacts(
    dest: Path,
    *,
    repo: str = DEFAULT_REPO,
    path: str = DEFAULT_PATH,
    ref: str = "",
) -> dict[str, object]:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        remote_paths = list_remote_json_paths(repo, path, ref=ref)
    except Exception as exc:
        return {
            "pulled": 0,
            "skipped": 0,
            "blocked": True,
            "delivery_status": "BLOCKED",
            "reason": "Public artifact listing unavailable: {0}".format(exc),
            "paths": [],
        }
    pulled = []
    skipped = 0
    for remote in remote_paths:
        local = dest / Path(remote).name
        try:
            fetch_remote_artifact(repo=repo, path=remote, dest=local, ref=ref)
            payload = json.loads(local.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            if local.exists():
                local.unlink()
            continue
        if not _is_complete_canonical(payload):
            skipped += 1
            local.unlink(missing_ok=True)
            continue
        pulled.append(str(local))
    return {
        "pulled": len(pulled),
        "skipped": skipped,
        "blocked": False,
        "delivery_status": "PENDING" if pulled else "BLOCKED",
        "reason": "Pulled complete public artifacts" if pulled else "No complete public artifacts found",
        "paths": pulled,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull complete public platform artifacts")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--ref", default="")
    parser.add_argument("--dest", required=True)
    ns = parser.parse_args(argv)
    report = pull_complete_artifacts(Path(ns.dest), repo=ns.repo, path=ns.path, ref=ns.ref)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
