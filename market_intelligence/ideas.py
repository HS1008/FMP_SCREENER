"""Research idea registry (IdeaLab thin contracts) - backend writer, dry-run only.

Path 2 of the platform: morning snapshot -> human/AI hypothesis -> versioned idea -> frozen
spec -> explicit human research approval bound to the exact spec hash -> DRY-RUN queue that
emits a contract for the existing QuantConnect research infrastructure -> canonical results
linked back by ``research_run_id`` (never duplicated) -> human review.

Hard rules encoded here (not policy prose):
* every spec change after a freeze creates a new version, resets the lifecycle and revokes
  approvals of superseded versions;
* approval binds to (idea_id, version, spec_hash) - a changed hash has no approval;
* unsupported research types register honestly with ``execution_support=UNSUPPORTED``;
* specs must not touch data on/after ``HOLDOUT_START`` (2025-01-01) or any holdout flag;
* automatic execution is disabled: ``queue_research`` only records a DRY_RUN plan.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from market_intelligence import CODE_VERSION
from market_intelligence.nulls import canonical_sha256, normalize_payload, strict_dumps

SCHEMA_VERSION = "idea_spec_v1"
CONTRACT_SCHEMA_VERSION = "idea_research_contract_v1"
HOLDOUT_START = date(2025, 1, 1)
DEFAULT_HOLDOUT_POLICY = {"holdout_start": HOLDOUT_START.isoformat(), "access": "NONE", "note": "Final holdout and 2025+ data are never read by idea research."}

# Lifecycle
DRAFT = "DRAFT"
SPEC_FROZEN = "SPEC_FROZEN"
RESEARCH_APPROVED = "RESEARCH_APPROVED"
RESEARCH_QUEUED = "RESEARCH_QUEUED"
RESEARCH_COMPLETE = "RESEARCH_COMPLETE"
HUMAN_REVIEW = "HUMAN_REVIEW"
ARCHIVED = "ARCHIVED"
REJECTED = "REJECTED"
STATES = (DRAFT, SPEC_FROZEN, RESEARCH_APPROVED, RESEARCH_QUEUED, RESEARCH_COMPLETE, HUMAN_REVIEW, ARCHIVED, REJECTED)

TRANSITIONS: dict[str, set[str]] = {
    DRAFT: {SPEC_FROZEN, ARCHIVED, REJECTED},
    SPEC_FROZEN: {RESEARCH_APPROVED, DRAFT, ARCHIVED, REJECTED},
    RESEARCH_APPROVED: {RESEARCH_QUEUED, SPEC_FROZEN, ARCHIVED, REJECTED},
    RESEARCH_QUEUED: {RESEARCH_COMPLETE, RESEARCH_APPROVED, ARCHIVED, REJECTED},
    RESEARCH_COMPLETE: {HUMAN_REVIEW, ARCHIVED},
    HUMAN_REVIEW: {ARCHIVED, REJECTED, DRAFT},
    ARCHIVED: set(),
    REJECTED: {DRAFT},
}

# Execution support is a fact about the platform today, not about the idea's merit.
SUPPORT_EXISTING_INFRA = "SUPPORTED_EXISTING_INFRA"
SUPPORT_DRY_RUN_CONTRACT = "SUPPORTED_DRY_RUN_CONTRACT"
SUPPORT_UNSUPPORTED = "UNSUPPORTED"
RESEARCH_TYPES: dict[str, dict[str, str]] = {
    "SINGLE_ASSET_MOMENTUM": {"support": SUPPORT_EXISTING_INFRA, "adapter": "quant-strategies platform research (TLTDurationMomentum-style WFO)"},
    "CROSS_SECTIONAL_FACTOR_ML": {"support": SUPPORT_EXISTING_INFRA, "adapter": "quant-strategies Stage 2 cross-sectional factor ML"},
    "SECTOR_ROTATION_DIAGNOSTIC": {"support": SUPPORT_DRY_RUN_CONTRACT, "adapter": "quant-strategies MarketIntelligenceResearch sector diagnostics (contract only; first 2025+ activation is a human gate)"},
    "MACRO_REGIME_OVERLAY": {"support": SUPPORT_UNSUPPORTED, "adapter": ""},
    "CREDIT_SPREAD_SIGNAL": {"support": SUPPORT_UNSUPPORTED, "adapter": ""},
    "PAIRS_RELATIVE_VALUE": {"support": SUPPORT_UNSUPPORTED, "adapter": ""},
    "BOND_RELATIVE_VALUE": {"support": SUPPORT_UNSUPPORTED, "adapter": ""},
    "OTHER": {"support": SUPPORT_UNSUPPORTED, "adapter": ""},
}

REQUIRED_SPEC_FIELDS = ("title", "research_type", "hypothesis", "universe", "signal", "horizon", "data_used")
_HOLDOUT_TOKENS = re.compile(r"holdout|final[_ ]?test|2025|2026", re.IGNORECASE)


class IdeaError(ValueError):
    """Validation or lifecycle rule violated (message is safe to show)."""


class ExecutionBlocked(RuntimeError):
    """Automatic research execution is disabled by policy."""


@dataclass
class SpecValidation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    spec_hash: str | None = None
    normalized: dict[str, Any] | None = None
    execution_support: str = SUPPORT_UNSUPPORTED


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _scan_dates(obj: Any, found: list[tuple[str, date]], path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _scan_dates(v, found, "{0}.{1}".format(path, k) if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_dates(v, found, "{0}[{1}]".format(path, i))
    elif isinstance(obj, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", obj):
        try:
            found.append((path, date.fromisoformat(obj)))
        except ValueError:
            pass


def validate_spec(spec: dict[str, Any]) -> SpecValidation:
    """Structural + boundary validation. Never mutates the input."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(spec, dict):
        return SpecValidation(False, ["spec must be a JSON object"])
    for field_name in REQUIRED_SPEC_FIELDS:
        if spec.get(field_name) in (None, "", [], {}):
            errors.append("missing required field: {0}".format(field_name))
    rtype = str(spec.get("research_type") or "")
    support_info = RESEARCH_TYPES.get(rtype)
    if support_info is None:
        errors.append("unknown research_type {0!r}; known: {1}".format(rtype, ", ".join(sorted(RESEARCH_TYPES))))
        support = SUPPORT_UNSUPPORTED
    else:
        support = support_info["support"]
        if support == SUPPORT_UNSUPPORTED:
            warnings.append("research_type {0} has no execution adapter; it can be registered and approved but only recorded as UNSUPPORTED when queued".format(rtype))

    data_used = spec.get("data_used")
    if data_used is not None and not isinstance(data_used, list):
        errors.append("data_used must be a list of {source_id, dataset/series} references")
    elif isinstance(data_used, list):
        for i, ref in enumerate(data_used):
            if not isinstance(ref, dict) or not ref.get("source_id"):
                errors.append("data_used[{0}] must be an object with source_id".format(i))

    policy = spec.get("holdout_policy") or {}
    if not isinstance(policy, dict):
        errors.append("holdout_policy must be an object")
        policy = {}
    access = str(policy.get("access") or "NONE").upper()
    if access != "NONE":
        errors.append("holdout_policy.access must be NONE (got {0})".format(access))
    holdout_start = _parse_date(policy.get("holdout_start")) or HOLDOUT_START
    if holdout_start > HOLDOUT_START:
        errors.append("holdout_policy.holdout_start {0} is later than the platform boundary {1}".format(holdout_start, HOLDOUT_START))

    dates: list[tuple[str, date]] = []
    _scan_dates({k: v for k, v in spec.items() if k not in {"holdout_policy", "conception_at"}}, dates)
    for path, d in dates:
        if d >= HOLDOUT_START:
            errors.append("{0}={1} touches the protected window (>= {2})".format(path, d.isoformat(), HOLDOUT_START.isoformat()))
    for key in ("universe", "signal", "params"):
        blob = strict_dumps(spec.get(key)) if spec.get(key) is not None else ""
        if _HOLDOUT_TOKENS.search(blob):
            warnings.append("{0} mentions holdout/2025+ tokens; verify the spec does not require protected data".format(key))

    normalized = normalize_payload({k: v for k, v in spec.items() if k != "conception_at"})
    normalized.setdefault("schema_version", SCHEMA_VERSION)
    normalized["holdout_policy"] = {**DEFAULT_HOLDOUT_POLICY, **(policy or {}), "access": "NONE", "holdout_start": HOLDOUT_START.isoformat()}
    spec_hash = canonical_sha256(normalized)
    return SpecValidation(not errors, errors, warnings, spec_hash, normalized, support)


