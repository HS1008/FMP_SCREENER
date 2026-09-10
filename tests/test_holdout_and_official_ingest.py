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


def test_official_csfml_v1_cannot_claim_zero_historical_impact():
    payload = {
        "schema_version": "stage2_ml_v1",
        "research_run_id": PIN["full_suite_run_id"],
        "strategy_id": "CrossSectionalFactorML",
        "run_status": "COMPLETE",
        "holdout_accessed": False,
        "git_commit": PIN["authoritative_csfml_v1_qc_sha"],
        "historical_v1_impact": "ZERO",
    }
    with pytest.raises(ArtifactContractError, match="historical_v1_impact"):
        refuse_impersonated_official_csfml_v1(payload)


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
            self.sql = []

        def execute(self, statement, params=None):
            self.sql.append(str(statement))
            self.params.append(params)

            class _Result:
                def fetchone(self_inner):
                    return None

            return _Result()

    recorded = _RecordingConn()
    apply_run_summary(recorded, official_stage1)
    assert recorded.params[-1]["research_run_id"] == "STAGE1_SPYTrend_c04553d8"
    assert any("DO NOTHING" in sql and "DO UPDATE" not in sql for sql in recorded.sql)
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


def test_sealed_without_tree_is_explicit_and_covers_e7b24642():
    from qc_research.contracts.sealed_results import (
        load_sealed_results,
        sealed_results_run_ids,
        sealed_without_tree_run_ids,
    )

    data = load_sealed_results()
    trees = set(data.get("committed_trees") or {})
    without = sealed_without_tree_run_ids()
    assert "STAGE2_CrossSectionalFactorML_e7b24642" in without
    missing = sealed_results_run_ids() - trees - without
    assert not missing, missing
    overlap = trees & without
    assert not overlap, overlap


def test_official_sealed_qc_backtest_ids_come_from_committed_trees():
    from qc_research.contracts.sealed_results import official_sealed_qc_backtest_ids
    from qc_research.tlt_duration_momentum import official_tlt_qc_backtest_ids

    ids = official_sealed_qc_backtest_ids()
    tlt = official_tlt_qc_backtest_ids()
    assert tlt <= ids
    assert "7dc2afca65a22195d4845bc4ecb3d465" in ids
    assert "75d7feae6d9c09c1a0b914a0ce2fdbe5" in ids
    assert "047ffb600b710df277e81e5cdb3355e1" in ids
    assert "not-an-official-id" not in ids
    assert "STAGE2_CrossSectionalFactorML_e7b24642" not in ids
    from qc_research.contracts.sealed_results import official_sealed_model_ids

    models = official_sealed_model_ids()
    assert "ridge-2015-67b04ffc3e6c" in models
    assert "not-an-official-model" not in models
    from qc_research.contracts.sealed_results import official_sealed_spec_hashes

    hashes = official_sealed_spec_hashes()
    assert "7684df2e9dff44fa" in hashes
    assert "d8f43c83ddec8d70" in hashes
    assert "ae38eb0e1ff2e078" in hashes
    assert "not-an-official-hash" not in hashes


def test_committed_tree_digests_match_and_refuse_drift():
    from qc_research.contracts.sealed_results import (
        SealedResultsError,
        load_sealed_results,
        verify_committed_tree_digests,
    )

    checked = verify_committed_tree_digests()
    data = load_sealed_results()
    assert set(checked) == set(data["committed_trees"])
    drifted = dict(data)
    drifted["committed_tree_digests"] = dict(data["committed_tree_digests"])
    drifted["committed_tree_digests"]["STAGE1_SPYTrend_c04553d8"] = "0" * 64
    with pytest.raises(SealedResultsError, match="digest mismatch"):
        verify_committed_tree_digests(drifted)


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


