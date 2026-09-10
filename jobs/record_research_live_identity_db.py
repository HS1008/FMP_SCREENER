"""Attach sanitized CSFML / TLT live query-back to the latest deploy identity row.

Uses the writer identity in a deploy subshell. Streamlit reads the latest row
only through mi_v_ops_status. Host JSON is an operator sidecar, not a UI source.
Does not call QuantConnect or change systemd. Missing live files stay NULL.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text

from db.connection import STREAMLIT_READONLY_ENV, streamlit_readonly_active
from jobs.record_deploy_identity_db import SecretBearingIdentity, _as_bool, _reject_secrets


DEFAULT_CSFML = "/var/lib/fmp/deploy/csfml_v1_live.json"
DEFAULT_TLT = "/var/lib/fmp/deploy/tlt_v0_live.json"
UPDATE_SQL = """
UPDATE mi_deploy_host_identity AS d
SET
    csfml_v1_live_present = :csfml_v1_live_present,
    csfml_v1_live_identity_ok = :csfml_v1_live_identity_ok,
    csfml_v1_live_blockers = :csfml_v1_live_blockers,
    tlt_v0_live_present = :tlt_v0_live_present,
    tlt_v0_live_identity_ok = :tlt_v0_live_identity_ok,
    tlt_v0_live_blockers = :tlt_v0_live_blockers
FROM (
    SELECT recorded_at
    FROM mi_deploy_host_identity
    ORDER BY recorded_at DESC
    LIMIT 1
) AS latest
WHERE d.recorded_at = latest.recorded_at
"""


def _blockers_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        parts = [value.strip()]
    else:
        parts = [str(item).strip() for item in value if str(item).strip()]
    text_value = ";".join(parts)[:500] or None
    _reject_secrets(text_value)
    return text_value


def load_live_report(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("live identity JSON must be an object")
    _reject_secrets(raw)
    return raw


def live_fields(prefix: str, report: Mapping[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return {
            "{0}_live_present".format(prefix): None,
            "{0}_live_identity_ok".format(prefix): None,
            "{0}_live_blockers".format(prefix): None,
        }
    return {
        "{0}_live_present".format(prefix): _as_bool(report.get("present")),
        "{0}_live_identity_ok".format(prefix): _as_bool(report.get("identity_ok")),
        "{0}_live_blockers".format(prefix): _blockers_text(report.get("blockers")),
    }


def build_record(
    *,
    csfml: Mapping[str, Any] | None,
    tlt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    record = {}
    record.update(live_fields("csfml_v1", csfml))
    record.update(live_fields("tlt_v0", tlt))
    for value in record.values():
        _reject_secrets(value)
    return record


def update_latest(record: Mapping[str, Any], *, engine) -> int:
    if streamlit_readonly_active():
        raise RuntimeError("writer live-identity update is refused in the Streamlit process")
    with engine.begin() as conn:
        result = conn.execute(text(UPDATE_SQL), dict(record))
        return int(result.rowcount or 0)


def print_record(record: Mapping[str, Any]) -> None:
    for key in (
        "csfml_v1_live_present",
        "csfml_v1_live_identity_ok",
        "csfml_v1_live_blockers",
        "tlt_v0_live_present",
        "tlt_v0_live_identity_ok",
        "tlt_v0_live_blockers",
    ):
        print("{0}={1}".format(key, record.get(key)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist sanitized research live identity")
    parser.add_argument("--csfml", default=DEFAULT_CSFML)
    parser.add_argument("--tlt", default=DEFAULT_TLT)
    args = parser.parse_args(argv)
    if streamlit_readonly_active() or (os.environ.get(STREAMLIT_READONLY_ENV) or "").strip():
        print("FAIL: record_research_live_identity_db is not a Streamlit path")
        return 4
    try:
        csfml = load_live_report(Path(args.csfml))
        tlt = load_live_report(Path(args.tlt))
        record = build_record(csfml=csfml, tlt=tlt)
    except SecretBearingIdentity as exc:
        print("FAIL: {0}".format(exc))
        return 4
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("FAIL: {0}".format(exc))
        return 2
    from db.connection import get_engine

    updated = update_latest(record, engine=get_engine())
    if updated < 1:
        print("FAIL: no mi_deploy_host_identity row to attach live query-back")
        return 2
    print("research_live_identity_db_updated={0}".format(updated))
    print_record(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