def _slug(title: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", title.upper()).strip("_")[:40] or "IDEA"


def _idea_row(conn, idea_id: str) -> dict[str, Any]:
    row = conn.execute(text("SELECT * FROM mi_research_ideas WHERE idea_id = :i FOR UPDATE"), {"i": idea_id}).mappings().first()
    if row is None:
        raise IdeaError("unknown idea {0}".format(idea_id))
    return dict(row)


def _current_version(conn, idea_id: str, version: int) -> dict[str, Any]:
    row = conn.execute(text("SELECT * FROM mi_research_idea_versions WHERE idea_id = :i AND version = :v"), {"i": idea_id, "v": version}).mappings().first()
    if row is None:
        raise IdeaError("idea {0} has no version {1}".format(idea_id, version))
    return dict(row)


def _transition(conn, idea: dict[str, Any], to_state: str, *, actor: str, actor_kind: str, reason: str | None, version: int | None, bound_spec_hash: str | None) -> None:
    from_state = idea["current_state"]
    if to_state not in TRANSITIONS.get(from_state, set()):
        raise IdeaError("illegal transition {0} -> {1} for {2}".format(from_state, to_state, idea["idea_id"]))
    if actor_kind == "AI" and to_state in {RESEARCH_APPROVED, RESEARCH_QUEUED, RESEARCH_COMPLETE, HUMAN_REVIEW}:
        raise IdeaError("AI actors cannot move ideas into {0}; only humans approve/queue/review".format(to_state))
    conn.execute(
        text(
            """
            INSERT INTO mi_research_idea_transitions (idea_id, version, from_state, to_state, actor, actor_kind, reason, bound_spec_hash)
            VALUES (:idea_id, :version, :from_state, :to_state, :actor, :actor_kind, :reason, :bound)
            """
        ),
        {"idea_id": idea["idea_id"], "version": version, "from_state": from_state, "to_state": to_state, "actor": actor, "actor_kind": actor_kind, "reason": reason, "bound": bound_spec_hash},
    )
    conn.execute(text("UPDATE mi_research_ideas SET current_state = :s, updated_at = NOW() WHERE idea_id = :i"), {"s": to_state, "i": idea["idea_id"]})
    idea["current_state"] = to_state


def _snapshot_ref(conn, source_snapshot_id: str | None, conception_at: datetime) -> tuple[str | None, str | None]:
    if not source_snapshot_id:
        return None, None
    row = conn.execute(text("SELECT snapshot_sha256, cutoff_at FROM mi_morning_context_snapshots WHERE snapshot_id = :s"), {"s": source_snapshot_id}).first()
    if row is None:
        raise IdeaError("source_snapshot_id {0} does not exist in mi_morning_context_snapshots".format(source_snapshot_id))
    cutoff = row.cutoff_at
    if cutoff is not None and cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    if cutoff is not None and cutoff > conception_at:
        raise IdeaError("idea conceived at {0} references a snapshot cut off later ({1}); inputs must precede the idea".format(conception_at.isoformat(), cutoff.isoformat()))
    return source_snapshot_id, row.snapshot_sha256


def register_idea(conn, spec: dict[str, Any], *, actor: str, actor_kind: str = "HUMAN", source_snapshot_id: str | None = None, conception_at: datetime | None = None) -> dict[str, Any]:
    """Create idea + version 1 in DRAFT. Returns the idea row with validation details."""
    validation = validate_spec(spec)
    if not validation.ok:
        raise IdeaError("; ".join(validation.errors))
    conception = conception_at or _parse_dt(spec.get("conception_at")) or utcnow()
    snap_id, snap_hash = _snapshot_ref(conn, source_snapshot_id or spec.get("source_snapshot_id"), conception)
    idea_id = "idea_{0}".format(uuid.uuid4().hex[:12])
    lineage_id = "LINEAGE_IDEA_{0}_{1}".format(_slug(str(spec["title"])), validation.spec_hash[:8])
    conn.execute(
        text(
            """
            INSERT INTO mi_research_ideas (idea_id, lineage_id, title, research_type, current_state, current_version, created_by)
            VALUES (:idea_id, :lineage_id, :title, :rtype, :state, 1, :actor)
            """
        ),
        {"idea_id": idea_id, "lineage_id": lineage_id, "title": str(spec["title"]), "rtype": str(spec["research_type"]), "state": DRAFT, "actor": actor},
    )
    _insert_version(conn, idea_id, 1, validation, snap_id, snap_hash, conception, actor)
    conn.execute(
        text("INSERT INTO mi_research_idea_transitions (idea_id, version, from_state, to_state, actor, actor_kind, reason, bound_spec_hash) VALUES (:i, 1, NULL, :s, :a, :k, 'registered', :h)"),
        {"i": idea_id, "s": DRAFT, "a": actor, "k": actor_kind, "h": validation.spec_hash},
    )
    return {"idea_id": idea_id, "lineage_id": lineage_id, "version": 1, "spec_hash": validation.spec_hash, "state": DRAFT, "execution_support": validation.execution_support, "warnings": validation.warnings}


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _insert_version(conn, idea_id: str, version: int, validation: SpecValidation, snap_id: str | None, snap_hash: str | None, conception: datetime, actor: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_research_idea_versions (idea_id, version, spec_json, spec_hash, research_type, source_snapshot_id, source_snapshot_hash,
                conception_at, data_used_json, holdout_policy_json, execution_support, created_by)
            VALUES (:idea_id, :version, CAST(:spec AS JSONB), :hash, :rtype, :snap_id, :snap_hash, :conception, CAST(:data_used AS JSONB), CAST(:policy AS JSONB), :support, :actor)
            """
        ),
        {
            "idea_id": idea_id,
            "version": version,
            "spec": strict_dumps(validation.normalized),
            "hash": validation.spec_hash,
            "rtype": str(validation.normalized["research_type"]),
            "snap_id": snap_id,
            "snap_hash": snap_hash,
            "conception": conception,
            "data_used": strict_dumps(validation.normalized.get("data_used")),
            "policy": strict_dumps(validation.normalized.get("holdout_policy")),
            "support": validation.execution_support,
            "actor": actor,
        },
    )


def revise_idea(conn, idea_id: str, spec: dict[str, Any], *, actor: str, actor_kind: str = "HUMAN", reason: str = "spec revised", source_snapshot_id: str | None = None, conception_at: datetime | None = None) -> dict[str, Any]:
    """New version; lifecycle resets to DRAFT; approvals of earlier versions are revoked."""
    idea = _idea_row(conn, idea_id)
    if idea["current_state"] in {ARCHIVED}:
        raise IdeaError("archived ideas cannot be revised; register a new idea")
    validation = validate_spec(spec)
    if not validation.ok:
        raise IdeaError("; ".join(validation.errors))
    current = _current_version(conn, idea_id, idea["current_version"])
    if current["spec_hash"] == validation.spec_hash:
        return {"idea_id": idea_id, "version": idea["current_version"], "spec_hash": validation.spec_hash, "state": idea["current_state"], "unchanged": True}
    version = int(idea["current_version"]) + 1
    conception = conception_at or _parse_dt(spec.get("conception_at")) or utcnow()
    snap_id, snap_hash = _snapshot_ref(conn, source_snapshot_id or spec.get("source_snapshot_id"), conception)
    _insert_version(conn, idea_id, version, validation, snap_id, snap_hash, conception, actor)
    revoked = conn.execute(
        text("UPDATE mi_research_idea_approvals SET revoked_at = NOW(), revoke_reason = :r WHERE idea_id = :i AND revoked_at IS NULL"),
        {"i": idea_id, "r": "superseded by version {0} (spec hash changed)".format(version)},
    ).rowcount
    conn.execute(text("UPDATE mi_research_ideas SET current_version = :v, title = :t, research_type = :rt, updated_at = NOW() WHERE idea_id = :i"), {"v": version, "t": str(spec["title"]), "rt": str(spec["research_type"]), "i": idea_id})
    if idea["current_state"] != DRAFT:
        conn.execute(
            text("INSERT INTO mi_research_idea_transitions (idea_id, version, from_state, to_state, actor, actor_kind, reason, bound_spec_hash) VALUES (:i, :v, :f, :s, :a, :k, :r, :h)"),
            {"i": idea_id, "v": version, "f": idea["current_state"], "s": DRAFT, "a": actor, "k": actor_kind, "r": reason, "h": validation.spec_hash},
        )
        conn.execute(text("UPDATE mi_research_ideas SET current_state = :s WHERE idea_id = :i"), {"s": DRAFT, "i": idea_id})
    return {"idea_id": idea_id, "version": version, "spec_hash": validation.spec_hash, "state": DRAFT, "approvals_revoked": int(revoked), "execution_support": validation.execution_support, "warnings": validation.warnings}


def freeze_spec(conn, idea_id: str, *, actor: str, actor_kind: str = "HUMAN", reason: str | None = None) -> dict[str, Any]:
    idea = _idea_row(conn, idea_id)
    version = _current_version(conn, idea_id, idea["current_version"])
    _transition(conn, idea, SPEC_FROZEN, actor=actor, actor_kind=actor_kind, reason=reason or "spec frozen", version=idea["current_version"], bound_spec_hash=version["spec_hash"])
    return {"idea_id": idea_id, "version": idea["current_version"], "spec_hash": version["spec_hash"], "state": SPEC_FROZEN}


def approve_research(conn, idea_id: str, *, approved_by: str, version: int, spec_hash: str, scope: str = "NON_HOLDOUT_RESEARCH", reason: str | None = None) -> dict[str, Any]:
    """Explicit human approval bound to the exact (version, spec_hash). Anything else is rejected."""
    idea = _idea_row(conn, idea_id)
    if int(version) != int(idea["current_version"]):
        raise IdeaError("approval targets version {0} but current version is {1}".format(version, idea["current_version"]))
    current = _current_version(conn, idea_id, version)
    if current["spec_hash"] != spec_hash:
        raise IdeaError("approval spec_hash does not match frozen spec (expected {0})".format(current["spec_hash"]))
    if scope != "NON_HOLDOUT_RESEARCH":
        raise IdeaError("only NON_HOLDOUT_RESEARCH scope can be approved through the registry")
    if idea["current_state"] != SPEC_FROZEN:
        raise IdeaError("idea must be SPEC_FROZEN to approve (state {0})".format(idea["current_state"]))
    conn.execute(
        text("INSERT INTO mi_research_idea_approvals (idea_id, version, spec_hash, scope, approved_by) VALUES (:i, :v, :h, :s, :a)"),
        {"i": idea_id, "v": version, "h": spec_hash, "s": scope, "a": approved_by},
    )
    _transition(conn, idea, RESEARCH_APPROVED, actor=approved_by, actor_kind="HUMAN", reason=reason or "research approved", version=version, bound_spec_hash=spec_hash)
    return {"idea_id": idea_id, "version": version, "spec_hash": spec_hash, "state": RESEARCH_APPROVED, "scope": scope}


def revoke_approval(conn, idea_id: str, *, actor: str, reason: str) -> dict[str, Any]:
    idea = _idea_row(conn, idea_id)
    revoked = conn.execute(text("UPDATE mi_research_idea_approvals SET revoked_at = NOW(), revoke_reason = :r WHERE idea_id = :i AND revoked_at IS NULL"), {"i": idea_id, "r": reason}).rowcount
    if idea["current_state"] in {RESEARCH_APPROVED, RESEARCH_QUEUED}:
        target = SPEC_FROZEN if idea["current_state"] == RESEARCH_APPROVED else RESEARCH_APPROVED
        # RESEARCH_QUEUED -> RESEARCH_APPROVED -> SPEC_FROZEN keeps the audit trail explicit.
        _transition(conn, idea, target, actor=actor, actor_kind="HUMAN", reason=reason, version=idea["current_version"], bound_spec_hash=None)
        if target == RESEARCH_APPROVED:
            _transition(conn, idea, SPEC_FROZEN, actor=actor, actor_kind="HUMAN", reason=reason, version=idea["current_version"], bound_spec_hash=None)
    return {"idea_id": idea_id, "approvals_revoked": int(revoked), "state": idea["current_state"]}


def active_approval(conn, idea_id: str, version: int, spec_hash: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT approved_by, approved_at, scope FROM mi_research_idea_approvals WHERE idea_id = :i AND version = :v AND spec_hash = :h AND revoked_at IS NULL ORDER BY approved_at DESC LIMIT 1"),
        {"i": idea_id, "v": version, "h": spec_hash},
    ).mappings().first()
    return dict(row) if row else None


def build_contract(conn, idea_id: str) -> dict[str, Any]:
    """Frozen IdeaLab -> QuantConnect research contract for the current approved version."""
    idea = _idea_row(conn, idea_id)
    version = _current_version(conn, idea_id, idea["current_version"])
    approval = active_approval(conn, idea_id, idea["current_version"], version["spec_hash"])
    if approval is None:
        raise IdeaError("no active approval bound to version {0} / spec {1}".format(idea["current_version"], version["spec_hash"]))
    support = RESEARCH_TYPES.get(version["research_type"], RESEARCH_TYPES["OTHER"])
    body = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "idea_id": idea_id,
        "lineage_id": idea["lineage_id"],
        "version": idea["current_version"],
        "spec_hash": version["spec_hash"],
        "research_type": version["research_type"],
        "execution_support": version["execution_support"],
        "adapter": support["adapter"],
        "spec": version["spec_json"],
        "holdout_policy": version["holdout_policy_json"],
        "data_used": version["data_used_json"],
        "source_snapshot": {"snapshot_id": version["source_snapshot_id"], "snapshot_sha256": version["source_snapshot_hash"]},
        "conception_at": version["conception_at"],
        "approval": {"approved_by": approval["approved_by"], "approved_at": approval["approved_at"], "scope": approval["scope"]},
        "execution_mode": "DRY_RUN",
        "constraints": {
            "no_data_on_or_after": HOLDOUT_START.isoformat(),
            "no_backtest_launch": True,
            "no_deployment": True,
            "results_return_path": "canonical QS artifact -> FMP research_runs (research_run_id) -> link_result",
        },
        "code_version": CODE_VERSION,
    }
    body = normalize_payload(body)
    body["artifact_sha256"] = canonical_sha256(body)
    return body


def queue_research(conn, idea_id: str, *, actor: str, dry_run: bool = True, notes: str | None = None) -> dict[str, Any]:
    """Record a DRY-RUN plan (contract) as an idea test. Real execution is a human gate elsewhere."""
    if not dry_run:
        raise ExecutionBlocked("automatic research execution is disabled; export the contract and run it through the existing QC research workflow after human review")
    idea = _idea_row(conn, idea_id)
    if idea["current_state"] != RESEARCH_APPROVED:
        raise IdeaError("idea must be RESEARCH_APPROVED to queue (state {0})".format(idea["current_state"]))
    contract = build_contract(conn, idea_id)
    support = contract["execution_support"]
    status = "DRY_RUN_PLANNED" if support != SUPPORT_UNSUPPORTED else "UNSUPPORTED"
    conn.execute(
        text(
            """
            INSERT INTO mi_research_idea_tests (idea_id, version, spec_hash, strategy_spec_hash, research_run_id, execution_status, artifact_ref, artifact_sha256, created_by, notes)
            VALUES (:i, :v, :h, NULL, NULL, :status, :ref, :sha, :actor, :notes)
            """
        ),
        {"i": idea_id, "v": contract["version"], "h": contract["spec_hash"], "status": status, "ref": "contract:{0}".format(contract["artifact_sha256"]), "sha": contract["artifact_sha256"], "actor": actor, "notes": notes or ("no execution adapter for {0}".format(contract["research_type"]) if status == "UNSUPPORTED" else "dry-run contract emitted; nothing launched")},
    )
    _transition(conn, idea, RESEARCH_QUEUED, actor=actor, actor_kind="HUMAN", reason="dry-run queued ({0})".format(status), version=contract["version"], bound_spec_hash=contract["spec_hash"])
    return {"idea_id": idea_id, "version": contract["version"], "spec_hash": contract["spec_hash"], "state": RESEARCH_QUEUED, "execution_status": status, "contract_sha256": contract["artifact_sha256"], "contract": contract}


def link_result(conn, idea_id: str, *, research_run_id: str, actor: str, artifact_sha256: str | None = None, notes: str | None = None) -> dict[str, Any]:
    """Link canonical results by research_run_id (must already exist in research_runs). No duplication."""
    idea = _idea_row(conn, idea_id)
    if idea["current_state"] != RESEARCH_QUEUED:
        raise IdeaError("results can only be linked to RESEARCH_QUEUED ideas (state {0})".format(idea["current_state"]))
    version = _current_version(conn, idea_id, idea["current_version"])
    run = conn.execute(text("SELECT research_run_id, strategy_id, run_status FROM research_runs WHERE research_run_id = :r"), {"r": research_run_id}).mappings().first()
    if run is None:
        raise IdeaError("research_run_id {0} is not in research_runs; ingest the canonical artifact first".format(research_run_id))
    conn.execute(
        text(
            """
            INSERT INTO mi_research_idea_tests (idea_id, version, spec_hash, strategy_spec_hash, research_run_id, execution_status, artifact_ref, artifact_sha256, created_by, notes)
            VALUES (:i, :v, :h, NULL, :run, :status, :ref, :sha, :actor, :notes)
            """
        ),
        {"i": idea_id, "v": idea["current_version"], "h": version["spec_hash"], "run": research_run_id, "status": "LINKED:{0}".format(run["run_status"] or "UNKNOWN"), "ref": "research_runs:{0}".format(research_run_id), "sha": artifact_sha256, "actor": actor, "notes": notes},
    )
    _transition(conn, idea, RESEARCH_COMPLETE, actor=actor, actor_kind="HUMAN", reason="canonical result linked", version=idea["current_version"], bound_spec_hash=version["spec_hash"])
    _transition(conn, idea, HUMAN_REVIEW, actor=actor, actor_kind="HUMAN", reason="awaiting human review; economic gate is not decided here", version=idea["current_version"], bound_spec_hash=version["spec_hash"])
    return {"idea_id": idea_id, "state": HUMAN_REVIEW, "research_run_id": research_run_id, "strategy_id": run["strategy_id"], "run_status": run["run_status"]}


def set_state(conn, idea_id: str, to_state: str, *, actor: str, actor_kind: str = "HUMAN", reason: str) -> dict[str, Any]:
    """Administrative transitions (ARCHIVED / REJECTED / back to DRAFT) with audit trail."""
    if to_state not in {ARCHIVED, REJECTED, DRAFT}:
        raise IdeaError("use the dedicated operations for {0}".format(to_state))
    idea = _idea_row(conn, idea_id)
    if to_state in {ARCHIVED, REJECTED}:
        conn.execute(text("UPDATE mi_research_idea_approvals SET revoked_at = NOW(), revoke_reason = :r WHERE idea_id = :i AND revoked_at IS NULL"), {"i": idea_id, "r": "idea {0}: {1}".format(to_state.lower(), reason)})
    _transition(conn, idea, to_state, actor=actor, actor_kind=actor_kind, reason=reason, version=idea["current_version"], bound_spec_hash=None)
    return {"idea_id": idea_id, "state": to_state}


def show_idea(conn, idea_id: str) -> dict[str, Any]:
    idea = conn.execute(text("SELECT * FROM mi_research_ideas WHERE idea_id = :i"), {"i": idea_id}).mappings().first()
    if idea is None:
        raise IdeaError("unknown idea {0}".format(idea_id))
    versions = [dict(r) for r in conn.execute(text("SELECT version, spec_hash, research_type, execution_support, source_snapshot_id, source_snapshot_hash, conception_at, created_by, created_at FROM mi_research_idea_versions WHERE idea_id = :i ORDER BY version"), {"i": idea_id}).mappings()]
    transitions = [dict(r) for r in conn.execute(text("SELECT version, from_state, to_state, actor, actor_kind, reason, bound_spec_hash, transitioned_at FROM mi_research_idea_transitions WHERE idea_id = :i ORDER BY transitioned_at, id"), {"i": idea_id}).mappings()]
    approvals = [dict(r) for r in conn.execute(text("SELECT version, spec_hash, scope, approved_by, approved_at, revoked_at, revoke_reason FROM mi_research_idea_approvals WHERE idea_id = :i ORDER BY approved_at"), {"i": idea_id}).mappings()]
    tests = [dict(r) for r in conn.execute(text("SELECT version, spec_hash, research_run_id, execution_status, artifact_ref, artifact_sha256, created_by, created_at, notes FROM mi_research_idea_tests WHERE idea_id = :i ORDER BY created_at, id"), {"i": idea_id}).mappings()]
    return normalize_payload({"idea": dict(idea), "versions": versions, "transitions": transitions, "approvals": approvals, "tests": tests})


def list_ideas(conn) -> list[dict[str, Any]]:
    return [normalize_payload(dict(r)) for r in conn.execute(text("SELECT * FROM mi_v_research_ideas ORDER BY updated_at DESC")).mappings()]


# ---- CLI --------------------------------------------------------------------------------------------

def _read_spec(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise IdeaError("spec file must contain a JSON object")
    return payload


def main(argv: list[str] | None = None, *, engine=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.research_ideas", description="Research idea registry (dry-run only; no execution)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("validate", help="Validate a spec file without writing")
    p.add_argument("--spec", required=True)
    p = sub.add_parser("register")
    p.add_argument("--spec", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--actor-kind", default="HUMAN", choices=["HUMAN", "AI"])
    p.add_argument("--source-snapshot-id", default=None)
    p = sub.add_parser("revise")
    p.add_argument("--idea", required=True)
    p.add_argument("--spec", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--actor-kind", default="HUMAN", choices=["HUMAN", "AI"])
    p.add_argument("--reason", default="spec revised")
    p = sub.add_parser("freeze")
    p.add_argument("--idea", required=True)
    p.add_argument("--actor", required=True)
    p = sub.add_parser("approve", help="Human approval bound to the exact version + spec hash")
    p.add_argument("--idea", required=True)
    p.add_argument("--approved-by", required=True)
    p.add_argument("--version", required=True, type=int)
    p.add_argument("--spec-hash", required=True)
    p = sub.add_parser("revoke")
    p.add_argument("--idea", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("queue", help="Dry-run only: records the contract, launches nothing")
    p.add_argument("--idea", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--execute", action="store_true", help="Refused by policy; present so the refusal is explicit")
    p.add_argument("--contract-out", default=None)
    p = sub.add_parser("link-result")
    p.add_argument("--idea", required=True)
    p.add_argument("--research-run-id", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--artifact-sha256", default=None)
    p = sub.add_parser("set-state")
    p.add_argument("--idea", required=True)
    p.add_argument("--to", required=True, choices=[ARCHIVED, REJECTED, DRAFT])
    p.add_argument("--actor", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("show")
    p.add_argument("--idea", required=True)
    sub.add_parser("list")
    ns = parser.parse_args(argv)

    if ns.command == "validate":
        result = validate_spec(_read_spec(ns.spec))
        print(strict_dumps({"ok": result.ok, "errors": result.errors, "warnings": result.warnings, "spec_hash": result.spec_hash, "execution_support": result.execution_support}, indent=2))
        return 0 if result.ok else 2

    if engine is None:
        from market_intelligence.writer_db import writer_engine

        engine = writer_engine()
    from market_intelligence.locking import EXIT_LOCK_CONTENTION, LockContention, writer_lock

    try:
        with writer_lock(engine):
            with engine.begin() as conn:
                if ns.command == "register":
                    out = register_idea(conn, _read_spec(ns.spec), actor=ns.actor, actor_kind=ns.actor_kind, source_snapshot_id=ns.source_snapshot_id)
                elif ns.command == "revise":
                    out = revise_idea(conn, ns.idea, _read_spec(ns.spec), actor=ns.actor, actor_kind=ns.actor_kind, reason=ns.reason)
                elif ns.command == "freeze":
                    out = freeze_spec(conn, ns.idea, actor=ns.actor)
                elif ns.command == "approve":
                    out = approve_research(conn, ns.idea, approved_by=ns.approved_by, version=ns.version, spec_hash=ns.spec_hash)
                elif ns.command == "revoke":
                    out = revoke_approval(conn, ns.idea, actor=ns.actor, reason=ns.reason)
                elif ns.command == "queue":
                    if ns.execute:
                        raise ExecutionBlocked("--execute is refused: idea research runs only through the human-gated QC workflow")
                    out = queue_research(conn, ns.idea, actor=ns.actor, dry_run=True)
                    if ns.contract_out:
                        with open(ns.contract_out, "w", encoding="utf-8") as fh:
                            fh.write(strict_dumps(out["contract"], indent=2))
                    out = {k: v for k, v in out.items() if k != "contract"}
                elif ns.command == "link-result":
                    out = link_result(conn, ns.idea, research_run_id=ns.research_run_id, actor=ns.actor, artifact_sha256=ns.artifact_sha256)
                elif ns.command == "set-state":
                    out = set_state(conn, ns.idea, ns.to, actor=ns.actor, reason=ns.reason)
                elif ns.command == "show":
                    out = show_idea(conn, ns.idea)
                else:
                    out = {"ideas": list_ideas(conn)}
    except LockContention as exc:
        print(strict_dumps({"status": "LOCK_CONTENTION", "error": str(exc)}))
        return EXIT_LOCK_CONTENTION
    except (IdeaError, ExecutionBlocked) as exc:
        print(strict_dumps({"status": "REJECTED", "error": str(exc)}))
        return 2
    print(strict_dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "ARCHIVED",
    "CONTRACT_SCHEMA_VERSION",
    "DRAFT",
    "ExecutionBlocked",
    "HOLDOUT_START",
    "HUMAN_REVIEW",
    "IdeaError",
    "REJECTED",
    "RESEARCH_APPROVED",
    "RESEARCH_COMPLETE",
    "RESEARCH_QUEUED",
    "RESEARCH_TYPES",
    "SCHEMA_VERSION",
    "SPEC_FROZEN",
    "STATES",
    "TRANSITIONS",
    "active_approval",
    "approve_research",
    "build_contract",
    "freeze_spec",
    "link_result",
    "list_ideas",
    "main",
    "queue_research",
    "register_idea",
    "revise_idea",
    "revoke_approval",
    "set_state",
    "show_idea",
    "validate_spec",
]
