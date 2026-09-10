"""Writer-side research ingest SQL.

Not a Streamlit import path. Strategy Monitor reads through qc_research.read_models.
"""

from qc_research.ingest.stage2_sql import (
    UPSERT_ARTIFACT_SQL,
    UPSERT_FEATURE_SQL,
    UPSERT_MODEL_SQL,
    UPSERT_SIGNAL_SQL,
    UPSERT_TRIAL_SQL,
    mark_run_incomplete,
    update_run_metadata,
    upsert_artifact,
    upsert_features_from_training_summary,
    upsert_model_from_metadata,
    upsert_signals_from_oos,
    upsert_trials_from_training_summary,
)

__all__ = [
    "UPSERT_ARTIFACT_SQL",
    "UPSERT_FEATURE_SQL",
    "UPSERT_MODEL_SQL",
    "UPSERT_SIGNAL_SQL",
    "UPSERT_TRIAL_SQL",
    "mark_run_incomplete",
    "update_run_metadata",
    "upsert_artifact",
    "upsert_features_from_training_summary",
    "upsert_model_from_metadata",
    "upsert_signals_from_oos",
    "upsert_trials_from_training_summary",
]
