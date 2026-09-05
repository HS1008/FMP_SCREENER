"""Idempotent ingest of platform (non-Stage-2-V1) research artifacts.

Does not download Object Store objects. Does not launch QuantConnect.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

from qc_research.object_store_sync import (
    PLATFORM_SCHEMA_VERSIONS,
    canonical_dumps,
    ingest_artifact,
    payload_for_hash,
    sha256_payload,
    validate_artifact,
)


class IngestEnvironmentError(RuntimeError):
    """Live PostgreSQL ingest is blocked until DATABASE_URL / DB_* are set."""


def live_postgres_configured() -> bool:
    if os.environ.get("DATABASE_URL"):
        return True
    return bool(os.environ.get("DB_HOST") and os.environ.get("DB_NAME") and os.environ.get("DB_USER"))


def require_live_postgres_ingest() -> None:
    """Human environment gate. Unit tests may use FakeConn without this."""
    if not live_postgres_configured():
        raise IngestEnvironmentError(
            "DATABASE_URL / DB_* unset. Live Strategy Monitor ingest is a human environment gate. "
            "Do not invent a database. Unit tests may ingest through FakeConn."
        )


UPSERT_TRIAL = """
INSERT INTO research_trials (
    research_run_id, trial_id, model_family, selected, rejected, inner_score, trial_json
) VALUES (
    :research_run_id, :trial_id, :model_family, :selected, :rejected, :inner_score,
    CAST(:trial_json AS JSONB)
)
ON CONFLICT (research_run_id, trial_id) DO UPDATE SET
    model_family = EXCLUDED.model_family,
    selected = EXCLUDED.selected,
    rejected = EXCLUDED.rejected,
    inner_score = EXCLUDED.inner_score,
    trial_json = EXCLUDED.trial_json
"""

UPSERT_PAIR = """
INSERT INTO research_pair_diagnostics (
    research_run_id, pair_left, pair_right, hedge_ratio_method, hedge_ratio,
    correlation, half_life, selection_used_oos, diagnostics_json
) VALUES (
    :research_run_id, :pair_left, :pair_right, :hedge_ratio_method, :hedge_ratio,
    :correlation, :half_life, :selection_used_oos, CAST(:diagnostics_json AS JSONB)
)
ON CONFLICT (research_run_id, pair_left, pair_right) DO UPDATE SET
    hedge_ratio_method = EXCLUDED.hedge_ratio_method,
    hedge_ratio = EXCLUDED.hedge_ratio,
    correlation = EXCLUDED.correlation,
    half_life = EXCLUDED.half_life,
    selection_used_oos = EXCLUDED.selection_used_oos,
    diagnostics_json = EXCLUDED.diagnostics_json
"""

UPSERT_FI = """
INSERT INTO research_fixed_income_metrics (
    research_run_id, metric_name, metric_value, available, source
) VALUES (
    :research_run_id, :metric_name, :metric_value, :available, :source
)
ON CONFLICT (research_run_id, metric_name) DO UPDATE SET
    metric_value = EXCLUDED.metric_value,
    available = EXCLUDED.available,
    source = EXCLUDED.source
"""

UPSERT_SPEC = """
INSERT INTO strategy_specs (
    strategy_spec_hash, strategy_id, strategy_family_id, research_lineage_id,
    research_mode, asset_class, spec_json, git_sha
) VALUES (
    :strategy_spec_hash, :strategy_id, :strategy_family_id, :research_lineage_id,
    :research_mode, :asset_class, CAST(:spec_json AS JSONB), :git_sha
)
ON CONFLICT (strategy_spec_hash) DO UPDATE SET
    spec_json = EXCLUDED.spec_json