def test_identical_sealed_artifact_skips_child_table_writes():
    from qc_research.contracts.hashing import payload_for_hash, sha256_payload
    from qc_research.ingest.stage2_sql import mark_run_incomplete, update_run_metadata

    path = (
        ROOT
        / "stage2_results"
        / "CrossSectionalFactorML"
        / PIN["full_suite_run_id"]
        / "2015"
        / "training_summary.json"
    )
    official = json.loads(path.read_text(encoding="utf-8"))
    sha = sha256_payload(payload_for_hash(official))

    class _StoredConn:
        def __init__(self):
            self.writes: list[str] = []

        def execute(self, statement, params=None):
            sql = str(statement)
            if "SELECT sha256" in sql:

                class _Result:
                    def mappings(self_inner):
                        class _Mappings:
                            def first(self_map):
                                return {"sha256": sha}

                        return _Mappings()

                return _Result()
            self.writes.append(sql)
            return None

    conn = _StoredConn()
    ingest_artifact(
        conn,
        key="sealed-train-2015",
        kind="training_summary",
        payload=official,
        logical_path=str(path),
    )
    joined = "\n".join(conn.writes).lower()
    assert "research_artifacts" in joined
    assert "ml_trials" not in joined
    assert "ml_feature_diagnostics" not in joined

    summary_path = (
        ROOT
        / "stage2_results"
        / "CrossSectionalFactorML"
        / PIN["full_suite_run_id"]
        / "run_summary.json"
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    class _ExistsConn:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "SELECT 1 FROM research_runs" in sql:

                class _Result:
                    def fetchone(self_inner):
                        return (1,)

                return _Result()
            raise AssertionError("must not upsert official research_runs")

    update_run_metadata(_ExistsConn(), summary)
    mark_run_incomplete(_RefuseConn(), PIN["full_suite_run_id"], "reconstruct")


def test_sealed_csfml_run_metadata_insert_once_when_exists_check_misses():
    from qc_research.ingest.stage2_sql import update_run_metadata

    summary_path = (
        ROOT
        / "stage2_results"
        / "CrossSectionalFactorML"
        / PIN["full_suite_run_id"]
        / "run_summary.json"
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    class _Conn:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=None):
            self.calls.append(str(statement))
            return None

    conn = _Conn()
    update_run_metadata(conn, summary)
    insert_sql = [sql for sql in conn.calls if "insert into research_runs" in sql.lower()]
    assert insert_sql
    assert all("DO NOTHING" in sql for sql in insert_sql)
    assert all("DO UPDATE" not in sql for sql in insert_sql)


def test_platform_identity_skips_existing_sealed_run():
    from qc_research.platform_ingest import ingest_platform_payload
    from qc_research.tlt_duration_momentum import wrap_tlt_duration_momentum_record

    path = ROOT / "qc_research" / "platform_artifacts" / "tlt_duration_momentum.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    wrapped = wrap_tlt_duration_momentum_record(record)
    summary = next(payload for kind, payload in wrapped if kind == "run_summary")

    class _ExistsConn:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "SELECT 1 FROM research_runs" in sql:

                class _Result:
                    def fetchone(self_inner):
                        return (1,)

                return _Result()
            raise AssertionError("must not rewrite sealed platform identity")

    ingest_platform_payload(_ExistsConn(), kind="run_summary", payload=summary)


def test_conflict_sql_sealed_is_insert_once():
    from qc_research.ingest.stage2_sql import UPSERT_TRIAL_SQL, conflict_sql
    from qc_research.platform_ingest import UPSERT_EXPERIMENT, UPSERT_TRIAL, _conflict_sql

    sealed_trial = _conflict_sql(UPSERT_TRIAL, sealed=True)
    assert "DO NOTHING" in sealed_trial
    assert "DO UPDATE" not in sealed_trial
    assert "ON CONFLICT (research_run_id, trial_id)" in sealed_trial
    open_trial = _conflict_sql(UPSERT_TRIAL, sealed=False)
    assert open_trial == UPSERT_TRIAL
    assert "DO UPDATE" in open_trial
    sealed_exp = _conflict_sql(UPSERT_EXPERIMENT, sealed=True)
    assert "DO NOTHING" in sealed_exp
    assert "DO UPDATE" not in sealed_exp
    sealed_ml = conflict_sql(UPSERT_TRIAL_SQL, sealed=True)
    assert "DO NOTHING" in sealed_ml
    assert "DO UPDATE" not in sealed_ml
    assert conflict_sql(UPSERT_TRIAL_SQL, sealed=False) == UPSERT_TRIAL_SQL


def test_upsert_signals_seals_official_qc_backtest_id_even_for_unsealed_run():
    from qc_research.ingest.stage2_sql import upsert_signals_from_oos

    captured: list[str] = []

    class _Conn:
        def execute(self, statement, params=None):
            captured.append(str(statement))

    upsert_signals_from_oos(
        _Conn(),
        {
            "research_run_id": "UNSEALED_COPY",
            "backtest_id": "047ffb600b710df277e81e5cdb3355e1",
            "monthly_signal_diagnostics": [{"timestamp": "2015-01-31", "rank_ic": 0.1}],
        },
    )
    assert captured
    assert "DO NOTHING" in captured[0]
    assert "DO UPDATE" not in captured[0]


def test_upsert_model_seals_official_model_id_even_for_unsealed_run():
    from qc_research.ingest.stage2_sql import upsert_model_from_metadata

    captured: list[str] = []

    class _Conn:
        def execute(self, statement, params=None):
            captured.append(str(statement))

    upsert_model_from_metadata(
        _Conn(),
        {
            "run_id": "UNSEALED_COPY",
            "model_id": "ridge-2015-67b04ffc3e6c",
            "outer_window_id": "2015",
        },
    )
    assert captured
    assert "DO NOTHING" in captured[0]
    assert "DO UPDATE" not in captured[0]


def test_official_monitor_strategy_register_is_insert_once():
    from qc_research.platform_ingest import register_platform_monitor_strategy

    captured: list[str] = []

    class _Conn:
        def execute(self, statement, params=None):
            captured.append(str(statement))

    register_platform_monitor_strategy(
        _Conn(),
        {"strategy_id": "SPYTrend", "name": "rewrite-me"},
        sealed=False,
    )
    assert captured
    assert "DO NOTHING" in captured[0]
    captured.clear()
    register_platform_monitor_strategy(
        _Conn(),
        {"strategy_id": "FutureBondTrend", "name": "live"},
        sealed=False,
    )
    assert captured
    assert "DO UPDATE" in captured[0]


def test_strategy_spec_seals_official_fingerprint_even_for_unsealed_run():
    from qc_research.platform_ingest import ingest_platform_payload

    captured: list[str] = []

    class _Conn:
        def execute(self, statement, params=None):
            captured.append(str(statement))

    ingest_platform_payload(
        _Conn(),
        kind="strategy_spec",
        payload={
            "research_run_id": "UNSEALED_COPY",
            "config_fingerprint": "7684df2e9dff44fa",
            "strategy_id": "OtherTrend",
            "identity": {
                "config_fingerprint": "7684df2e9dff44fa",
                "strategy_id": "OtherTrend",
            },
        },
    )
    assert captured
    assert "DO NOTHING" in captured[0]
    assert "DO UPDATE" not in captured[0]
    captured.clear()
    ingest_platform_payload(
        _Conn(),
        kind="strategy_spec",
        payload={
            "research_run_id": "UNSEALED_COPY",
            "config_fingerprint": "deadbeefdeadbeef",
            "strategy_id": "FutureBondTrend",
            "identity": {
                "config_fingerprint": "deadbeefdeadbeef",
                "strategy_id": "FutureBondTrend",
            },
        },
    )
    assert captured
    assert "DO UPDATE" in captured[0]


def test_object_store_get_refused_before_account_read():
    from qc_research.object_store_sync import ObjectStoreClient

    def _fail(_endpoint, _payload):
        raise AssertionError("object_get must not call qc_post")

    with pytest.raises(RuntimeError, match="Object Store get is refused"):
        ObjectStoreClient(_fail).object_get("stage2/model.pkl")


def test_qc_ingest_post_refuses_create_and_object_get():
    from jobs.sync_quantconnect import qc_post

    for endpoint in (
        "/backtests/create",
        "/compile/create",
        "/files/create",
        "/object/set",
        "/object/get",
        "/live/create",
        "/projects/create",
    ):
        with pytest.raises(RuntimeError, match="refused from FMP ingest"):
            qc_post(endpoint, {})


def test_sealed_tlt_children_insert_once_on_conflict():
    from qc_research.platform_ingest import ingest_platform_payload
    from qc_research.tlt_duration_momentum import wrap_tlt_duration_momentum_record

    path = ROOT / "qc_research" / "platform_artifacts" / "tlt_duration_momentum.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    wrapped = wrap_tlt_duration_momentum_record(record)

    class _Conn:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=None):
            self.calls.append((str(statement), params))

            class _Result:
                def fetchone(self_inner):
                    return None

            return _Result()

    conn = _Conn()
    child_kinds = {"trials", "oos_aggregate", "experiment_manifest"}
    for kind, artifact in wrapped:
        if kind in child_kinds:
            ingest_platform_payload(conn, kind=kind, payload=artifact)
    child_sql = [
        sql
        for sql, _ in conn.calls
        if any(
            table in sql.lower()
            for table in ("research_trials", "research_oos_windows", "research_experiments")
        )
    ]
    assert child_sql
    assert all("DO NOTHING" in sql for sql in child_sql)
    assert all("DO UPDATE" not in sql for sql in child_sql)


