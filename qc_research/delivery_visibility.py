"""Research-delivery visibility: separate, observable facts about remote fetch, local fallback,
artifact identity/age, and what was actually delivered.

Problem this fixes: the hourly ingest listed ``research/platform_smokes`` on the QS default
branch (HTTP 404), fell back to the committed FMP artifacts, ingested them, and Actions
reported success. Nothing distinguished "new remote artifact delivered" from "old local
copy re-ingested". This module records both truthfully:

* ``remote``      - attempted?, OK/BLOCKED, configured repo/ref/path, redacted reason
* ``ingest``      - source REMOTE / LOCAL_FALLBACK / LOCAL_EXPLICIT, artifact path, sha256,
                    last git commit + date touching the file (artifact age; provenance, not market as-of)
* ``upstream_delivery_status``  FRESH_REMOTE | BLOCKED | NOT_ATTEMPTED
* ``downstream_data_status``    FRESH_REMOTE | LAST_KNOWN_GOOD | NONE
* ``claims.new_remote_artifact_delivered``  explicit boolean

The ``build`` subcommand is stdlib-only (it runs on a bare Actions runner). ``record`` writes
the report into the Market Intelligence tables (``mi_ingestion_runs`` / ``mi_data_freshness``
for source ``QS_RESEARCH_DELIVERY``) on the authorized host so the Data Health page shows it.
No research economics or frozen artifacts are touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "delivery_visibility_v1"
SOURCE_ID = "QS_RESEARCH_DELIVERY"
DATASET = "platform_research_artifacts"

REMOTE_OK = "OK"
REMOTE_BLOCKED = "BLOCKED"
REMOTE_NOT_ATTEMPTED = "NOT_ATTEMPTED"

SOURCE_REMOTE = "REMOTE"
SOURCE_LOCAL_FALLBACK = "LOCAL_FALLBACK"
SOURCE_LOCAL_EXPLICIT = "LOCAL_EXPLICIT"
SOURCE_NONE = "NONE"

_SECRET_ENV = ("QS_READ_TOKEN", "CROSS_REPO_DISPATCH_TOKEN", "GH_PAT", "GITHUB_TOKEN")


def redact(text: str) -> str:
    out = str(text)
    for name in _SECRET_ENV:
        value = os.environ.get(name)
        if value:
            out = out.replace(value, "[REDACTED]")
    return out[:400]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_last_commit(path: Path, repo_root: Path) -> dict[str, Any]:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%H%x00%cI", "--", str(path)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_committed_at": None}
    if not out:
        return {"git_commit": None, "git_committed_at": None}
    sha, _, committed = out.partition("\x00")
    return {"git_commit": sha or None, "git_committed_at": committed or None}


def describe_artifact(path: Path, *, repo_root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Identity and age facts for one local artifact file (no research fields are modified)."""
    now = now or datetime.now(timezone.utc)
    info: dict[str, Any] = {"path": str(path), "bytes": path.stat().st_size, "sha256": _sha256_file(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if isinstance(payload, dict):
        info["strategy_id"] = payload.get("strategy_id")
        info["research_status"] = payload.get("research_status") or payload.get("run_status") or payload.get("state")
        info["artifact_claims_delivery_status"] = payload.get("delivery_status")
        info["research_lineage_id"] = payload.get("research_lineage_id")
    info.update(_git_last_commit(path, repo_root))
    committed = info.get("git_committed_at")
    if committed:
        try:
            committed_dt = datetime.fromisoformat(str(committed))
            if committed_dt.tzinfo is None:
                committed_dt = committed_dt.replace(tzinfo=timezone.utc)
            info["age_days_since_commit"] = round((now - committed_dt).total_seconds() / 86400.0, 2)
        except ValueError:
            info["age_days_since_commit"] = None
    else:
        info["age_days_since_commit"] = None
    info["note"] = "git commit date is file provenance, not a market as-of or a fresh remote delivery"
    return info


def _artifact_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(p for p in target.rglob("*.json") if p.is_file())
    return []


def build_report(
    *,
    event: str,
    target: Path,
    repo_root: Path,
    remote: dict[str, Any] | None,
    remote_config: dict[str, Any],
    explicit_local: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the delivery report from the pull/fetch result and the resolved ingest target.

    ``remote`` is the JSON emitted by ``qc_research.pull_public_platform_artifacts`` (scheduled
    path) or a ``{"attempted": True, "status": ..., "reason": ...}`` dict for a dispatched fetch.
    ``None`` means no remote attempt was made (explicit local ingest).
    """
    now = now or datetime.now(timezone.utc)
    target = Path(target)
    files = _artifact_files(target)
    artifacts = [describe_artifact(p, repo_root=repo_root, now=now) for p in files]

    if remote is None:
        remote_status = REMOTE_NOT_ATTEMPTED
        remote_reason = "no remote fetch requested"
        pulled = 0
    else:
        blocked = bool(remote.get("blocked")) or str(remote.get("status") or "").upper() == REMOTE_BLOCKED
        pulled = int(remote.get("pulled") or 0)
        remote_status = REMOTE_OK if (not blocked and pulled > 0) else REMOTE_BLOCKED
        remote_reason = redact(str(remote.get("reason") or ("no complete remote artifacts" if not blocked else "remote unavailable")))

    incoming = "incoming" in target.parts
    if remote_status == REMOTE_OK and incoming:
        ingest_source = SOURCE_REMOTE
    elif explicit_local and remote is None:
        ingest_source = SOURCE_LOCAL_EXPLICIT
    elif artifacts:
        ingest_source = SOURCE_LOCAL_FALLBACK
    else:
        ingest_source = SOURCE_NONE

    upstream = {REMOTE_OK: "FRESH_REMOTE", REMOTE_BLOCKED: "BLOCKED", REMOTE_NOT_ATTEMPTED: "NOT_ATTEMPTED"}[remote_status]
    if ingest_source == SOURCE_REMOTE:
        downstream = "FRESH_REMOTE"
    elif artifacts:
        downstream = "LAST_KNOWN_GOOD"
    else:
        downstream = "NONE"

    newest_commit = max((a.get("git_committed_at") or "" for a in artifacts), default="") or None
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "event": event,
        "remote": {
            "attempted": remote is not None,
            "status": remote_status,
            "repo": remote_config.get("repo"),
            "ref": remote_config.get("ref") or None,
            "ref_source": remote_config.get("ref_source") or ("unset -> provider default branch" if not remote_config.get("ref") else "explicit"),
            "path": remote_config.get("path"),
            "pulled": pulled,
            "skipped": int((remote or {}).get("skipped") or 0),
            "reason": remote_reason,
        },
        "ingest": {
            "source": ingest_source,
            "target": str(target),
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
            "newest_local_commit_at": newest_commit,
        },
        "upstream_delivery_status": upstream,
        "downstream_data_status": downstream,
        "claims": {
            "new_remote_artifact_delivered": ingest_source == SOURCE_REMOTE,
            "local_fallback_ingested": ingest_source == SOURCE_LOCAL_FALLBACK,
        },
        "notes": [
            "A successful local-fallback ingest re-publishes previously delivered artifacts; it is not a new remote delivery.",
            "Remote BLOCKED keeps previous valid research rows in PostgreSQL (LAST_KNOWN_GOOD).",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    remote = report["remote"]
    ingest = report["ingest"]
    lines = [
        "## Research delivery ({0})".format(report["event"]),
        "",
        "| Fact | Value |",
        "|------|-------|",
        "| Remote fetch | {0} (attempted={1}) |".format(remote["status"], remote["attempted"]),
        "| Remote source | `{0}` @ `{1}` ({2}) path `{3}` |".format(remote["repo"], remote["ref"] or "default branch", remote["ref_source"], remote["path"]),
        "| Remote reason | {0} |".format(remote["reason"]),
        "| Ingest source | **{0}** ({1} artifact(s) from `{2}`) |".format(ingest["source"], ingest["artifact_count"], ingest["target"]),
        "| Upstream delivery | **{0}** |".format(report["upstream_delivery_status"]),
        "| Downstream data | **{0}** |".format(report["downstream_data_status"]),
        "| New remote artifact delivered | **{0}** |".format(report["claims"]["new_remote_artifact_delivered"]),
        "",
    ]
    if ingest["artifacts"]:
        lines += ["| Artifact | strategy | sha256 | last commit | age (d) |", "|---|---|---|---|---|"]
        for a in ingest["artifacts"]:
            lines.append("| `{0}` | {1} | `{2}` | `{3}` {4} | {5} |".format(Path(a["path"]).name, a.get("strategy_id"), a["sha256"][:12], (a.get("git_commit") or "")[:10], a.get("git_committed_at") or "", a.get("age_days_since_commit")))
    return "\n".join(lines) + "\n"


def record_to_postgres(engine, report: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """Persist the report as an ingestion run + freshness row for source QS_RESEARCH_DELIVERY."""
    from sqlalchemy import text

    from market_intelligence.store import RUN_FAILED, RUN_SUCCEEDED, finish_run, record_freshness, start_run, upsert_source_registry

    remote = report["remote"]
    ingest = report["ingest"]
    newest = ingest.get("newest_local_commit_at")
    latest_obs = date.fromisoformat(str(newest)[:10]) if newest else None
    transport_ok = remote["status"] == REMOTE_OK
    with engine.begin() as conn:
        upsert_source_registry(
            conn,
            entries=[
                {
                    "source_id": SOURCE_ID,
                    "provider": "hs1008/quant-strategies (GitHub)",
                    "dataset": DATASET,
                    "source_url": "https://github.com/{0}".format(remote.get("repo") or "hs1008/quant-strategies"),
                    "expected_cadence": "ON_DEMAND",
                    "usage_scope": "INTERNAL_ONLY",
                    "attribution": "Canonical QuantConnect research artifacts published by quant-strategies.",
                    "terms_notes": "Read-only fetch of committed JSON artifacts; no QC calls; no model binaries.",
                    "units_metadata": {"artifact": "canonical research JSON"},
                }
            ],
            enabled={SOURCE_ID: True},
            access={SOURCE_ID: "CONFIGURED" if remote.get("ref") else "SOURCE_REF_NOT_CONFIGURED"},
        )
        run_id = start_run(conn, source_id=SOURCE_ID, dataset=DATASET)
        finish_run(
            conn,
            run_id,
            status=RUN_SUCCEEDED if transport_ok else RUN_FAILED,
            counts={"received": ingest["artifact_count"], "inserted": remote["pulled"], "unchanged": ingest["artifact_count"] if ingest["source"] != SOURCE_REMOTE else 0},
            error_redacted=None if transport_ok else "remote {0}: {1}".format(remote["status"], remote["reason"]),
            details={k: report[k] for k in ("event", "remote", "upstream_delivery_status", "downstream_data_status", "claims")} | {"ingest": {k: v for k, v in ingest.items() if k != "artifacts"}, "artifact_hashes": [a["sha256"] for a in ingest["artifacts"]]},
        )
        freshness = record_freshness(
            conn,
            source_id=SOURCE_ID,
            dataset=DATASET,
            cadence="ON_DEMAND",
            transport_status="OK" if transport_ok else "FAILED",
            latest_observation=latest_obs,
            success=transport_ok,
            error_redacted=None if transport_ok else "upstream BLOCKED; downstream {0}".format(report["downstream_data_status"]),
            run_id=run_id,
            today=today,
        )
        conn.execute(text("SELECT 1"))
    return {"run_id": run_id, "freshness_status": freshness, "transport_status": "OK" if transport_ok else "FAILED"}


def _load_json(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {"blocked": True, "reason": "pull report unreadable"}
    return payload if isinstance(payload, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research delivery visibility")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="Build the delivery report (stdlib only)")
    b.add_argument("--event", required=True)
    b.add_argument("--target", required=True, help="Resolved ingest target (file or directory)")
    b.add_argument("--pull-report", default=None, help="JSON written by pull_public_platform_artifacts")
    b.add_argument("--remote-status", default=None, choices=[REMOTE_OK, REMOTE_BLOCKED], help="Dispatched fetch outcome when no pull report exists")
    b.add_argument("--remote-reason", default="")
    b.add_argument("--repo", default="hs1008/quant-strategies")
    b.add_argument("--ref", default="")
    b.add_argument("--ref-source", default="")
    b.add_argument("--path", default="")
    b.add_argument("--explicit-local", action="store_true")
    b.add_argument("--out", required=True)
    b.add_argument("--summary", default=None, help="Append markdown to this file (e.g. $GITHUB_STEP_SUMMARY)")
    r = sub.add_parser("record", help="Record a built report into PostgreSQL (authorized host only)")
    r.add_argument("--report", required=True)
    r.add_argument(
        "--require-postgres",
        action="store_true",
        help="Fail closed when the writer engine is missing or the report is unreadable",
    )
    ns = parser.parse_args(argv)

    if ns.command == "build":
        remote: dict[str, Any] | None = _load_json(ns.pull_report)
        if remote is None and ns.remote_status:
            remote = {"attempted": True, "status": ns.remote_status, "blocked": ns.remote_status == REMOTE_BLOCKED, "pulled": 1 if ns.remote_status == REMOTE_OK else 0, "reason": ns.remote_reason}
        report = build_report(
            event=ns.event,
            target=Path(ns.target),
            repo_root=Path.cwd(),
            remote=remote,
            remote_config={"repo": ns.repo, "ref": ns.ref, "ref_source": ns.ref_source, "path": ns.path},
            explicit_local=ns.explicit_local,
        )
        out = Path(ns.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        markdown = render_markdown(report)
        print(markdown)
        if ns.summary:
            with open(ns.summary, "a", encoding="utf-8") as fh:
                fh.write(markdown)
        return 0

    report = _load_json(ns.report)
    if report is None:
        print("delivery report missing or unreadable; nothing recorded")
        return 2 if ns.require_postgres else 0
    from qc_research.platform_ingest import IngestEnvironmentError, postgres_engine

    try:
        engine = postgres_engine()
    except IngestEnvironmentError as exc:
        print("PostgreSQL not configured; delivery report not recorded ({0})".format(exc.__class__.__name__))
        return 2 if ns.require_postgres else 0
    result = record_to_postgres(engine, report)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
