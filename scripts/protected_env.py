"""Atomic env-file updates that preserve existing non-placeholder secrets by default.

Never prints values. Intended for host provisioning scripts and unit tests with fake keys.
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile

PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "CHANGE_ME",
        "CHANGEME",
        "PLACEHOLDER",
        "REPLACE_ME",
        "TODO",
        "YOUR_KEY_HERE",
        "XXX",
    }
)
ALLOWED_SECRET_KEYS = frozenset(
    {
        "FRED_API_KEY",
        "EIA_API_KEY",
        "OPENFIGI_API_KEY",
        "SEC_USER_AGENT",
        "AI_CONTEXT_API_TOKEN",
        "DATABASE_READONLY_URL",
        "DASHBOARD_READONLY_URL",
        "MARKET_INTELLIGENCE_DATABASE_URL",
    }
)
# API keys must be single tokens; SEC_USER_AGENT must allow spaces (org + email).
SPACE_ALLOWED_KEYS = frozenset({"SEC_USER_AGENT"})
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
VALUE_RE = re.compile(r"^[^\r\n\x00]*$")


def valid_key(key: str) -> bool:
    return bool(KEY_RE.fullmatch(key)) and key in ALLOWED_SECRET_KEYS


def is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    stripped = value.strip().strip('"').strip("'")
    return stripped.upper() in PLACEHOLDER_VALUES


def parse_assignment(text: str, key: str) -> str | None:
    pattern = re.compile(r"^(?!#)\s*{0}=(.*)$".format(re.escape(key)), re.M)
    match = pattern.search(text)
    if not match:
        return None
    raw = match.group(1).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return raw[1:-1]
    return raw


def sanitize_url(url: str, secret: str | None) -> str:
    redacted = url
    if secret:
        redacted = redacted.replace(secret, "<redacted>")
    redacted = re.sub(r"(api_key=)[^&]+", r"\1<redacted>", redacted, flags=re.I)
    redacted = re.sub(r"(token=)[^&]+", r"\1<redacted>", redacted, flags=re.I)
    return redacted


def _format_assignment(key: str, value: str) -> str:
    if key in SPACE_ALLOWED_KEYS or any(ch.isspace() for ch in value) or any(ch in value for ch in "#'\""):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return '{0}="{1}"'.format(key, escaped)
    return "{0}={1}".format(key, value)


def upsert_if_placeholder(text: str, key: str, value: str, *, rotate: bool = False) -> tuple[str, str]:
    if not valid_key(key):
        raise ValueError("key not allowed")
    if not VALUE_RE.fullmatch(value):
        raise ValueError("value contains disallowed characters")
    if not value.strip():
        raise ValueError("value empty")
    if key not in SPACE_ALLOWED_KEYS and any(ch.isspace() for ch in value):
        raise ValueError("value empty or contains whitespace")
    current = parse_assignment(text, key)
    if current is not None and not is_placeholder(current) and not rotate:
        return text, "preserved"
    pattern = re.compile(r"^#?\s*{0}=.*$".format(re.escape(key)), re.M)
    replacement = _format_assignment(key, value)
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1), ("rotated" if rotate and current and not is_placeholder(current) else "replaced_placeholder")
    if text and not text.endswith("\n"):
        text += "\n"
    return text + replacement + "\n", "inserted"


def write_atomic(path: pathlib.Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
