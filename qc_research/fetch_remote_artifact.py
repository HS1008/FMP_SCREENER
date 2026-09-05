"""Fetch a canonical research artifact from GitHub. Never prints tokens."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def github_raw_url(repo: str, ref: str, path: str) -> str:
    return "https://raw.githubusercontent.com/{0}/{1}/{2}".format(
        repo.strip("/"),
        ref or "main",
        path.lstrip("/"),
    )


def github_contents_url(repo: str, ref: str, path: str) -> str:
    url = "https://api.github.com/repos/{0}/contents/{1}".format(repo.strip("/"), path.lstrip("/"))
    if ref:
        url += "?ref={0}".format(ref)
    return url


def _token() -> str:
    return (
        os.environ.get("QS_READ_TOKEN")
        or os.environ.get("CROSS_REPO_DISPATCH_TOKEN")
        or os.environ.get("GH_PAT")
        or ""
    )


def fetch_remote_artifact(
    *,
    repo: str,
    path: str,
    dest: Path,
    ref: str = "",
    token: str | None = None,
) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    secret = token if token is not None else _token()
    raw = github_raw_url(repo, ref, path)
    headers = {"User-Agent": "fmp-platform-ingest"}
    if secret:
        headers["Authorization"] = "Bearer {0}".format(secret)
    request = urllib.request.Request(raw, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            dest.write_bytes(response.read())
            return dest
    except urllib.error.HTTPError:
        if not secret:
            raise
    api = urllib.request.Request(
        github_contents_url(repo, ref, path),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer {0}".format(secret),
            "User-Agent": "fmp-platform-ingest",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(api, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    download = payload.get("download_url")
    if not download:
        raise RuntimeError("GitHub contents response has no download_url")
    request = urllib.request.Request(
        download,
        headers={"Authorization": "Bearer {0}".format(secret), "User-Agent": "fmp-platform-ingest"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        dest.write_bytes(response.read())
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch a remote platform research artifact")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--ref", default="")
    ns = parser.parse_args(argv)
    fetch_remote_artifact(repo=ns.repo, path=ns.path, dest=Path(ns.dest), ref=ns.ref)
    print("fetched {0}".format(ns.dest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
