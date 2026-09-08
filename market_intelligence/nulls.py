"""Boundary normalization: provider missing tokens, NaN/NaT/Infinity -> NULL; strict JSON; hashing.

Hash convention is shared with ``qc_research.object_store_sync``: SHA-256 of canonical
JSON (sorted keys, compact separators) of the body excluding only the top-level
``artifact_sha256`` key. List order is preserved. Bodies are normalized first so a
strict JSON round trip is stable.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from qc_research.object_store_sync import canonical_dumps, payload_for_hash, sha256_payload

MISSING_TOKENS: frozenset[str] = frozenset(
    {"", ".", "na", "n/a", "nan", "null", "none", "-", "--", "#n/a", "nat", "missing"}
)


class MalformedValueError(ValueError):
    """A provider value is neither numeric nor a recognised missing token."""


def is_missing_token(raw: Any) -> bool:
    if raw is None:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() in MISSING_TOKENS
    return False


def normalize_numeric(raw: Any) -> tuple[Decimal | None, str | None]:
    """Return ``(value, missing_reason)``.

    ``value`` is a :class:`Decimal` (exact provider text preserved) or ``None``.
    Zero is a valid value. Non-finite floats and missing tokens become ``None`` with a
    reason. Non-numeric text raises :class:`MalformedValueError`.
    """
    if raw is None:
        return None, "missing"
    if isinstance(raw, bool):
        raise MalformedValueError("boolean is not a numeric observation")
    if isinstance(raw, Decimal):
        if not raw.is_finite():
            return None, "non_finite"
        return raw, None
    if isinstance(raw, int):
        return Decimal(raw), None
    if isinstance(raw, float):
        if math.isnan(raw) or math.isinf(raw):
            return None, "non_finite"
        return Decimal(repr(raw)), None
    if hasattr(raw, "item") and not isinstance(raw, str):  # numpy scalar
        try:
            return normalize_numeric(raw.item())
        except (AttributeError, TypeError, ValueError):
            pass
    if isinstance(raw, str):
        text = raw.strip()
        if text.lower() in MISSING_TOKENS:
            return None, "missing_token:{0}".format(text or "<empty>")
        try:
            value = Decimal(text.replace(",", ""))
        except InvalidOperation as exc:
            raise MalformedValueError("non-numeric value {0!r}".format(raw)) from exc
        if not value.is_finite():
            return None, "non_finite"
        return value, None
    raise MalformedValueError("unsupported value type {0}".format(type(raw).__name__))


def _pd():
    try:
        import pandas as pd  # noqa: WPS433 (optional dependency at runtime)
    except ImportError:  # pragma: no cover
        return None
    return pd


def normalize_scalar(value: Any) -> Any:
    """Convert a scalar to strict-JSON-safe Python; NaN/NaT/inf -> ``None``."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        as_float = float(value)
        return int(value) if value == value.to_integral_value() and abs(as_float) < 1e15 else as_float
    pd = _pd()
    if pd is not None:
        # pd.NaT subclasses datetime, so it must be checked before the datetime branch.
        if value is pd.NaT:
            return None
        if isinstance(value, pd.Timestamp):
            if pd.isna(value):
                return None
            return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item") and callable(value.item):  # numpy scalar
        try:
            return normalize_scalar(value.item())
        except (TypeError, ValueError):
            return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def normalize_payload(obj: Any) -> Any:
    """Recursively normalize dicts/lists/scalars for strict JSON (no NaN/Infinity)."""
    if isinstance(obj, dict):
        return {str(key): normalize_payload(val) for key, val in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [normalize_payload(item) for item in obj]
    if isinstance(obj, (set, frozenset)):
        return [normalize_payload(item) for item in sorted(obj, key=str)]
    pd = _pd()
    if pd is not None and isinstance(obj, pd.DataFrame):
        return [normalize_payload(row) for row in obj.to_dict(orient="records")]
    if pd is not None and isinstance(obj, pd.Series):
        return normalize_payload(obj.to_dict())
    return normalize_scalar(obj)


def strict_dumps(obj: Any, *, indent: int | None = None) -> str:
    """Strict JSON: sorted keys, no NaN/Infinity (raises if any slipped through)."""
    return json.dumps(
        normalize_payload(obj),
        sort_keys=True,
        allow_nan=False,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )


def strict_loads(text: str) -> Any:
    def _reject(token: str) -> Any:
        raise ValueError("non-strict JSON token {0!r}".format(token))

    return json.loads(text, parse_constant=_reject)


def canonical_body(payload: dict[str, Any]) -> dict[str, Any]:
    return normalize_payload(payload_for_hash(payload))


def canonical_sha256(payload: dict[str, Any]) -> str:
    """Hash the normalized body excluding only top-level ``artifact_sha256``."""
    return sha256_payload(canonical_body(payload))


def with_artifact_hash(payload: dict[str, Any]) -> dict[str, Any]:
    body = canonical_body(payload)
    body["artifact_sha256"] = sha256_payload(body)
    return body


def verify_artifact_hash(payload: dict[str, Any]) -> tuple[bool, str]:
    expected = str(payload.get("artifact_sha256") or "").strip()
    actual = canonical_sha256(payload)
    return (bool(expected) and expected == actual), actual


__all__ = [
    "MISSING_TOKENS",
    "MalformedValueError",
    "canonical_body",
    "canonical_dumps",
    "canonical_sha256",
    "is_missing_token",
    "normalize_numeric",
    "normalize_payload",
    "normalize_scalar",
    "strict_dumps",
    "strict_loads",
    "verify_artifact_hash",
    "with_artifact_hash",
]