"""


def _inner(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else payload


def _strategy_id_from_run(run_id: str) -> str:
    text = str(run_id or "")
    if text.startswith("PLATFORM_"):
        rest = text[len("PLATFORM_") :]
        if "_" in rest:
            head, tail = rest.rsplit("_", 1)
            if tail[:8].isdigit():
                return head
            return rest
        return rest
    return text


def platform_run_identity(payload: dict[str, Any]) -> dict[str, Any]:
    inner = _inner(payload)
    identity = dict(inner.get("identity") or payload.get("identity") or {})
    run_id = str(payload.get("research_run_id") or inner.get("research_run_id") or "")
    strategy_id = str(
        payload.get("strategy_id")
        or inner.get("strategy_id")
        or identity.get("strategy_id")
        or _strategy_id_from_run(run_id)
        or ""
    )
    lineage = str(
        inner.get("research_lineage_id")
        or identity.get("research_lineage_id")
        or payload.get("research_lineage_id")
        or strategy_id
    )
    return {
        "research_run_id": run_id,
        "strategy_id": strategy_id,
        "research_lineage_id": lineage,
        "research_mode": inner.get("research_mode") or payload.get("research_mode") or identity.get("research_mode"),
        "asset_class": inner.get("asset_class") or payload.get("asset_class") or identity.get("asset_class"),
        "strategy_family_id": inner.get("strategy_family_id")
        or payload.get("strategy_family_id")
        or identity.get("strategy_family_id")
        or identity.get("strategy_family"),
        "strategy_spec_hash": inner.get("strategy_spec_hash")
        or inner.get("config_fingerprint")
        or payload.get("config_fingerprint")
        or identity.get("config_fingerprint")
        or identity.get("strategy_spec_hash"),
        "run_status": inner.get("run_status") or payload.get("run_status"),
    }


def ingest_platform_payload(conn, *, kind: str, payload: dict[str, Any]) -> None:
    inner = _inner(payload)
    run_id = str(payload.get("research_run_id") or inner.get("research_run_id") or "")
    if kind == "trials":
        selected = inner.get("selected_trial_id")
        for row in list(inner.get("candidates") or []) + list(inner.get("rejected") or []):
            conn.execute(
                text(UPSERT_TRIAL),
                {
                    "research_run_id": run_id,
                    "trial_id": row.get("trial_id"),
                    "model_family": row.get("model_family"),
                    "selected": row.get("trial_id") == selected,
                    "rejected": bool(row.get("rejected")),
                    "inner_score": row.get("inner_score"),
                    "trial_json": canonical_dumps(row),
                },
            )
    elif kind == "pair_diagnostics":
        pair = inner.get("pair") or []
        if len(pair) >= 2:
            conn.execute(
                text(UPSERT_PAIR),
                {
                    "research_run_id": run_id,
                    "pair_left": pair[0],
                    "pair_right": pair[1],
                    "hedge_ratio_method": inner.get("hedge_ratio_method"),
                    "hedge_ratio": inner.get("hedge_ratio"),
                    "correlation": inner.get("correlation"),
                    "half_life": inner.get("half_life"),
                    "selection_used_oos": bool(inner.get("selection_used_oos")),
                    "diagnostics_json": canonical_dumps(inner),
                },
            )
    elif kind in {"fixed_income_risk", "fixed_income_diagnostics"}:
        for name, value in (inner.get("metrics") or {"gross_dv01": inner.get("gross_dv01")}).items():
            conn.execute(
                text(UPSERT_FI),
                {
                    "research_run_id": run_id,
                    "metric_name": name,
                    "metric_value": value if isinstance(value, (int, float)) else None,
                    "available": value is not None,
                    "source": inner.get("source") or "platform_artifact",
                },
            )
    elif kind == "oos_aggregate":
        windows = inner.get("windows") or []
        for index, window in enumerate(windows):
            conn.execute(
                text(
                    """
                    INSERT INTO research_oos_windows (
                        research_run_id, outer_window_id, oos_start, oos_end, metrics_json
                    ) VALUES (
                        :research_run_id, :outer_window_id, CAST(:oos_start AS DATE),
                        CAST(:oos_end AS DATE), CAST(:metrics_json AS JSONB)
                    )
                    ON CONFLICT (research_run_id, outer_window_id) DO UPDATE SET
                        oos_start = EXCLUDED.oos_start,
                        oos_end = EXCLUDED.oos_end,
                        metrics_json = EXCLUDED.metrics_json
                    """
                ),
                {
                    "research_run_id": run_id,
                    "outer_window_id": str(window.get("window_id") or window.get("kind") or index),
                    "oos_start": window.get("start") or window.get("oos_start"),
                    "oos_end": window.get("end") or window.get("oos_end"),
                    "metrics_json": canonical_dumps(window),
                },
            )
    elif kind == "experiment_manifest":
        experiments = inner.get("experiments") or []
        if not experiments:
            experiments = [inner.get("experiment_id") or "platform"]
        for index, item in enumerate(experiments):
            experiment_id = item if isinstance(item, str) else str((item or {}).get("experiment_id") or index)
            conn.execute(
                text(UPSERT_EXPERIMENT),
                {
                    "research_run_id": run_id,
                    "experiment_id": experiment_id,
                    "metadata_json": canonical_dumps(item if isinstance(item, dict) else inner),
                },
            )
    elif kind == "strategy_spec":
        spec = inner if inner.get("identity") else payload
        identity = spec.get("identity") or {}
        conn.execute(
            text(UPSERT_SPEC),
            {
                "strategy_spec_hash": identity.get("config_fingerprint") or payload.get("config_fingerprint") or "",
                "strategy_id": identity.get("strategy_id") or payload.get("strategy_id") or "",
                "strategy_family_id": identity.get("strategy_family_id"),
                "research_lineage_id": identity.get("research_lineage_id"),
                "research_mode": identity.get("research_mode"),
                "asset_class": identity.get("asset_class"),
                "spec_json": canonical_dumps(spec),
                "git_sha": identity.get("git_sha"),
            },
        )
    if kind in {"run_summary", "run_manifest"} and run_id:
        identity = platform_run_identity(payload)
        if identity["strategy_id"]:
            conn.execute(text(UPSERT_PLATFORM_RUN), identity)


SKIP_NO_DATABASE = (
    "DATABASE_URL / DB_* unset. Live Strategy Monitor ingest skipped. "
    "This is an environment gate, not a code failure. Do not invent credentials."
)
DEFAULT_ARTIFACT_ROOT = Path(__file__).resolve().parents[1] / "qc_research" / "platform_artifacts"
SMOKE_FAMILY_HINTS = {
    "ml_discovery",
    "ml_treasury_futures",
    "ml_cloud_train",
    "ml_cloud_train_futures",
    "ml_ridge_transport",
    "cross_sectional_pit",
    "manual_equity",
    "pairs",
    "treasury_futures",
}

UPSERT_EXPERIMENT = """
INSERT INTO research_experiments (
    research_run_id, experiment_id, metadata_json
) VALUES (
    :research_run_id, :experiment_id, CAST(:metadata_json AS JSONB)
)
ON CONFLICT (research_run_id, experiment_id) DO UPDATE SET
    metadata_json = EXCLUDED.metadata_json
