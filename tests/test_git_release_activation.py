"""Live-checkout activation must not inherit an unmarked shallow commit."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib_git_release.sh"
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fmp-test",
    "GIT_AUTHOR_EMAIL": "fmp-test@example.com",
    "GIT_COMMITTER_NAME": "fmp-test",
    "GIT_COMMITTER_EMAIL": "fmp-test@example.com",
    "GIT_TERMINAL_PROMPT": "0",
}


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


def _file_url(path: Path) -> str:
    posix = _posix(path)
    if len(posix) >= 2 and posix[1] == ":":
        return "file:///" + posix
    if not posix.startswith("/"):
        posix = "/" + posix
    return "file://" + posix


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        capture_output=True,
        text=True,
        check=check,
        env=GIT_ENV,
        cwd=str(cwd) if cwd is not None else None,
    )


def _bash(script: str, check: bool = True) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required for checkout activation tests")
    return subprocess.run(
        [bash, "-c", script],
        capture_output=True,
        text=True,
        check=check,
        env=GIT_ENV,
    )


def _write_commit(repo: Path, name: str) -> str:
    (repo / "marker.txt").write_text(name + "\n", encoding="utf-8")
    _git("add", "marker.txt", cwd=repo)
    _git("commit", "-m", name, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


@pytest.fixture
def ancestry_repos(tmp_path: Path) -> dict[str, str]:
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git and bash are required for checkout activation tests")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", cwd=origin)
    _git("config", "user.email", "fmp-test@example.com", cwd=origin)
    _git("config", "user.name", "fmp-test", cwd=origin)
    parent = _write_commit(origin, "parent")
    child = _write_commit(origin, "child")
    origin_url = _file_url(origin)
    staged = tmp_path / "staged"
    clone = _git("clone", "--depth", "1", origin_url, str(staged))
    assert clone.returncode == 0, clone.stderr
    assert _git("rev-parse", "--is-shallow-repository", cwd=staged).stdout.strip() == "true"
    live = tmp_path / "live"
    live.mkdir()
    _git("init", cwd=live)
    _git("config", "user.email", "fmp-test@example.com", cwd=live)
    _git("config", "user.name", "fmp-test", cwd=live)
    fetch = _git(
        "fetch",
        "--update-head-ok",
        _file_url(staged),
        child,
        cwd=live,
        check=False,
    )
    checkout = _git("checkout", "--detach", child, cwd=live, check=False)
    assert checkout.returncode == 0, checkout.stderr + checkout.stdout
    return {
        "origin": origin_url,
        "origin_path": _posix(origin),
        "staged": _posix(staged),
        "live": _posix(live),
        "parent": parent,
        "child": child,
        "lib": _posix(LIB),
        "fetch_err": (fetch.stderr or "") + (fetch.stdout or ""),
    }


def test_shallow_stage_into_live_checkout_is_unwalkable(ancestry_repos):
    parent = ancestry_repos["parent"]
    child = ancestry_repos["child"]
    live = Path(ancestry_repos["live"])
    assert _git("rev-parse", "HEAD", cwd=live).stdout.strip() == child
    parent_obj = _git("cat-file", "-e", "{0}^{{commit}}".format(parent), cwd=live, check=False)
    log = _git("log", "-1", "--format=%H", cwd=live, check=False)
    assert parent_obj.returncode != 0
    assert log.returncode != 0
    combined = (log.stderr or "") + (log.stdout or "") + ancestry_repos["fetch_err"]
    assert "Failed to traverse parents" in combined or "Could not read" in combined
    assert "shallow roots are not allowed to be updated" in ancestry_repos["fetch_err"] or (
        parent[:12] in combined
    )


def test_activate_live_checkout_repairs_shallow_stage_graph(ancestry_repos):
    script = """
set -euo pipefail
. "{lib}"
activate_live_checkout "{live}" "{child}" "{origin}" "{staged}"
test "$(git -C "{live}" rev-parse HEAD)" = "{child}"
test "$(git -C "{live}" log -1 --format=%H)" = "{child}"
git -C "{live}" cat-file -e "{parent}^{{commit}}"
git -C "{live}" rev-list --max-count=2 HEAD >/dev/null
""".format(
        lib=ancestry_repos["lib"],
        live=ancestry_repos["live"],
        child=ancestry_repos["child"],
        origin=ancestry_repos["origin"],
        staged=ancestry_repos["staged"],
        parent=ancestry_repos["parent"],
    )
    result = _bash(script, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "git_graph=connected sha={0}".format(ancestry_repos["child"]) in result.stdout
    live = Path(ancestry_repos["live"])
    assert _git("rev-parse", "HEAD", cwd=live).stdout.strip() == ancestry_repos["child"]
    assert _git("log", "-1", "--format=%H", cwd=live).stdout.strip() == ancestry_repos["child"]


def test_activate_live_checkout_discards_leftover_tracked_dirt(tmp_path: Path):
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git and bash are required for checkout activation tests")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", cwd=origin)
    _git("config", "user.email", "fmp-test@example.com", cwd=origin)
    _git("config", "user.name", "fmp-test", cwd=origin)
    (origin / "stable.txt").write_text("canonical\n", encoding="utf-8")
    _git("add", "stable.txt", cwd=origin)
    _git("commit", "-m", "stable", cwd=origin)
    sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    live = tmp_path / "live"
    _git("clone", _file_url(origin), str(live))
    (live / "stable.txt").write_text("host dirt\n", encoding="utf-8")
    dirty = _git("status", "--porcelain", "--untracked-files=no", cwd=live)
    assert "stable.txt" in dirty.stdout
    script = """
set -euo pipefail
. "{lib}"
activate_live_checkout "{live}" "{sha}" "{origin}"
test "$(git -C "{live}" status --porcelain --untracked-files=no)" = ""
grep -qx canonical "{live}/stable.txt"
""".format(
        lib=_posix(LIB),
        live=_posix(live),
        sha=sha,
        origin=_file_url(origin),
    )
    result = _bash(script, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (live / "stable.txt").read_text(encoding="utf-8") == "canonical\n"
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=live).stdout == ""


def test_assert_commit_graph_rejects_missing_parent(ancestry_repos):
    result = _bash(
        'set -euo pipefail; . "{lib}"; assert_commit_graph "{live}" "{child}"'.format(
            lib=ancestry_repos["lib"],
            live=ancestry_repos["live"],
            child=ancestry_repos["child"],
        ),
        check=False,
    )
    assert result.returncode != 0
    combined = result.stderr + result.stdout
    assert "incomplete" in combined or "missing required parent" in combined or "git log -1 failed" in combined
