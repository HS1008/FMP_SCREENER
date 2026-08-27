# Stage 1 Research Monitor (FMP_SCREENER)

This document describes how Stage 1 QuantConnect research backtests land
in PostgreSQL and the Streamlit Strategy Monitor.

QuantConnect remains the simulation engine. PostgreSQL is the system of
record. Streamlit is the monitor. This app does **not** promote a
strategy to paper or live based on research results.

## Database tables

Migrations live in `db/migrations/` and are applied by:

```bash
python -m jobs.apply_migrations
```

GitHub Actions runs this **before** restarting Streamlit.

### `backtests` (extended)

Existing live/legacy columns are unchanged. Stage 1 adds:

- `research_suite_version`, `research_run_id`, `research_experiment_id`
- `research_test_type`, `research_phase`, `research_window_id`
- `research_git_commit`, `research_is_holdout`, `research_dirty`
- `train_start` / `train_end` / `test_start` / `test_end`
- `parameters_json` (strategy parameters only; `research_*` keys are split out)
- `objective_name` / `objective_value`
- `raw_statistics_json`, `research_guide_json`
- `backtest_start` / `backtest_end`, `error_message`

Primary experiment metadata comes from QuantConnect `parameterSet`.
Backtest name parsing (`S1__strategy__run__type__window__seq`) is fallback
only.

Canonical metric storage:

- CAGR / drawdown / net profit / win rate / PSR = **decimal** (`0.12` = 12%)
- Sharpe / Sortino / Alpha / Beta = ratio / raw value
- Format only in Streamlit

Legacy backtests without Stage 1 metadata stay in the same table and are
shown under **Legacy / Other Backtests**. They are excluded from Stage 1
aggregates.

### `backtest_equity_points`

Strategy Equity chart samples (bounded, typically ≤ 1000 points) keyed by
`(backtest_id, timestamp, series_name)`.

### `research_runs`

One row per `research_run_id`, including `holdout_accessed` and
`holdout_access_count`.

### `schema_migrations`

Records applied SQL files. Re-running a migration is safe (`IF NOT EXISTS`).

## Backtest syncing

```bash
python -m jobs.sync_quantconnect              # live + backtests (default)
python -m jobs.sync_quantconnect --live-only
python -m jobs.sync_quantconnect --backtests-only
```

Default behavior is unchanged: live status, portfolio, positions, orders,
trades, plus a backtest list upsert.

For **new or incomplete Stage 1** backtests (`name` starts with `S1__`):

1. `/backtests/read` for `parameterSet`, statistics, researchGuide, errors
2. `/backtests/chart/read` with chart `Strategy Equity`

Completed Stage 1 rows that already have research metadata **and** an
equity curve are only summary-updated from `/backtests/list`. Incomplete
or running backtests are refreshed.

Failed / runtime-error experiments remain visible, are never used as
winners, and keep `error_message` when QuantConnect provides one.

## One-minute research sync

Keep the existing ~10-minute live QuantConnect cron intact.

Optional faster research ingest (not installed automatically):

```bash
* * * * * cd /root/FMP_SCREENER && /root/FMP_SCREENER/venv/bin/python -m jobs.sync_quantconnect --backtests-only >> /root/FMP_SCREENER/outputs/backtest_sync.log 2>&1
```

Idempotent installer (run only after you approve a production cron change):

```bash
bash scripts/install_backtest_sync_cron.sh
```

## Dashboard sections

Strategy Monitor still shows Current Paper State, Paper Performance,
Current Positions, Strategy Rules, Execution, and deployment metadata.

**Stage 1 Validation** adds a research-run selector (default: latest run
for the selected strategy) and tabs:

1. **Summary** — baseline / validation / holdout KPIs, OOS/IS ratio,
   robustness, WFO stats, PASS/WATCH/FAIL with the underlying checks.
   Untouched holdout is shown as a success state, not missing data.
   Repeated holdout access is a prominent warning.
2. **Split Tests** — BASELINE_DEV, VALIDATION, FINAL_HOLDOUT only.
3. **Parameter Robustness** — every `PARAM_SENS` backtest, Sharpe vs
   `sma_period` when that primary parameter is numeric. The raw maximum
   is not implied to be preferred.
4. **Walk-Forward** — OOS KPIs and one row per `WFO_TEST`. Holdout WFO
   is labelled separately. Training grids are under an expander.
5. **All Backtests** — every Stage 1 backtest with filters, plus equity
   curve for the selected id (`Equity curve not synced yet.` if missing).

**Backtest vs Paper** compares paper against, in order:

1. FINAL_HOLDOUT if it exists for the latest Stage 1 run
2. VALIDATION
3. BASELINE_DEV
4. legacy latest backtest fallback

The compared backtest is labelled explicitly. Paper Sharpe/CAGR stay
blank until enough live history exists.

## Migration process

Deploy path:

```
GitHub main push
  → GitHub Actions
  → SSH DigitalOcean
  → git pull
  → activate venv
  → pip install -r requirements.txt
  → python -m jobs.apply_migrations
  → restart fmp-dashboard
  → verify active
```

`.env` still supplies `DB_*` and `QC_*`. Credentials are not logged.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| "Stage 1 columns are not in PostgreSQL yet" | Run `python -m jobs.apply_migrations` |
| Stage 1 names listed but no metadata | Wait for `--backtests-only` sync; confirm `parameterSet` on the QC backtest |
| "Equity curve not synced yet." | Chart endpoint still loading, or `Strategy Equity` missing. Re-sync. |
| Paper compared to a WFO grid run | Confirm `research_test_type` is populated; comparison should ignore grid runs |
| Holdout warning | Expected if `--include-holdout` was used more than once for that git commit |
| Live positions wrong | Live sync path is unchanged; use `--live-only` to isolate |

Do not put database passwords in this repo, logs, or the dashboard.