"""

UPSERT_PLATFORM_RUN = """
INSERT INTO research_runs (
    research_run_id,
    strategy_id,
    research_kind,
    research_mode,
    asset_class,
    strategy_family_id,
    strategy_spec_hash,
    research_lineage_id,
    run_status,
    holdout_accessed,
    holdout_access_count
) VALUES (
    :research_run_id,
    :strategy_id,
    'platform_research',
    :research_mode,
    :asset_class,
    :strategy_family_id,
    :strategy_spec_hash,
    :research_lineage_id,
    :run_status,
    FALSE,
    0
)
ON CONFLICT (research_run_id) DO UPDATE SET
    last_seen_at = NOW(),
    strategy_id = COALESCE(NULLIF(EXCLUDED.strategy_id, ''), research_runs.strategy_id),
    research_kind = 'platform_research',
    research_mode = COALESCE(EXCLUDED.research_mode, research_runs.research_mode),
    asset_class = COALESCE(EXCLUDED.asset_class, research_runs.asset_class),
    strategy_family_id = COALESCE(EXCLUDED.strategy_family_id, research_runs.strategy_family_id),
    strategy_spec_hash = COALESCE(EXCLUDED.strategy_spec_hash, research_runs.strategy_spec_hash),
    research_lineage_id = COALESCE(EXCLUDED.research_lineage_id, research_runs.research_lineage_id),
    run_status = COALESCE(EXCLUDED.run_status, research_runs.run_status)
