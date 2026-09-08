"""Consumer for ``sector_internals_v1`` artifacts (isolated PIT sector producer in quant-strategies).

Backend-only writer. Validates an artifact file that the quant-strategies MarketIntelligenceResearch
producer built locally (synthetic or already-authorized pre-holdout data), then stores the aggregate
rows in canonical PostgreSQL with revision semantics. It never calls QuantConnect, never reads
constituent-level data, and refuses:

* a canonical hash that does not match (``canonical_sha256`` == QS ``hash_payload``);
* a schema/units mismatch, missing definitions, or any row carrying constituent-level keys;
* any decision date on/after the artifact's effective holdout boundary, or a boundary later than
  the platform's ``HOLDOUT_START``; every row is checked (date gate), not just the window header;
* unknown provenance. ``SYNTHETIC_TEST_ONLY`` artifacts are stored but flagged
  ``research_eligible = FALSE`` so the dashboard labels them; they are never research evidence.

Revision semantics: the key is (decision_date, sector, method_version). Re-ingesting the same
artifact is a no-op; a different artifact whose row for a key is byte-identical (row hash) is
unchanged; a differing row becomes revision_seq+1 and the previous row keeps its history with
``is_current = FALSE``. Nothing is deleted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text

from market_intelligence.ideas import HOLDOUT_START
from market_intelligence.nulls import canonical_sha256, normalize_payload, strict_dumps
from market_intelligence.store import RUN_FAILED, RUN_SUCCEEDED, TRANSPORT_FAILED, TRANSPORT_OK, finish_run, record_freshness, start_run

SCHEMA_VERSION = "sector_internals_v1"
SOURCE_ID = "QC_MARKET_INTELLIGENCE"
DATASET = "sector_internals_v1"
CONSUMER_VERSION = "pit_sector_consumer_v1"
REQUIRED_TOP_LEVEL = ("schema_version", "method_version", "producer", "provenance", "boundary", "window", "params", "pit", "definitions", "units", "lineage", "coverage", "rows", "artifact_sha256")
REQUIRED_ROW_KEYS = ("decision_date", "sector", "constituent_count", "priced_count", "return_denominator", "trailing_status", "cw_status", "held_status", "concentration_status")
FORBIDDEN_ROW_KEYS = ("symbols", "members", "prices", "closes", "market_caps", "tickers")
REQUIRED_UNITS = {"pct_above_*": "fraction_0_1", "*_return_*": "simple_return_fraction", "hhi_cap": "sum_of_squared_weights_0_1"}
KNOWN_PROVENANCE = ("REAL_QC", "RECONSTRUCTED", "LOCAL_TEST", "LOCAL_LICENSED", "SYNTHETIC_TEST_ONLY", "UNAVAILABLE", "REAL_HISTORICAL_PRE_2025")
RESEARCH_ELIGIBLE_PROVENANCE = ("REAL_QC", "LOCAL_LICENSED", "REAL_HISTORICAL_PRE_2025")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ArtifactRejected(ValueError):
    """The artifact failed validation; nothing was written."""


@dataclass
class IngestReport:
    artifact_sha256: str
    method_version: str
    provenance: str
    research_eligible: bool
    run_id: str | None = None
    status: str = "VALIDATED"
    artifact_new: bool = False
    rows_received: int = 0
    rows_inserted: int = 0
    rows_revised: int = 0
    rows_unchanged: int = 0
    window_start: str | None = None
    window_end: str | None = None
    effective_holdout_start: str | None = None
    sectors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return normalize_payload({"consumer_version": CONSUMER_VERSION, **self.__dict__})


def _date(value: Any, label: str) -> date:
    text_value = str(value or "")
    if not _DATE_RE.match(text_value):
        raise ArtifactRejected("{0}: {1!r} is not an ISO calendar date".format(label, value))
    try:
        return date.fromisoformat(text_value)
    except ValueError as exc:
        raise ArtifactRejected("{0}: invalid calendar date {1!r}".format(label, value)) from exc


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def verify_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Full consumer-side validation. Returns the artifact dict; raises ArtifactRejected otherwise."""
    if not isinstance(payload, Mapping):
        raise ArtifactRejected("artifact must be a JSON object")
    body = dict(payload)
    missing = [key for key in REQUIRED_TOP_LEVEL if key not in body]
    if missing:
        raise ArtifactRejected("artifact missing keys: {0}".format(", ".join(missing)))
    if body["schema_version"] != SCHEMA_VERSION:
        raise ArtifactRejected("unsupported schema_version {0!r} (expected {1})".format(body["schema_version"], SCHEMA_VERSION))
    expected = str(body["artifact_sha256"] or "")
    computed = canonical_sha256({k: v for k, v in body.items() if k != "artifact_sha256"})
    if not expected or computed != expected:
        raise ArtifactRejected("artifact_sha256 mismatch (declared {0}, computed {1})".format(expected[:12], computed[:12]))
    provenance = str(body["provenance"] or "")
    if provenance not in KNOWN_PROVENANCE:
        raise ArtifactRejected("unknown provenance {0!r}".format(provenance))
    units = body["units"] if isinstance(body["units"], Mapping) else {}
    for key, wanted in REQUIRED_UNITS.items():
        if units.get(key) != wanted:
            raise ArtifactRejected("units[{0}] must be {1!r} (got {2!r})".format(key, wanted, units.get(key)))
    if not isinstance(body["definitions"], Mapping) or not body["definitions"]:
        raise ArtifactRejected("artifact carries no definitions")
    pit = body["pit"] if isinstance(body["pit"], Mapping) else {}
    if pit.get("membership_pit") is not True or pit.get("classification_pit") is not True:
        raise ArtifactRejected("artifact does not declare PIT membership and classification")
    if pit.get("constituent_level_data_included") is not False:
        raise ArtifactRejected("artifact must declare constituent_level_data_included = false")
    boundary_block = body["boundary"] if isinstance(body["boundary"], Mapping) else {}
    boundary = _date(boundary_block.get("effective_holdout_start"), "boundary.effective_holdout_start")
    if boundary > HOLDOUT_START:
        raise ArtifactRejected("boundary {0} is after the platform holdout start {1}".format(boundary, HOLDOUT_START))
    window = body["window"] if isinstance(body["window"], Mapping) else {}
    window_start = _date(window.get("start"), "window.start")
    window_end = _date(window.get("end"), "window.end")
    if window_end < window_start:
        raise ArtifactRejected("window.end precedes window.start")
    if window_end >= boundary:
        raise ArtifactRejected("window.end {0} is on/after the boundary {1}".format(window_end, boundary))
    params = body["params"] if isinstance(body["params"], Mapping) else {}
    if not isinstance(params.get("return_sessions"), int) or params["return_sessions"] <= 0:
        raise ArtifactRejected("params.return_sessions must be a positive integer")
    rows = body["rows"]
    if not isinstance(rows, list) or not rows:
        raise ArtifactRejected("artifact has no rows")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ArtifactRejected("rows[{0}] is not an object".format(index))
        forbidden = [k for k in FORBIDDEN_ROW_KEYS if k in row]
        if forbidden:
            raise ArtifactRejected("rows[{0}] carries constituent-level data: {1}".format(index, ", ".join(forbidden)))
        missing_row = [k for k in REQUIRED_ROW_KEYS if k not in row]
        if missing_row:
            raise ArtifactRejected("rows[{0}] missing {1}".format(index, ", ".join(missing_row)))
        decision = _date(row["decision_date"], "rows[{0}].decision_date".format(index))
        if decision >= boundary:
            raise ArtifactRejected("rows[{0}].decision_date {1} is on/after the boundary {2}".format(index, decision, boundary))
        if not (window_start <= decision <= window_end):
            raise ArtifactRejected("rows[{0}].decision_date {1} is outside the declared window".format(index, decision))
        if not str(row["sector"] or "").strip():
            raise ArtifactRejected("rows[{0}] has an empty sector".format(index))
        for key, value in row.items():
            if key.startswith("pct_above_") and not key.endswith(("_status", "_n", "_denominator")) and value is not None:
                number = _num(value)
                if number is None or not 0.0 <= number <= 1.0:
                    raise ArtifactRejected("rows[{0}].{1}={2!r} is not a fraction in [0, 1]".format(index, key, value))
        if int(row["priced_count"]) > int(row["constituent_count"]):
            raise ArtifactRejected("rows[{0}]: priced_count exceeds constituent_count".format(index))
    return body


