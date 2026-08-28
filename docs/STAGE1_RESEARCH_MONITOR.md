# Stage 1 Research Monitor (FMP_SCREENER)

This document describes how Stage 1 QuantConnect research backtests land
in PostgreSQL and the Streamlit Strategy Monitor.

QuantConnect remains the simulation engine. PostgreSQL is the system of
record. Streamlit is the monitor. This app does **not** promote a
strategy to paper or live based on research results.

Research backtests come from the dedicated QuantConnect research project
(`qc_research_project_name` / `qc_research_project_id`, for SPYTrend:
`SPYTrendResearch`). Paper/live monitoring continues to use the execution
project (`qc_project_id` / `qc_deployment_id`). A new research project does
**not** restore holdout innocence; existing `EXPOSED_PRIOR_TO_STAGE1`
rows remain valid.

If `qc_research_project_id` is still NULL, `--backtests-only` looks up the
exact research project name via `/projects/read` and stores the ID. It
never falls back to the execution project.

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

Legacy backtests missing `backtest_start` / `backtest_end` get **one**
`/backtests/read` to store official `backtestStart` / `backtestEnd`. They
are never converted into Stage 1 rows. Once dates are stored they are not
re-read on later syncs. That is how historical SPYTrend full-history
runs become `EXPOSED_PRIOR_TO_STAGE1`.

`research_runs` is authoritative for progress (`expected_experiment_count`,
`run_status`, completed/failed/skipped). QuantConnect cannot represent a
skipped experiment that never ran. `--backtests-only` therefore
**automatically** imports every

`stage1_results/<strategy_id>/<research_run_id>/run_summary.json`

file in the working tree after the QC backtest list sync. Re-importing
the same JSON is idempotent (`ON CONFLICT (research_run_id)`). An explicit
path still works:

```bash
python -m jobs.sync_quantconnect --backtests-only --import-run-summary path/to/run_summary.json
```

After a skipped OOS (80 completed, 1 skipped) the monitor must show
**INCOMPLETE**, never `80/81 IN_PROGRESS`.

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

Keep the existing ~10-minute live QuantConnect cron intact. Deploy
installs the one-minute backtests-only cron automatically. The installer
is idempotent: re-deploying does not create a second cron line.

The cron is flock-protected (`flock -n` on `outputs/backtest_sync.flock`).
A second one-minute invocation exits immediately if a sync is still
running. Production verification uses the **same lock file** with
`flock -w` so it waits briefly for the cron sync instead of writing
PostgreSQL at the same time. Live-only verification does not take this
lock.

```bash
* * * * * flock -n /root/FMP_SCREENER/outputs/backtest_sync.flock -c 'cd /root/FMP_SCREENER && /root/FMP_SCREENER/venv/bin/python -m jobs.sync_quantconnect --backtests-only >> /root/FMP_SCREENER/outputs/backtest_sync.log 2>&1'
```

Idempotent installer (also executed by deploy):

```bash
bash scripts/install_backtest_sync_cron.sh
```

## Dashboard sections

Strategy Monitor still shows Current Paper State, Paper Performance,
Current Positions, Strategy Rules, Execution, and deployment metadata.

**STAGE 1 RESEARCH RESULTS** is the phone-visible research section
(smoke tests stay separate and are excluded from the 81-count). Tabs:

1. **Summary** — run status, PASS/WATCH/FAIL/INCOMPLETE, expected /
   completed / failed / skipped, selected parameter, research vs
   execution project, Git SHA, research date range.
2. **Development / Validation** — Baseline, frozen IS parameter choice,
   Validation (CAGR, Sharpe, Sortino, max drawdown, net profit, trades).
3. **Walk-Forward** — one row per WFO window including skipped OOS.
   Unstable parameter selection, OOS deterioration vs validation, failed
   or skipped windows are highlighted. Training grids stay in an expander.
4. **Equity Curves** — Baseline, Validation, and WFO OOS only. The 63
   training curves are not rendered together.
5. **Experiments** — filterable table of every Stage 1 experiment.
6. **Audit / Safety** — SPYTrendResearch vs SPYTrend, execution untouched,
   holdout / 2023+ / paper-live flags.

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
