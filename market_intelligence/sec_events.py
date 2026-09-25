"""Deterministic corporate-event labels from SEC form types. No LLM classification."""

from __future__ import annotations

from typing import Any

EVENT_VERSION = "sec_events_v1"

FORM_EVENTS = {
    "8-K": "other",
    "8-K/A": "other",
    "6-K": "other",
    "10-K": "earnings",
    "10-K/A": "earnings",
    "10-Q": "earnings",
    "10-Q/A": "earnings",
    "20-F": "earnings",
    "40-F": "earnings",
    "4": "management",
    "3": "management",
    "5": "management",
    "S-1": "capital_raise",
    "S-3": "capital_raise",
    "S-4": "mna",
    "S-8": "other",
    "424B": "capital_raise",
    "424B2": "capital_raise",
    "424B5": "capital_raise",
    "13D": "mna",
    "13D/A": "mna",
    "13G": "other",
    "13G/A": "other",
    "SC 13D": "mna",
    "SC TO-T": "mna",
    "DEF 14A": "management",
    "DEFA14A": "management",
    "PRE 14A": "management",
    "11-K": "other",
    "N-CSR": "other",
    "N-Q": "other",
}


def event_from_form(form: str | None, *, item_codes: tuple[str, ...] = ()) -> dict[str, Any]:
    raw = str(form or "").strip().upper()
    event_type = FORM_EVENTS.get(raw)
    if event_type is None:
        compact = raw.replace(" ", "")
        for prefix, mapped in (
            ("424B", "capital_raise"),
            ("8-K", "other"),
            ("10-K", "earnings"),
            ("10-Q", "earnings"),
            ("S-", "capital_raise"),
        ):
            if compact.startswith(prefix.replace(" ", "")):
                event_type = mapped
                break
    items = {str(item).strip() for item in item_codes if str(item).strip()}
    if "2.02" in items:
        event_type = "earnings"
    elif "2.01" in items or "1.01" in items:
        event_type = "mna" if "2.01" in items else event_type or "contracts"
    elif "5.02" in items:
        event_type = "management"
    elif "8.01" in items and event_type is None:
        event_type = "other"
    if event_type is None:
        return {"event_type": None, "status": "UNSUPPORTED", "reason": "unmapped_form", "form": raw, "version": EVENT_VERSION}
    return {
        "event_type": event_type,
        "status": "OK",
        "form": raw,
        "item_codes": sorted(items),
        "confidence": "RULE",
        "version": EVENT_VERSION,
        "guidance_or_surprise": None,
        "note": "SEC disclosure is not a complete future earnings calendar or analyst-consensus source.",
    }