def load_artifact(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return verify_artifact(payload)


def _row_hash(row: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(row))


def _pct(row: Mapping[str, Any], window: int) -> float | None:
    return _num(row.get("pct_above_{0}d".format(window)))


def ingest_artifact(conn, artifact: Mapping[str, Any], *, source_ref: str | None = None, parent_run_id: str | None = None) -> IngestReport:
    """Validate then store; idempotent per artifact hash; revision-aware per (date, sector, method)."""
    body = verify_artifact(artifact)
    sha = body["artifact_sha256"]
    method = str(body["method_version"])
    provenance = str(body["provenance"])
    eligible = provenance in RESEARCH_ELIGIBLE_PROVENANCE
    k = int(body["params"]["return_sessions"])
    report = IngestReport(sha, method, provenance, eligible, window_start=body["window"]["start"], window_end=body["window"]["end"], effective_holdout_start=body["boundary"]["effective_holdout_start"], sectors=sorted({str(r["sector"]) for r in body["rows"]}))
    run_id = start_run(conn, source_id=SOURCE_ID, dataset=DATASET, parent_run_id=parent_run_id)
    report.run_id = run_id
    exists = conn.execute(text("SELECT 1 FROM mi_pit_sector_artifacts WHERE artifact_sha256 = :s"), {"s": sha}).scalar()
    if exists:
        report.status = "UNCHANGED"
        report.rows_received = len(body["rows"])
        report.rows_unchanged = len(body["rows"])
    else:
        report.artifact_new = True
        contract = body.get("contract") if isinstance(body.get("contract"), Mapping) else {}
        conn.execute(
            text(
                """
                INSERT INTO mi_pit_sector_artifacts (
                    artifact_sha256, schema_version, method_version, producer, provenance, research_eligible,
                    contract_idea_id, contract_spec_hash, contract_sha256, effective_holdout_start, window_start, window_end,
                    sessions, row_count, params_json, pit_json, lineage_json, coverage_json, definitions_json, units_json,
                    generated_at, ingestion_run_id, source_ref
                ) VALUES (
                    :sha, :schema, :method, :producer, :prov, :eligible,
                    :idea, :spec_hash, :contract_sha, CAST(:boundary AS DATE), CAST(:wstart AS DATE), CAST(:wend AS DATE),
                    :sessions, :rows, CAST(:params AS JSONB), CAST(:pit AS JSONB), CAST(:lineage AS JSONB), CAST(:coverage AS JSONB), CAST(:defs AS JSONB), CAST(:units AS JSONB),
                    :generated, :run_id, :source_ref
                )
                """
            ),
            {
                "sha": sha,
                "schema": body["schema_version"],
                "method": method,
                "producer": str(body.get("producer") or ""),
                "prov": provenance,
                "eligible": eligible,
                "idea": contract.get("idea_id"),
                "spec_hash": contract.get("spec_hash"),
                "contract_sha": contract.get("artifact_sha256"),
                "boundary": body["boundary"]["effective_holdout_start"],
                "wstart": body["window"]["start"],
                "wend": body["window"]["end"],
                "sessions": int(body["window"].get("sessions") or 0),
                "rows": len(body["rows"]),
                "params": strict_dumps(body["params"]),
                "pit": strict_dumps(body["pit"]),
                "lineage": strict_dumps(body["lineage"]),
                "coverage": strict_dumps(body["coverage"]),
                "defs": strict_dumps(body["definitions"]),
                "units": strict_dumps(body["units"]),
                "generated": str(body.get("generated_at") or ""),
                "run_id": run_id,
                "source_ref": source_ref,
            },
        )
        for row in body["rows"]:
            report.rows_received += 1
            row_sha = _row_hash(row)
            current = conn.execute(
                text("SELECT id, revision_seq, row_sha256 FROM mi_pit_sector_internals WHERE decision_date = CAST(:d AS DATE) AND sector = :sec AND method_version = :m AND is_current"),
                {"d": row["decision_date"], "sec": row["sector"], "m": method},
            ).mappings().first()
            if current is not None and current["row_sha256"] == row_sha:
                report.rows_unchanged += 1
                continue
            revision = 1
            if current is not None:
                revision = int(current["revision_seq"]) + 1
                conn.execute(text("UPDATE mi_pit_sector_internals SET is_current = FALSE WHERE id = :id"), {"id": current["id"]})
                report.rows_revised += 1
            else:
                report.rows_inserted += 1
            conn.execute(
                text(
                    """
                    INSERT INTO mi_pit_sector_internals (
                        decision_date, sector, method_version, revision_seq, is_current, artifact_sha256, row_sha256, provenance, research_eligible,
                        return_sessions, constituent_count, priced_count, pct_above_20d, pct_above_50d, pct_above_100d, pct_above_200d,
                        median_return, ew_return, cw_return, cw_status, ew_minus_cw, dispersion, return_denominator,
                        held_ew_return, held_status, hhi_cap, top5_cap_share, concentration_status, row_json, ingestion_run_id
                    ) VALUES (
                        CAST(:d AS DATE), :sec, :m, :rev, TRUE, :sha, :row_sha, :prov, :eligible,
                        :k, :cc, :pc, :p20, :p50, :p100, :p200,
                        :med, :ew, :cw, :cw_status, :ewcw, :disp, :den,
                        :held, :held_status, :hhi, :top5, :conc_status, CAST(:row AS JSONB), :run_id
                    )
                    """
                ),
                {
                    "d": row["decision_date"],
                    "sec": row["sector"],
                    "m": method,
                    "rev": revision,
                    "sha": sha,
                    "row_sha": row_sha,
                    "prov": provenance,
                    "eligible": eligible,
                    "k": k,
                    "cc": int(row["constituent_count"]),
                    "pc": int(row["priced_count"]),
                    "p20": _pct(row, 20),
                    "p50": _pct(row, 50),
                    "p100": _pct(row, 100),
                    "p200": _pct(row, 200),
                    "med": _num(row.get("median_return_{0}d".format(k))),
                    "ew": _num(row.get("ew_return_{0}d".format(k))),
                    "cw": _num(row.get("cw_return_{0}d".format(k))),
                    "cw_status": row.get("cw_status"),
                    "ewcw": _num(row.get("ew_minus_cw_{0}d".format(k))),
                    "disp": _num(row.get("dispersion_{0}d".format(k))),
                    "den": int(row["return_denominator"]),
                    "held": _num(row.get("held_ew_return_{0}d".format(k))),
                    "held_status": row.get("held_status"),
                    "hhi": _num(row.get("hhi_cap")),
                    "top5": _num(row.get("top5_cap_share")),
                    "conc_status": row.get("concentration_status"),
                    "row": strict_dumps(dict(row)),
                    "run_id": run_id,
                },
            )
        report.status = "INGESTED"
    finish_run(
        conn,
        run_id,
        status=RUN_SUCCEEDED,
        counts={"received": report.rows_received, "inserted": report.rows_inserted, "revised": report.rows_revised, "unchanged": report.rows_unchanged},
        details={"artifact_sha256": sha, "method_version": method, "provenance": provenance, "research_eligible": eligible, "status": report.status, "consumer_version": CONSUMER_VERSION},
    )
    record_freshness(conn, source_id=SOURCE_ID, dataset=DATASET, cadence="ON_DEMAND", transport_status=TRANSPORT_OK, latest_observation=date.fromisoformat(body["window"]["end"]), success=True, error_redacted=None, run_id=run_id)
    return report


def record_rejection(conn, *, reason: str, source_ref: str | None = None, parent_run_id: str | None = None) -> str:
    """Record a FAILED consumer run + transport failure for a rejected artifact (nothing else is written)."""
    run_id = start_run(conn, source_id=SOURCE_ID, dataset=DATASET, parent_run_id=parent_run_id)
    finish_run(conn, run_id, status=RUN_FAILED, error_redacted=reason[:200], details={"consumer_version": CONSUMER_VERSION, "source_ref": source_ref})
    record_freshness(conn, source_id=SOURCE_ID, dataset=DATASET, cadence="ON_DEMAND", transport_status=TRANSPORT_FAILED, latest_observation=None, success=False, error_redacted=reason[:200], run_id=run_id)
    return run_id


__all__ = [
    "ArtifactRejected",
    "CONSUMER_VERSION",
    "DATASET",
    "IngestReport",
    "RESEARCH_ELIGIBLE_PROVENANCE",
    "SCHEMA_VERSION",
    "SOURCE_ID",
    "ingest_artifact",
    "load_artifact",
    "record_rejection",
    "verify_artifact",
]
