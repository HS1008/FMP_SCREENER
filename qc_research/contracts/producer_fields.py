"""Pinned producer required-field lists for FMP-only CI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SNAPSHOT = Path(__file__).resolve().parent / "producer_required_fields.json"


def load_producer_required() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def required_by_kind() -> dict[str, tuple[str, ...]]:
    payload = load_producer_required()
    raw = payload.get("required_by_kind") or {}
    return {str(kind): tuple(fields) for kind, fields in raw.items()}
