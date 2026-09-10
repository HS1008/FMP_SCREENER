"""Read-only SQL loaders for Strategy Monitor.

Does not write. Does not train. Does not call QuantConnect.
"""

from qc_research.read_models.monitor_queries import (
    PLATFORM_RUN_IDS_SQL,
    as_payload,
    load_platform_run_ids,
    load_stage2_artifact_payload,
    load_stage2_feature_diagnostics,
    load_stage2_models,
    load_stage2_run_ids,
    load_stage2_signal_points,
    load_stage2_trials,
    read_sql,
)

__all__ = [
    "PLATFORM_RUN_IDS_SQL",
    "as_payload",
    "load_platform_run_ids",
    "load_stage2_artifact_payload",
    "load_stage2_feature_diagnostics",
    "load_stage2_models",
    "load_stage2_run_ids",
    "load_stage2_signal_points",
    "load_stage2_trials",
    "read_sql",
]
