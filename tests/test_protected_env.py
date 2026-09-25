"""Preserve-by-default protected env updates; fake keys never printed."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.protected_env import is_placeholder, sanitize_url, upsert_if_placeholder, write_atomic


CANARY = "fake-eia-canary-9f3c2b1a"


def test_placeholder_detection():
    assert is_placeholder("CHANGE_ME")
    assert is_placeholder("")
    assert not is_placeholder(CANARY)


def test_preserve_existing_non_placeholder_without_rotate():
    text = "FRED_API_KEY=already-real-looking-key\nOTHER=keep\n"
    updated, action = upsert_if_placeholder(text, "FRED_API_KEY", CANARY, rotate=False)
    assert action == "preserved"
    assert updated == text
    assert CANARY not in updated


def test_rotate_replaces_explicitly():
    text = "FRED_API_KEY=already-real-looking-key\n"
    updated, action = upsert_if_placeholder(text, "FRED_API_KEY", CANARY, rotate=True)
    assert action == "rotated"
    assert "FRED_API_KEY=" + CANARY in updated


def test_replace_placeholder_and_insert_missing():
    text = "FRED_API_KEY=CHANGE_ME\n"
    updated, action = upsert_if_placeholder(text, "FRED_API_KEY", CANARY)
    assert action == "replaced_placeholder"
    updated, action = upsert_if_placeholder(updated, "EIA_API_KEY", CANARY)
    assert action == "inserted"
    assert "EIA_API_KEY=" + CANARY in updated


def test_rejects_newline_injection_and_unknown_keys():
    with pytest.raises(ValueError):
        upsert_if_placeholder("", "FRED_API_KEY", "abc\nDATABASE_URL=evil")
    with pytest.raises(ValueError):
        upsert_if_placeholder("", "PATH", CANARY)


def test_sanitize_url_redacts_api_key():
    url = "https://api.eia.gov/v2/x/data/?api_key={0}&length=5".format(CANARY)
    redacted = sanitize_url(url, CANARY)
    assert CANARY not in redacted
    assert "api_key=<redacted>" in redacted


def test_atomic_write_does_not_echo_canary(tmp_path: Path, capsys):
    target = tmp_path / "env"
    write_atomic(target, "EIA_API_KEY={0}\n".format(CANARY))
    assert target.read_text(encoding="utf-8").startswith("EIA_API_KEY=")
    out = capsys.readouterr()
    assert CANARY not in out.out
    assert CANARY not in out.err
