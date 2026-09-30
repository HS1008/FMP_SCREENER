"""Unused immutable release trees are pruned without touching keep pointers."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib_release_prune.sh"
HOST = ROOT / "scripts" / "deploy_host.sh"


def _sha(n: int) -> str:
    return "{0:040x}".format(n)


def _write_tree(root: Path, sha: str, *, complete: bool) -> Path:
    tree = root / sha
    bin_dir = tree / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(python.stat().st_mode | stat.S_IEXEC)
    if complete:
        streamlit = bin_dir / "streamlit"
        streamlit.write_text("#!/bin/sh\n", encoding="utf-8")
        streamlit.chmod(streamlit.stat().st_mode | stat.S_IEXEC)
    return tree


def _run_prune(tmp_path: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_extra or {})
    script = """
set -euo pipefail
# shellcheck disable=SC1091
. "{lib}"
prune_unused_releases
remove_incomplete_release_venv "${{STAGED:-}}"
""".format(lib=LIB)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_piped_host_embeds_release_prune_lib():
    lib = LIB.read_text(encoding="utf-8")
    host = HOST.read_text(encoding="utf-8")
    start = "# BEGIN EMBEDDED scripts/lib_release_prune.sh\n"
    end = "# END EMBEDDED scripts/lib_release_prune.sh"
    assert start in host
    assert end in host
    embedded = host.split(start, 1)[1].split(end, 1)[0]
    assert embedded == lib
    assert host.index("prune_unused_releases") < host.index('python3 -m venv "$STAGED/venv"')
    assert "stage-only must not move current/previous pointers" in host


def test_prune_keeps_pointers_and_deletes_unused_and_incomplete(tmp_path: Path):
    releases = tmp_path / "releases"
    releases.mkdir()
    current_sha = _sha(1)
    previous_sha = _sha(2)
    verified_sha = _sha(3)
    requested_sha = _sha(4)
    extra_sha = _sha(5)
    unused_old = _sha(6)
    unused_incomplete = _sha(7)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep-me").write_text("no\n", encoding="utf-8")

    for sha in (current_sha, previous_sha, verified_sha, requested_sha, extra_sha, unused_old):
        _write_tree(releases, sha, complete=True)
    _write_tree(releases, unused_incomplete, complete=False)
    (releases / "not-a-sha").mkdir()
    (releases / "not-a-sha" / "stay").write_text("yes\n", encoding="utf-8")

    current = tmp_path / "current"
    previous = tmp_path / "previous"
    current.symlink_to(releases / current_sha)
    previous.symlink_to(releases / previous_sha)
    # Newest extra complete unused tree wins the keep-extra slot.
    os.utime(releases / extra_sha, None)
    os.utime(releases / unused_old, (0, 0))

    state = tmp_path / "state"
    state.mkdir()
    (state / "last_verified.sha").write_text(verified_sha + "\n", encoding="utf-8")

    escaped = tmp_path / "escaped-link"
    escaped.symlink_to(outside)

    env = {
        "RELEASE_ROOT": str(releases),
        "CURRENT_LINK": str(current),
        "PREVIOUS_LINK": str(previous),
        "STATE_DIR": str(state),
        "FMP_RELEASE_PRUNE_KEEP_SHA": requested_sha,
        "FMP_RELEASE_KEEP_EXTRA": "1",
    }
    result = _run_prune(tmp_path, env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (releases / current_sha).is_dir()
    assert (releases / previous_sha).is_dir()
    assert (releases / verified_sha).is_dir()
    assert (releases / requested_sha).is_dir()
    assert (releases / extra_sha).is_dir()
    assert not (releases / unused_old).exists()
    assert not (releases / unused_incomplete).exists()
    assert (releases / "not-a-sha" / "stay").is_file()
    assert (outside / "keep-me").is_file()
    assert "reason=current" in result.stdout
    assert "reason=previous" in result.stdout
    assert "reason=last_verified" in result.stdout
    assert "reason=requested" in result.stdout
    assert "reason=extra_recent" in result.stdout
    assert "release_prune=delete sha={0}".format(unused_old) in result.stdout
    assert "release_prune=delete sha={0}".format(unused_incomplete) in result.stdout


def test_incomplete_requested_venv_is_removed_not_the_tree(tmp_path: Path):
    releases = tmp_path / "releases"
    requested = _sha(11)
    tree = _write_tree(releases, requested, complete=False)
    env = {
        "RELEASE_ROOT": str(releases),
        "CURRENT_LINK": str(tmp_path / "missing-current"),
        "PREVIOUS_LINK": str(tmp_path / "missing-previous"),
        "STATE_DIR": str(tmp_path / "state"),
        "FMP_RELEASE_PRUNE_KEEP_SHA": requested,
        "STAGED": str(tree),
        "FMP_RELEASE_KEEP_EXTRA": "0",
    }
    result = _run_prune(tmp_path, env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert tree.is_dir()
    assert not (tree / "venv").exists()
    assert "incomplete_release_venv=remove" in result.stdout


def test_prune_can_be_disabled(tmp_path: Path):
    releases = tmp_path / "releases"
    unused = _sha(21)
    _write_tree(releases, unused, complete=True)
    env = {
        "RELEASE_ROOT": str(releases),
        "CURRENT_LINK": str(tmp_path / "missing-current"),
        "PREVIOUS_LINK": str(tmp_path / "missing-previous"),
        "STATE_DIR": str(tmp_path / "state"),
        "FMP_RELEASE_PRUNE": "0",
        "FMP_RELEASE_KEEP_EXTRA": "0",
    }
    result = _run_prune(tmp_path, env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (releases / unused).is_dir()
    assert "release_prune=skipped" in result.stdout