def test_sealed_csfml_children_insert_once_on_first_ingest():
    path = (
        ROOT
        / "stage2_results"
        / "CrossSectionalFactorML"
        / PIN["full_suite_run_id"]
        / "2015"
        / "training_summary.json"
    )
    official = json.loads(path.read_text(encoding="utf-8"))

    class _Conn:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=None):
            self.calls.append((str(statement), params))
            return None

    conn = _Conn()
    ingest_artifact(
        conn,
        key="first-official-2015",
        kind="training_summary",
        payload=official,
        logical_path=str(path),
    )
    child_sql = [
        sql
        for sql, _ in conn.calls
        if "insert into" in sql.lower()
        and any(table in sql.lower() for table in ("ml_trials", "ml_feature_diagnostics", "research_artifacts"))
    ]
    assert child_sql
    assert any("ml_trials" in sql.lower() for sql in child_sql)
    assert all("DO NOTHING" in sql for sql in child_sql)
    assert all("DO UPDATE" not in sql for sql in child_sql)


def test_sealed_tlt_identity_insert_once_when_exists_check_misses():
    from qc_research.platform_ingest import ingest_platform_payload
    from qc_research.tlt_duration_momentum import wrap_tlt_duration_momentum_record

    path = ROOT / "qc_research" / "platform_artifacts" / "tlt_duration_momentum.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    wrapped = wrap_tlt_duration_momentum_record(record)
    summary = next(payload for kind, payload in wrapped if kind == "run_summary")

    class _Conn:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=None):
            self.calls.append((str(statement), params))
            return None

    conn = _Conn()
    ingest_platform_payload(conn, kind="run_summary", payload=summary)
    identity_sql = [
        sql
        for sql, _ in conn.calls
        if "insert into" in sql.lower()
        and any(table in sql.lower() for table in ("research_runs", "strategies"))
    ]
    assert identity_sql
    assert all("DO NOTHING" in sql for sql in identity_sql)
    assert all("DO UPDATE" not in sql for sql in identity_sql)


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
