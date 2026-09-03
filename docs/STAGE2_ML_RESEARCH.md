# Stage 2 ML Research (FMP_SCREENER)

Phase 1 adds the persistence and monitor contracts for Stage 2 ML research.
It does **not** train models and it does **not** launch QuantConnect jobs.

```
QuantConnect research project
        ↓
QuantConnect Object Store
        ↓
jobs.sync_quantconnect --backtests-only
  (existing Stage 1 backtest sync, then Stage 2 artifact sync)
        ↓
PostgreSQL
        ↓
Streamlit Strategy Monitor (read-only fragment)
```

Backend synchronization cadence and frontend refresh cadence stay independent.
The Strategy Monitor still uses a 30-second Streamlit fragment. It never
hard-reloads the browser and never calls QuantConnect or Object Store.

## Roles

| Component | Role |
|---|---|
| `quant-strategies` | Strategy source and Stage 2 research orchestration |
| QuantConnect research project | Historical simulation / future ML training |
| Object Store | Model binaries and JSON diagnostics |
| `jobs.sync_quantconnect` | Ingest backtests + Object Store artifacts |
| PostgreSQL | System of record |
| Streamlit | Read-only research monitor |

Streamlit is **never** an execution engine. It must not train models, run
inner CV, launch backtests, or promote anything to paper/live.

## Schema

Migration `db/migrations/003_stage2_ml_research.sql` is idempotent and does
not drop Stage 1 columns.

New / extended objects:

- `research_runs`: `research_kind`, artifact hashes/ids, trial/CV counts
- `backtests`: model/signal columns
- `ml_trials`
- `ml_models`
- `ml_feature_diagnostics`
- `ml_signal_points`
- `research_artifacts`

Stage 1 aggregations continue to filter on `S1` / `S1__` only. Stage 2 rows
use `S2__` names and `research_suite_version` starting with `S2`.

## Sync

After the existing research-project backtest sync, `sync_quantconnect`
calls `qc_research.object_store_sync.sync_stage2_object_store`.

The artifact sync:

- identifies Stage 2 runs from synchronized backtests
- reads expected Object Store keys
- validates `schema_version = stage2_ml_v1`
- upserts artifacts / trials / models / diagnostics / signal points
- skips re-download when the stored SHA-256 already matches
- marks the run `INCOMPLETE` when a required artifact is missing or tampered

It does not create QuantConnect backtests.

The one-minute `--backtests-only` cron and flock lock are unchanged.

## Monitor

`qc_research/ml_monitor_ui.py` renders a Stage 2 section inside the existing
live monitor fragment when Stage 2 rows exist. It queries PostgreSQL only.

Holdout rows are shown separately and never change PASS/WATCH/FAIL.
