"""Research vs promotion vs holdout fields. Mirrored from quant-strategies.

Does not change economic numbers. HUMAN_REVIEW_REQUIRED is a promotion gate.
"""

from __future__ import annotations

from typing import Any, Mapping

COMPLETE = "COMPLETE"
RESEARCH_COMPLETE = "RESEARCH_COMPLETE"
HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
HUMAN_SPEC_REQUIRED = "HUMAN_SPEC_REQUIRED"
DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"
HOLDOUT_STATUS_LOCKED = "LOCKED"
HOLDOUT_STATUS_AUTHORIZED = "AUTHORIZED"
DELIVERY_PENDING = "PENDING"
DELIVERY_DELIVERED = "DELIVERED"
DELIVERY_BLOCKED = "BLOCKED"
DELIVERY_FAILED = "FAILED"


def completed_nonholdout_lifecycle(
    *,
    economic_gate: str = "NOT_DEFINED",
    holdout_locked: bool = True,
    delivery_status: str | None = None,
) -> dict[str, Any]:
    return {
        "research_status": COMPLETE,
        "economic_gate": economic_gate or "NOT_DEFINED",
        "promotion_gate": HUMAN_REVIEW_REQUIRED,
        "holdout_status": HOLDOUT_STATUS_LOCKED if holdout_locked else HOLDOUT_STATUS_AUTHORIZED,
        "delivery_status": delivery_status or DELIVERY_PENDING,
        "state": COMPLETE,
        "holdout_locked": bool(holdout_locked),
    }


def normalize_research_lifecycle(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(payload or {})
    explicit_status = str(raw.get("research_status") or "")
    state = str(raw.get("state") or raw.get("run_status") or raw.get("research_state") or "")
    dry_run = raw.get("dry_run") is True or state == DRY_RUN_COMPLETE
    holdout_locked = raw.get("holdout_locked")
    if holdout_locked is None:
        holdout_locked = raw.get("holdout_accessed") in {None, False, 0, "false"}
    holdout_status = str(raw.get("holdout_status") or "")
    if not holdout_status:
        holdout_status = HOLDOUT_STATUS_LOCKED if holdout_locked else HOLDOUT_STATUS_AUTHORIZED
    economic_gate = str(raw.get("economic_gate") or "NOT_DEFINED")
    promotion = str(raw.get("promotion_gate") or "")
    complete_like = explicit_status in {COMPLETE, RESEARCH_COMPLETE} or state in {
        COMPLETE,
        RESEARCH_COMPLETE,
        "NON_HOLDOUT_COMPLETE",
    }
    if not complete_like and state == HUMAN_REVIEW_REQUIRED and not dry_run:
        windows = raw.get("official_windows") or raw.get("window_count") or (raw.get("aggregate") or {}).get("windows")
        window_count = raw.get("window_count")
        if window_count is None and isinstance(windows, list):
            window_count = len(windows)
        if raw.get("cloud_validated") is True or (window_count and int(window_count) > 0 and holdout_locked):
            complete_like = True
    if dry_run:
        return {
            "research_status": DRY_RUN_COMPLETE,
            "economic_gate": economic_gate,
            "promotion_gate": promotion or HUMAN_REVIEW_REQUIRED,
            "holdout_status": holdout_status,
            "state": DRY_RUN_COMPLETE,
            "holdout_locked": bool(holdout_locked),
        }
    delivery_status = str(raw.get("delivery_status") or "") or None
    if complete_like:
        return completed_nonholdout_lifecycle(
            economic_gate=economic_gate,
            holdout_locked=bool(holdout_locked),
            delivery_status=delivery_status,
        )
    return {
        "research_status": explicit_status or state or "IN_PROGRESS",
        "economic_gate": economic_gate,
        "promotion_gate": promotion or HUMAN_REVIEW_REQUIRED,
        "holdout_status": holdout_status,
        "delivery_status": delivery_status or DELIVERY_PENDING,
        "state": state or explicit_status or "IN_PROGRESS",
        "holdout_locked": bool(holdout_locked),
    }