"""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def postgres_url_from_env() -> str | None:
    url = os.environ.get("DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            return "postgresql+psycopg2://" + url[len("postgres://") :]
        if url.startswith("postgresql://") and "+psycopg2" not in url:
            return "postgresql+psycopg2://" + url[len("postgresql://") :]
        return url
    if os.environ.get("DB_HOST") and os.environ.get("DB_NAME") and os.environ.get("DB_USER"):
        return (
            "postgresql+psycopg2://{0}:{1}@{2}:{3}/{4}".format(
                os.environ.get("DB_USER") or "",
                os.environ.get("DB_PASSWORD") or "",
                os.environ.get("DB_HOST"),
                os.environ.get("DB_PORT") or "5432",
                os.environ.get("DB_NAME"),
            )
        )
    return None


def postgres_engine():
    """Live engine only after the environment gate. Never invents a URL."""
    require_live_postgres_ingest()
    url = postgres_url_from_env()
    if not url:
        raise IngestEnvironmentError(SKIP_NO_DATABASE)
    from sqlalchemy import create_engine

    return create_engine(url, pool_pre_ping=True)


def is_platform_artifact(payload: dict[str, Any]) -> bool:
    return str(payload.get("schema_version") or "") in PLATFORM_SCHEMA_VERSIONS


def is_smoke_record(payload: dict[str, Any]) -> bool:
    if is_platform_artifact(payload):
        return False
    if payload.get("winner_backtest_id") or payload.get("baseline_backtest_id"):
        return True
    return str(payload.get("family") or "") in SMOKE_FAMILY_HINTS


def _hashed_envelope(kind: str, run_id: str, inner: dict[str, Any], **meta: Any) -> dict[str, Any]:
    body = {
        "schema_version": "platform_artifact_v1",
        "kind": kind,
        "research_run_id": run_id,
        "strategy_id": meta.get("strategy_id") or inner.get("strategy_id"),
        "provenance": meta.get("provenance") or inner.get("provenance") or "REAL_QC",
        "research_mode": meta.get("research_mode") or inner.get("research_mode"),
        "run_status": inner.get("run_status"),
        "payload": dict(inner),
    }
    body["payload"].setdefault("research_run_id", run_id)
    body["payload"].setdefault("provenance", body["provenance"])
    body["artifact_sha256"] = sha256_payload(payload_for_hash(body))
    return body


def wrap_smoke_record(record: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Turn a platform smoke runner JSON into hashed Monitor artifacts."""
    run_id = str(record.get("run_id") or record.get("research_run_id") or "")
    if not run_id:
        raise ValueError("smoke record is missing run_id")
    strategy_id = str(record.get("strategy_id") or run_id)
    provenance = str(record.get("provenance") or "REAL_QC")
    metrics = dict(record.get("metrics") or {})
    baseline_metrics = dict(record.get("baseline_metrics") or {})
    start = record.get("start_date") or "2019-01-02"
    end = record.get("end_date") or "2019-06-28"
    summary = {
        "research_run_id": run_id,
        "run_status": record.get("cloud_state") or record.get("state") or "CLOUD_VALIDATED",
        "research_mode": record.get("research_mode") or "ML_DISCOVERY",
        "asset_class": record.get("asset_class")
        or (
            "TREASURY_FUTURE"
            if str(record.get("family") or "") in {"ml_cloud_train_futures", "ml_treasury_futures", "treasury_futures"}
            else "ETF"
            if str(record.get("family") or "")
            in {"ml_discovery", "ml_cloud_train", "ml_ridge_transport", "cross_sectional_pit", "manual_equity"}
            else None
        ),
        "strategy_family_id": record.get("strategy_family_id") or record.get("research_lineage_id"),
        "research_lineage_id": record.get("research_lineage_id"),
        "research_state": record.get("cloud_state") or record.get("state"),
        "trial_count": record.get("trial_count"),
        "model_family": record.get("model_family"),
        "selected_candidate": record.get("selected_candidate"),
        "baseline_trial_id": record.get("baseline_trial_id"),
        "search_space_hash": record.get("search_space_hash"),
        "feature_schema_hash": record.get("feature_schema_hash"),
        "history_provider": record.get("history_provider") or "qc_cloud",
        "training_layer": record.get("training_layer") or "qc_cloud",
        "data_read_used": bool(record.get("data_read_used")),
        "object_store_key": record.get("object_store_key"),
        "train_backtest_id": record.get("train_backtest_id"),
        "winner_backtest_id": record.get("winner_backtest_id") or record.get("backtest_id"),
        "baseline_backtest_id": record.get("baseline_backtest_id"),
        "economic_gate": record.get("economic_gate") or "NOT_DEFINED",
        "contract_hash": record.get("contract_hash"),
        "cost_model_id": record.get("cost_model_id"),
        "intercept_only": (record.get("fitted_model") or {}).get("intercept_only"),
        "fitted_model": record.get("fitted_model") or {},
        "exported_binary": bool(record.get("exported_binary")),
        "provenance": provenance,
        "observation_provenance": record.get("observation_provenance"),
        "ci_run_id": record.get("ci_run_id"),
        "ci_commit": record.get("ci_commit"),
        "note": record.get("note"),
    }
    oos = {
        "research_run_id": run_id,
        "windows": [
            {"kind": "winner", "start": start, "end": end, **metrics},
            {"kind": "baseline", "start": start, "end": end, **baseline_metrics},
        ],
        "sharpe_ratio": metrics.get("sharpe_ratio"),
        "baseline_sharpe_ratio": baseline_metrics.get("sharpe_ratio"),
        "identical_oos_windows": bool(record.get("identical_oos_windows", True)),
        "provenance": provenance,
    }
    return [
        (
            "run_summary",
            _hashed_envelope(
                "run_summary",
                run_id,
                summary,
                strategy_id=strategy_id,
                provenance=provenance,
                research_mode=summary["research_mode"],
            ),
        ),
        (
            "oos_aggregate",
            _hashed_envelope("oos_aggregate", run_id, oos, strategy_id=strategy_id, provenance=provenance),
        ),
    ]


