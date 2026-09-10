# Cross-repo research artifact contract

Producer: `HS1008/quant-strategies`
Consumer: `HS1008/FMP_SCREENER`

Streamlit never trains models, never launches QuantConnect, and never downloads `model.pkl`.

## SHA-256

SHA-256 over canonical JSON (`sort_keys=True`, `separators=(",", ":")`) with the `artifact_sha256` key excluded.

Producer: `research/stage2/artifact_contract.py`
Consumer: `qc_research/contracts/hashing.py`

## Required identity fields

- `schema_version`
- `research_run_id`
- `strategy_id`
- `research_lineage_id` (when the kind has lineage)
- `feature_set_id` / `feature_set_hash` (Stage 2)
- `target_id` / `target_hash` (Stage 2)
- `holdout_accessed=false` on official non-holdout evidence
- `economic_gate` remains `NOT_DEFINED` until a human defines thresholds
- `promotion_gate` is not an economic PASS
- no `SYNTHETIC_TEST_ONLY` accepted as official evidence
- no model binary publication

## Compatibility

| Producer schema | Consumer ingest | Monitor |
|---|---|---|
| Stage 1 orchestrator `run_summary` | `jobs/stage1_backtests.py` | Stage 1 tabs |
| `stage2_ml_v1` | `qc_research/object_store_sync.py` | Stage 2 / library |
| `platform_artifact_v1` / `platform_v1` | `qc_research/platform_ingest.py` | Platform research |

A schema change requires a new `schema_version` and fixtures on both sides.

## Compatibility matrix

| Kind | Producer | Consumer | Official holdout |
|---|---|---|---|
| Stage 1 `run_summary` | `research/backtest_orchestrator.py` | `jobs/stage1_backtests.py` | never |
| Stage 2 `run_manifest` / `run_summary` / training / OOS / assessment | `research/stage2/artifact_contract.py` | `qc_research/object_store_sync.py` | `holdout_accessed=false` |
| Platform `platform_artifact_v1` | research adapters | `qc_research/platform_ingest.py` | 2025+ windows rejected |

Sanitized fixtures: producer `research/artifact_fixtures.py`, consumer `qc_research/contracts/fixtures.py`.
When both repos are present, `tests/test_cross_repo_artifact_contract.py` requires identical SHA-256 for each sanitized kind.
`SANITIZED_CONTRACT_FIXTURE` is for tests. `SYNTHETIC_TEST_ONLY` is refused as official evidence.
`model.pkl` stays on QuantConnect Object Store and is never downloaded by FMP.
