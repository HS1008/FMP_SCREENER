"""Holdout fields stay sticky and official CSFML V1 identity cannot be impersonated."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from qc_research.contracts.kinds import ArtifactContractError, reject_holdout_access
from qc_research.contracts.label_integrity import (
    load_csfml_v1_label_integrity,
    refuse_impersonated_official_csfml_v1,
)
from qc_research.object_store_sync import ArtifactSyncError, ingest_artifact


ROOT = Path(__file__).resolve().parents[1]
PIN = load_csfml_v1_label_integrity()


class _RefuseConn:
    def execute(self, *args, **kwargs):
        raise AssertionError("rejected payload must not reach SQL")


def test_holdout_status_accessed_is_refused():
    with pytest.raises(ArtifactContractError, match="holdout_status"):
        reject_holdout_access(
            {
                "holdout_accessed": False,
                "holdout_status": "ACCESSED",
                "research_run_id": "STAGE2_X",
            }
        )


def test_official_csfml_v1_wrong_sha_is_refused():
    payload = {
        "schema_version": "stage2_ml_v1",
        "research_run_id": PIN["full_suite_run_id"],
        "strategy_id": "CrossSectionalFactorML",
        "run_status": "COMPLETE",
        "holdout_accessed": False,
        "git_commit": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    }
    with pytest.raises(ArtifactContractError, match="non-pin git_commit"):
        refuse_impersonated_official_csfml_v1(payload)
    with pytest.raises(ArtifactSyncError, match="non-pin git_commit"):
        ingest_artifact(_RefuseConn(), key="fake-official", kind="run_summary", payload=payload)


def test_official_csfml_v1_missing_sha_is_refused():
    with pytest.raises(ArtifactContractError, match="requires git_commit"):
        refuse_impersonated_official_csfml_v1(
            {
                "research_run_id": PIN["full_suite_run_id"],
                "strategy_id": "CrossSectionalFactorML",
                "run_status": "COMPLETE",
                "holdout_accessed": False,
            }
        )


def test_official_csfml_v1_window_artifact_without_sha_is_allowed():
    refuse_impersonated_official_csfml_v1(
        {
            "research_run_id": PIN["full_suite_run_id"],
            "strategy_id": "CrossSectionalFactorML",
            "window_id": "2015",
            "holdout_accessed": False,
        }
    )


def test_sealed_official_stage1_and_csfml_payloads_cannot_mutate():
    from jobs.stage1_backtests import RunSummaryImportError, apply_run_summary
    from qc_research.contracts.sealed_results import sealed_results_run_ids

    assert PIN["full_suite_run_id"] in sealed_results_run_ids()
    assert "STAGE1_SPYTrend_c04553d8" in sealed_results_run_ids()
    mutated_stage1 = {
        "research_run_id": "STAGE1_SPYTrend_c04553d8",
        "strategy_id": "SPYTrend",
        "run_status": "COMPLETE",
        "expected_experiment_count": 81,
        "completed_count": 1,
        "failed_count": 0,
        "skipped_count": 0,
        "git_commit": "f04dbfb1a936c753a42a1389d9181f7c22f551a3",
    }
    with pytest.raises(RunSummaryImportError, match="sealed"):
        apply_run_summary(_RefuseConn(), mutated_stage1)
    official_stage1 = json.loads(
        (
            ROOT
            / "stage1_results"
            / "SPYTrend"
            / "STAGE1_SPYTrend_c04553d8"
            / "run_summary.json"
        ).read_text(encoding="utf-8")
    )

    class _RecordingConn:
        def __init__(self):
            self.params = []

        def execute(self, statement, params=None):
            self.params.append(params)

            class _Result:
                def fetchone(self_inner):
                    return None

            return _Result()

    recorded = _RecordingConn()
    apply_run_summary(recorded, official_stage1)
    assert recorded.params[-1]["research_run_id"] == "STAGE1_SPYTrend_c04553d8"
    pin_ok_mutated = dict(official_stage1)
    pin_ok_mutated["config_fingerprint"] = "deadbeefdeadbeef"
    with pytest.raises(RunSummaryImportError, match="sealed"):
        apply_run_summary(_RefuseConn(), pin_ok_mutated)
    official = json.loads(
        (
            ROOT
            / "stage2_results"
            / "CrossSectionalFactorML"
            / PIN["full_suite_run_id"]
            / "2015"
            / "training_summary.json"
        ).read_text(encoding="utf-8")
    )

    class _OkConn:
        def execute(self, statement, params=None):
            return None

    ingest_artifact(
        _OkConn(),
        key="ok-official-2015",
        kind="training_summary",
        payload=official,
        logical_path="stage2_results/CrossSectionalFactorML/{0}/2015/training_summary.json".format(
            PIN["full_suite_run_id"]
        ),
    )
    mutated = dict(official)
    trials = list(mutated.get("candidate_trials") or [])
    if trials:
        first = dict(trials[0])
        first["median_rank_ic"] = 999
        mutated["candidate_trials"] = [first] + trials[1:]
    with pytest.raises(ArtifactSyncError, match="sealed"):
        ingest_artifact(
            _RefuseConn(),
            key="mutated-official-2015",
            kind="training_summary",
            payload=mutated,
            logical_path="stage2_results/CrossSectionalFactorML/{0}/2015/training_summary.json".format(
                PIN["full_suite_run_id"]
            ),
        )


def test_sealed_run_without_committed_tree_refuses_first_ingest():
    from qc_research.contracts.sealed_results import (
        SealedResultsError,
        refuse_sealed_committed_mismatch,
        sealed_results_run_ids,
    )

    assert "STAGE2_CrossSectionalFactorML_e7b24642" in sealed_results_run_ids()
    with pytest.raises(SealedResultsError, match="no committed official tree"):
        refuse_sealed_committed_mismatch(
            {
                "research_run_id": "STAGE2_CrossSectionalFactorML_e7b24642",
                "strategy_id": "CrossSectionalFactorML",
                "holdout_accessed": False,
            }
        )
    with pytest.raises(ArtifactSyncError, match="no committed official tree"):
        ingest_artifact(
            _RefuseConn(),
            key="fake-e7b24642",
            kind="run_summary",
            payload={
                "schema_version": "stage2_ml_v1",
                "research_run_id": "STAGE2_CrossSectionalFactorML_e7b24642",
                "strategy_id": "CrossSectionalFactorML",
                "run_status": "COMPLETE",
                "holdout_accessed": False,
                "git_commit": "e7b246421f24c6e904db4e45878365536e32cdca",
                "economic_gate": "NOT_DEFINED",
            },
        )


def test_official_tlt_wrapped_payloads_match_committed_file():
    from qc_research.contracts.sealed_results import (
        SealedResultsError,
        refuse_sealed_committed_mismatch,
    )
    from qc_research.tlt_duration_momentum import wrap_tlt_duration_momentum_record

    path = ROOT / "qc_research" / "platform_artifacts" / "tlt_duration_momentum.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    wrapped = wrap_tlt_duration_momentum_record(record)
    assert wrapped

    class _OkConn:
        def execute(self, statement, params=None):
            return None

    for kind, artifact in wrapped:
        refuse_sealed_committed_mismatch(artifact, logical_path=str(path))
        ingest_artifact(
            _OkConn(),
            key="ok-tlt-{0}".format(kind),
            kind=kind,
            payload=artifact,
            logical_path=str(path),
        )
    mutated = dict(wrapped[0][1])
    inner = dict(mutated.get("payload") or {})
    inner["economic_gate"] = "PASS"
    mutated["payload"] = inner
    with pytest.raises(SealedResultsError, match="committed official file"):
        refuse_sealed_committed_mismatch(mutated, logical_path=str(path))
    with pytest.raises(ArtifactSyncError, match="committed official file"):
        ingest_artifact(
            _RefuseConn(),
            key="mutated-tlt",
            kind="run_summary",
            payload=mutated,
            logical_path=str(path),
        )


def test_official_csfml_v1_mutated_pin_fields_are_refused():
    base = {
        "research_run_id": PIN["full_suite_run_id"],
        "strategy_id": "CrossSectionalFactorML",
        "git_commit": PIN["authoritative_csfml_v1_qc_sha"],
        "holdout_accessed": False,
        "economic_gate": "NOT_DEFINED",
    }
    with pytest.raises(ArtifactContractError, match="economic_gate"):
        refuse_impersonated_official_csfml_v1({**base, "economic_gate": "PASS"})
    with pytest.raises(ArtifactContractError, match="holdout_accessed"):
        refuse_impersonated_official_csfml_v1({**base, "holdout_accessed": True})
    with pytest.raises(ArtifactContractError, match="holdout_accessed"):
        refuse_impersonated_official_csfml_v1({**base, "holdout_access_count": 1})
    with pytest.raises(ArtifactContractError, match="strategy_id"):
        refuse_impersonated_official_csfml_v1({**base, "strategy_id": "SPYTrend"})
    with pytest.raises(ArtifactContractError, match="holdout_accessed"):
        refuse_impersonated_official_csfml_v1(
            {
                "research_run_id": PIN["full_suite_run_id"],
                "strategy_id": "CrossSectionalFactorML",
                "window_id": "2015",
                "holdout_accessed": True,
            }
        )


def test_official_csfml_v1_pin_shas_are_accepted():
    for sha in (PIN["authoritative_csfml_v1_sha"], PIN["authoritative_csfml_v1_qc_sha"]):
        refuse_impersonated_official_csfml_v1(
            {
                "research_run_id": PIN["full_suite_run_id"],
                "git_commit": sha,
                "holdout_accessed": False,
            }
        )


def test_non_official_csfml_run_is_not_bound_to_v1_sha():
    refuse_impersonated_official_csfml_v1(
        {
            "research_run_id": "STAGE2_CrossSectionalFactorML_FIXTURE01",
            "git_commit": "ffffffffffffffffffffffffffffffffffffffff",
            "holdout_accessed": False,
        }
    )


def test_stage2_sql_holdout_fields_are_monotonic():
    sql = (ROOT / "qc_research" / "ingest" / "stage2_sql.py").read_text(encoding="utf-8")
    platform = (ROOT / "qc_research" / "platform_ingest.py").read_text(encoding="utf-8")
    sync = (ROOT / "jobs" / "sync_quantconnect.py").read_text(encoding="utf-8")
    for source in (sql, platform):
        assert "WHEN UPPER(COALESCE(research_runs.holdout_status, '')) = 'ACCESSED'" in source
        assert "holdout_accessed = COALESCE(research_runs.holdout_accessed, FALSE)" in source
        assert "OR COALESCE(EXCLUDED.holdout_accessed, FALSE)" in source
        assert "holdout_status = COALESCE(EXCLUDED.holdout_status, research_runs.holdout_status)" not in source
    assert "WHEN research_runs.run_status = 'COMPLETE' THEN research_runs.run_status" in sql
    assert "AND COALESCE(run_status, '') <> 'COMPLETE'" in sql
    assert (
        "WHEN research_runs.run_status IN ('COMPLETE', 'RESEARCH_COMPLETE', 'NON_HOLDOUT_COMPLETE')"
        in platform
    )
    assert "run_status = COALESCE(EXCLUDED.run_status, research_runs.run_status)" not in platform
    assert "research_is_holdout = COALESCE(backtests.research_is_holdout, FALSE)" in sync
    assert "OR COALESCE(EXCLUDED.research_is_holdout, FALSE)" in sync


def test_stage2_reingest_cannot_clear_holdout(pg_engine):
    from qc_research.contracts.fixtures import stage2_run_summary
    from qc_research.ingest.stage2_sql import update_run_metadata

    payload = stage2_run_summary()
    run_id = "STAGE2_HOLD_MONOTONIC_TEST"
    payload = dict(payload)
    payload["research_run_id"] = run_id
    payload["run_id"] = run_id
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO research_runs (
                    research_run_id, strategy_id, holdout_accessed, holdout_status, research_kind
                ) VALUES (
                    :run_id, 'CrossSectionalFactorML', TRUE, 'ACCESSED', 'stage2_ml'
                )
                """
            ),
            {"run_id": run_id},
        )
        update_run_metadata(conn, payload)
        row = conn.execute(
            text(
                """
                SELECT holdout_status, holdout_accessed
                FROM research_runs
                WHERE research_run_id = :run_id
                """
            ),
            {"run_id": run_id},
        ).mappings().one()
        assert row["holdout_status"] == "ACCESSED"
        assert row["holdout_accessed"] is True
        conn.execute(text("DELETE FROM research_runs WHERE research_run_id = :run_id"), {"run_id": run_id})


def test_library_sql_excludes_holdout_accessed_flag():
    from qc_research.research_library import LIBRARY_RUNS_SQL

    assert "COALESCE(rr.holdout_accessed, FALSE) IS NOT TRUE" in LIBRARY_RUNS_SQL
    assert "rr.holdout_accessed" in LIBRARY_RUNS_SQL
    assert "rr.git_commit" in LIBRARY_RUNS_SQL
    assert "rr.holdout_access_count" in LIBRARY_RUNS_SQL
    assert "rr.skipped_count" in LIBRARY_RUNS_SQL