def discover_platform_files(root: Path | None = None) -> list[Path]:
    base = Path(root) if root is not None else DEFAULT_ARTIFACT_ROOT
    if base.is_file() and base.suffix == ".json":
        return [base]
    if not base.is_dir():
        return []
    return sorted(path for path in base.rglob("*.json") if path.is_file() and path.name != "README.md")


def load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("{0} is not a JSON object".format(path))
    return payload


def normalize_platform_file(path: Path) -> list[tuple[str, dict[str, Any]]]:
    payload = load_json_object(path)
    if is_platform_artifact(payload):
        kind = str(payload.get("kind") or path.stem)
        validate_artifact(kind, payload)
        return [(kind, payload)]
    if is_smoke_record(payload):
        wrapped = wrap_smoke_record(payload)
        for kind, artifact in wrapped:
            validate_artifact(kind, artifact)
        return wrapped
    raise ValueError("{0} is neither a platform_artifact_v1 nor a proven smoke record".format(path))


def ingest_platform_files(conn, paths: Iterable[Path], *, root: Path | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {"ingested": 0, "skipped": 0, "errors": [], "artifacts": []}
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        try:
            items = normalize_platform_file(path)
        except Exception as exc:
            summary["errors"].append("{0}: {1}".format(path, exc))
            continue
        for kind, artifact in items:
            run_id = str(artifact.get("research_run_id") or "")
            key = "platform_research/{0}/{1}".format(run_id, kind)
            if key in seen:
                summary["skipped"] += 1
                continue
            seen.add(key)
            ingest_artifact(
                conn,
                key=key,
                kind=kind,
                payload=artifact,
                expected_hash=artifact.get("artifact_sha256"),
                transport="platform_research_files",
                logical_path=str(path),
            )
            summary["ingested"] += 1
            summary["artifacts"].append({"key": key, "kind": kind, "research_run_id": run_id})
    return summary


def monitor_view_from_artifacts(artifacts: list[tuple[str, dict[str, Any]]]) -> dict[str, Any] | None:
    from qc_research.ml_monitor_ui import build_platform_monitor_view

    by_kind = {kind: payload for kind, payload in artifacts}
    summary = by_kind.get("run_summary")
    if summary is None:
        return None
    inner = summary.get("payload") if isinstance(summary.get("payload"), dict) else summary
    strategy_id = str(summary.get("strategy_id") or inner.get("strategy_id") or "")
    run_id = str(summary.get("research_run_id") or inner.get("research_run_id") or "")
    return build_platform_monitor_view(
        strategy_id=strategy_id,
        selected_run=run_id,
        run_summary=summary,
        oos=by_kind.get("oos_aggregate"),
        model_metadata=by_kind.get("model_metadata"),
    )


def verify_monitor_view(view: dict[str, Any] | None) -> dict[str, Any]:
    if not view:
        raise ValueError("Strategy Monitor view is empty after ingest")
    provenance = str(view.get("provenance_kind") or view.get("provenance") or "")
    if provenance != "REAL_QC":
        raise ValueError("Strategy Monitor provenance is {0}, expected REAL_QC".format(provenance))
    if view.get("economic_pass") is True:
        raise ValueError("licensed infrastructure evidence must not be treated as economic PASS")
    return view
