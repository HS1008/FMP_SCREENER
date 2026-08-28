# Stage 1 run summaries

QuantConnect cannot represent an experiment that never ran (for example a
WFO OOS skipped because its training grid had no acceptable result).

The quant-strategies Stage 1 workflow publishes `run_summary.json` to the
private `stage1-results` branch. This public tree is the copy DigitalOcean
pulls with FMP_SCREENER:

```
stage1_results/<strategy_id>/<research_run_id>/run_summary.json
```

`--backtests-only` imports every file under this directory after the
QuantConnect backtest list sync. Re-importing the same JSON is
idempotent and finalizes `research_runs.run_status` to `COMPLETE` or
`INCOMPLETE`.

Do not put secrets in these files. Identities are run ids, not timestamps.
