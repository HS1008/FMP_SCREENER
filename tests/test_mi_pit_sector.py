"""PIT sector internals consumer: validation, canonical ingest, revision semantics, date gate, read model, CLI.

All data here is the SYNTHETIC ``sector_internals_v1`` fixture emitted by the quant-strategies producer
(``research.market_intelligence.sector_internals``) with ``provenance = SYNTHETIC_TEST_ONLY``. Nothing
in this module is research evidence; the tests prove the consumer path, not any hypothesis.
PostgreSQL tests use the disposable ``FMP_TEST_DATABASE_URL`` database (skipped/failed otherwise).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from market_intelligence import pit_sector
from market_intelligence.export_policy import filter_for_export
from market_intelligence.nulls import canonical_sha256, strict_loads
from market_intelligence.pit_sector import ArtifactRejected, ingest_artifact, record_rejection, verify_artifact
from market_intelligence.read_models import pit_sector_context

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "sector_internals_v1_synthetic.json"


def _artifact() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _rehash(body: dict) -> dict:
    body["artifact_sha256"] = canonical_sha256({k: v for k, v in body.items() if k != "artifact_sha256"})
    return body


# ---- pure validation (no DB) ---------------------------------------------------------------------------------

def test_fixture_artifact_verifies_and_is_synthetic_not_research_eligible():
    body = verify_artifact(_artifact())
    assert body["provenance"] == "SYNTHETIC_TEST_ONLY"
    assert "SYNTHETIC_TEST_ONLY" not in pit_sector.RESEARCH_ELIGIBLE_PROVENANCE
    assert body["boundary"]["effective_holdout_start"] == "2020-01-01" < "2025-01-01"
    assert all(r["decision_date"] < "2020-01-01" for r in body["rows"])
    assert body["pit"]["constituent_level_data_included"] is False


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda b: b.__setitem__("artifact_sha256", "0" * 64), "artifact_sha256 mismatch"),
        (lambda b: b["rows"][0].__setitem__("pct_above_50d", 0.51), "artifact_sha256 mismatch"),  # tampered row, stale hash
        (lambda b: b.__setitem__("schema_version", "sector_internals_v0") or _rehash(b), "unsupported schema_version"),
        (lambda b: b["units"].__setitem__("pct_above_*", "percent_0_100") or _rehash(b), "units[pct_above_*]"),
        (lambda b: b.__setitem__("definitions", {}) or _rehash(b), "no definitions"),
        (lambda b: b["rows"][3].__setitem__("symbols", ["AAA", "BBB"]) or _rehash(b), "constituent-level data"),
        (lambda b: b["rows"][3].__setitem__("market_caps", {"AAA": 1.0}) or _rehash(b), "constituent-level data"),
        (lambda b: b["pit"].__setitem__("constituent_level_data_included", True) or _rehash(b), "constituent_level_data_included"),
        (lambda b: b["pit"].__setitem__("membership_pit", False) or _rehash(b), "PIT membership"),
        (lambda b: b["boundary"].__setitem__("effective_holdout_start", "2026-01-01") or _rehash(b), "after the platform holdout start"),
        (lambda b: b["boundary"].__setitem__("effective_holdout_start", "2018-09-01") or _rehash(b), "on/after the boundary"),
        (lambda b: b["rows"][-1].__setitem__("decision_date", "2020-07-01") or b["window"].__setitem__("end", "2020-06-30") or b["boundary"].__setitem__("effective_holdout_start", "2020-07-01") or _rehash(b), "].decision_date 2020-07-01 is on/after the boundary"),
        (lambda b: b["rows"][-1].__setitem__("decision_date", "2019-06-03") or _rehash(b), "outside the declared window"),
        (lambda b: b["rows"][0].__setitem__("decision_date", "18-06-19") or _rehash(b), "not an ISO calendar date"),
        (lambda b: b["rows"][0].__setitem__("pct_above_20d", 1.2) or _rehash(b), "not a fraction in [0, 1]"),
        (lambda b: b["rows"][0].__setitem__("priced_count", 99) or _rehash(b), "priced_count exceeds constituent_count"),
        (lambda b: (b["rows"][0].pop("held_status"), _rehash(b)), "missing held_status"),
        (lambda b: b.__setitem__("provenance", "CURRENT_FMP_UNIVERSE") or _rehash(b), "unknown provenance"),
        (lambda b: b["params"].__setitem__("return_sessions", 0) or _rehash(b), "return_sessions"),
        (lambda b: (b.pop("lineage"), _rehash(b)), "missing keys: lineage"),
        (lambda b: b.__setitem__("rows", []) or _rehash(b), "no rows"),
    ],
)
def test_verify_rejects_tampering_schema_units_forbidden_dates_and_constituent_data(mutate, needle):
    body = _artifact()
    mutate(body)
    with pytest.raises(ArtifactRejected) as excinfo:
        verify_artifact(body)
    assert needle in str(excinfo.value)


def test_verify_requires_an_object_and_never_infers_missing_boundary():
    with pytest.raises(ArtifactRejected):
        verify_artifact([])  # type: ignore[arg-type]
    body = _artifact()
    body["boundary"] = {"platform_holdout_start": "2025-01-01"}
    _rehash(body)
    with pytest.raises(ArtifactRejected) as excinfo:
        verify_artifact(body)
    assert "boundary.effective_holdout_start" in str(excinfo.value)


# ---- PostgreSQL ingest ---------------------------------------------------------------------------------------

def _counts(conn) -> dict:
    return {
        "artifacts": conn.execute(text("SELECT COUNT(*) FROM mi_pit_sector_artifacts")).scalar(),
        "rows": conn.execute(text("SELECT COUNT(*) FROM mi_pit_sector_internals")).scalar(),
        "current": conn.execute(text("SELECT COUNT(*) FROM mi_v_pit_sector_internals_current")).scalar(),
        "latest": conn.execute(text("SELECT COUNT(*) FROM mi_v_pit_sector_internals_latest")).scalar(),
    }


def test_ingest_stores_aggregates_with_provenance_freshness_and_run(mi_db):
    body = _artifact()
    with mi_db.begin() as conn:
        report = ingest_artifact(conn, body, source_ref="fixture.json")
    assert report.status == "INGESTED" and report.artifact_new
    assert (report.rows_received, report.rows_inserted, report.rows_revised, report.rows_unchanged) == (330, 330, 0, 0)
    assert report.research_eligible is False and report.effective_holdout_start == "2020-01-01"
    with mi_db.connect() as conn:
        assert _counts(conn) == {"artifacts": 1, "rows": 330, "current": 330, "latest": 3}
        art = conn.execute(text("SELECT * FROM mi_v_pit_sector_artifacts")).mappings().one()
        assert art["research_eligible"] is False and art["provenance"] == "SYNTHETIC_TEST_ONLY"
        assert art["contract_idea_id"] == body["contract"]["idea_id"] and art["contract_spec_hash"] == body["contract"]["spec_hash"]
        assert str(art["effective_holdout_start"]) == "2020-01-01" and str(art["window_end"]) == "2018-11-19"
        latest = conn.execute(text("SELECT sector, decision_date, revision_seq, pct_above_50d, ew_return, cw_return, held_ew_return, return_sessions FROM mi_v_pit_sector_internals_latest ORDER BY sector")).mappings().all()
        assert [r["sector"] for r in latest] == ["Energy", "Health", "Tech"]
        assert all(str(r["decision_date"]) == "2018-11-19" and r["revision_seq"] == 1 and r["return_sessions"] == 21 for r in latest)
        source_row = next(r for r in body["rows"] if r["sector"] == "Tech" and r["decision_date"] == "2018-11-19")
        tech = next(r for r in latest if r["sector"] == "Tech")
        assert float(tech["pct_above_50d"]) == pytest.approx(source_row["pct_above_50d"])
        assert float(tech["ew_return"]) == pytest.approx(source_row["ew_return_21d"])
        # Held and trailing are distinct quantities carried as distinct columns.
        assert (tech["held_ew_return"] is None) == (source_row["held_ew_return_21d"] is None)
        # NULL stays NULL: no cap-weighted value is fabricated where the producer left it unsupported.
        nulls = conn.execute(text("SELECT COUNT(*) FROM mi_pit_sector_internals WHERE cw_return IS NULL")).scalar()
        assert nulls == sum(1 for r in body["rows"] if r.get("cw_return_21d") is None)
        assert conn.execute(text("SELECT COUNT(*) FROM mi_pit_sector_internals WHERE decision_date >= DATE '2020-01-01'")).scalar() == 0
        run = conn.execute(text("SELECT status, source_id, dataset, details_json FROM mi_ingestion_runs WHERE run_id = :r"), {"r": report.run_id}).mappings().one()
        assert run["status"] == "SUCCEEDED" and run["source_id"] == "QC_MARKET_INTELLIGENCE" and run["dataset"] == "sector_internals_v1"
        details = run["details_json"] if isinstance(run["details_json"], dict) else strict_loads(run["details_json"])
        assert details["research_eligible"] is False and details["status"] == "INGESTED"
        fresh = conn.execute(text("SELECT transport_status, latest_observation_date, expected_cadence FROM mi_data_freshness WHERE source_id='QC_MARKET_INTELLIGENCE' AND dataset='sector_internals_v1'")).mappings().one()
        assert fresh["transport_status"] == "OK" and str(fresh["latest_observation_date"]) == "2018-11-19" and fresh["expected_cadence"] == "ON_DEMAND"


def test_reingesting_the_same_artifact_is_a_noop(mi_db):
    body = _artifact()
    with mi_db.begin() as conn:
        ingest_artifact(conn, body)
    with mi_db.begin() as conn:
        again = ingest_artifact(conn, body)
    assert again.status == "UNCHANGED" and not again.artifact_new
    assert again.rows_unchanged == 330 and again.rows_inserted == 0 and again.rows_revised == 0
    with mi_db.connect() as conn:
        assert _counts(conn) == {"artifacts": 1, "rows": 330, "current": 330, "latest": 3}
        assert conn.execute(text("SELECT COUNT(*) FROM mi_ingestion_runs WHERE dataset='sector_internals_v1' AND status='SUCCEEDED'")).scalar() == 2


def test_revised_artifact_supersedes_only_changed_rows_and_keeps_history(mi_db):
    base = _artifact()
    with mi_db.begin() as conn:
        ingest_artifact(conn, base)
    revised = copy.deepcopy(base)
    target = next(r for r in revised["rows"] if r["sector"] == "Health" and r["decision_date"] == "2018-11-19")
    original_value = target["pct_above_50d"]
    target["pct_above_50d"] = 0.0 if original_value != 0.0 else 1.0
    revised["generated_at"] = "2024-06-01T00:00:00+00:00"
    _rehash(revised)
    assert revised["artifact_sha256"] != base["artifact_sha256"]
    with mi_db.begin() as conn:
        report = ingest_artifact(conn, revised)
    assert report.status == "INGESTED" and report.artifact_new
    assert (report.rows_revised, report.rows_unchanged, report.rows_inserted) == (1, 329, 0)
    with mi_db.connect() as conn:
        assert _counts(conn) == {"artifacts": 2, "rows": 331, "current": 330, "latest": 3}
        revs = conn.execute(text("SELECT revision_seq, is_current, pct_above_50d, artifact_sha256 FROM mi_pit_sector_internals WHERE sector='Health' AND decision_date=DATE '2018-11-19' ORDER BY revision_seq")).mappings().all()
        assert [(r["revision_seq"], r["is_current"]) for r in revs] == [(1, False), (2, True)]
        assert float(revs[0]["pct_above_50d"]) == pytest.approx(original_value) and revs[0]["artifact_sha256"] == base["artifact_sha256"]
        assert float(revs[1]["pct_above_50d"]) == pytest.approx(target["pct_above_50d"]) and revs[1]["artifact_sha256"] == revised["artifact_sha256"]
        latest = conn.execute(text("SELECT pct_above_50d, revision_seq FROM mi_v_pit_sector_internals_latest WHERE sector='Health'")).mappings().one()
        assert latest["revision_seq"] == 2 and float(latest["pct_above_50d"]) == pytest.approx(target["pct_above_50d"])
        # Unique (date, sector, method, revision) index: a duplicate revision cannot be inserted.
        with pytest.raises(Exception):
            with mi_db.begin() as writer:
                writer.execute(text("INSERT INTO mi_pit_sector_internals (decision_date, sector, method_version, revision_seq, is_current, artifact_sha256, row_sha256, provenance, return_sessions, constituent_count, priced_count, row_json) VALUES (DATE '2018-11-19', 'Health', :m, 2, FALSE, :s, 'x', 'SYNTHETIC_TEST_ONLY', 21, 1, 1, '{}'::jsonb)"), {"m": base["method_version"], "s": base["artifact_sha256"]})


def test_rejected_artifact_writes_nothing_but_records_failure_and_keeps_valid_data(mi_db):
    base = _artifact()
    with mi_db.begin() as conn:
        ingest_artifact(conn, base)
    bad = copy.deepcopy(base)
    bad["rows"][0]["pct_above_20d"] = 7.0
    _rehash(bad)
    with pytest.raises(ArtifactRejected):
        with mi_db.begin() as conn:
            ingest_artifact(conn, bad)
    with mi_db.begin() as conn:
        run_id = record_rejection(conn, reason="rows[0].pct_above_20d=7.0 is not a fraction in [0, 1]", source_ref="bad.json")
    with mi_db.connect() as conn:
        assert _counts(conn) == {"artifacts": 1, "rows": 330, "current": 330, "latest": 3}
        run = conn.execute(text("SELECT status, error_redacted FROM mi_ingestion_runs WHERE run_id=:r"), {"r": run_id}).mappings().one()
        assert run["status"] == "FAILED" and "pct_above_20d" in run["error_redacted"]
        fresh = conn.execute(text("SELECT transport_status, latest_observation_date, last_error_redacted FROM mi_data_freshness WHERE source_id='QC_MARKET_INTELLIGENCE' AND dataset='sector_internals_v1'")).mappings().one()
        assert fresh["transport_status"] == "FAILED" and fresh["last_error_redacted"]
        # The last valid observation date survives a failed attempt (freshness keeps the last success).
        assert str(fresh["latest_observation_date"]) == "2018-11-19"


def test_read_model_exposes_latest_history_and_internal_only_scope(mi_db):
    with mi_db.connect() as conn:
        empty = pit_sector_context(conn)
    assert empty["available"] is False and empty["reason"] == "NO_ARTIFACT_INGESTED" and empty["latest"] == []
    with mi_db.begin() as conn:
        ingest_artifact(conn, _artifact())
    with mi_db.connect() as conn:
        ctx = pit_sector_context(conn)
    assert ctx["available"] is True and ctx["export_scope"] == "INTERNAL_ONLY"
    assert [r["sector"] for r in ctx["latest"]] == ["Energy", "Health", "Tech"]
    assert set(ctx["history"]) == {"Energy", "Health", "Tech"}
    dates = [r["decision_date"] for r in ctx["history"]["Tech"]]
    assert dates == sorted(dates) and len(dates) == 110 and dates[-1] == "2018-11-19"
    assert ctx["artifacts"][0]["research_eligible"] is False
    json.dumps(ctx)  # JSON-safe (dates and Decimals normalised)
    exported = filter_for_export({"export_scope": "INTERNAL_ONLY", "pit_sectors": ctx})
    dumped = json.dumps(exported)
    assert "pct_above_50d" not in dumped and "0.6" not in dumped  # value-bearing content does not leave the DB-only surface


def test_read_model_reports_migration_pending_without_views(mi_db, monkeypatch):
    monkeypatch.setattr("market_intelligence.read_models._view_exists", lambda conn, name: False)
    with mi_db.connect() as conn:
        ctx = pit_sector_context(conn)
    assert ctx == {"available": False, "reason": "MIGRATION_PENDING", "latest": [], "history": {}, "artifacts": []}


# ---- CLI job -------------------------------------------------------------------------------------------------

def test_job_validate_only_ingest_and_rejection_exit_codes(mi_db, tmp_path, capsys):
    from jobs.ingest_pit_sector_internals import EXIT_CONFIGURATION, EXIT_REJECTED, run

    assert run(["--artifact", str(tmp_path / "missing.json")], engine=mi_db) == EXIT_CONFIGURATION
    assert run(["--artifact", str(FIXTURE), "--validate-only"], engine=mi_db) == 0
    assert "VALIDATED" in capsys.readouterr().out
    with mi_db.connect() as conn:
        assert _counts(conn)["artifacts"] == 0  # validate-only never writes
    assert run(["--artifact", str(FIXTURE), "--json"], engine=mi_db) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "INGESTED" and payload["rows_inserted"] == 330 and payload["consumer_version"] == pit_sector.CONSUMER_VERSION
    assert run(["--artifact", str(FIXTURE)], engine=mi_db) == 0
    assert "UNCHANGED" in capsys.readouterr().out
    tampered = _artifact()
    tampered["rows"][0]["pct_above_50d"] = 0.123456
    bad_path = tmp_path / "tampered.json"
    bad_path.write_text(json.dumps(tampered), encoding="utf-8")
    assert run(["--artifact", str(bad_path)], engine=mi_db) == EXIT_REJECTED
    assert "artifact_sha256 mismatch" in capsys.readouterr().out
    with mi_db.connect() as conn:
        assert _counts(conn) == {"artifacts": 1, "rows": 330, "current": 330, "latest": 3}
        assert conn.execute(text("SELECT COUNT(*) FROM mi_ingestion_runs WHERE dataset='sector_internals_v1' AND status='FAILED'")).scalar() == 1
        registry = conn.execute(text("SELECT enabled, access_status, dataset FROM mi_source_registry WHERE source_id='QC_MARKET_INTELLIGENCE'")).mappings().one()
        assert registry["enabled"] is True and registry["access_status"] == "CONFIGURED" and registry["dataset"] == "sector_internals_v1"
