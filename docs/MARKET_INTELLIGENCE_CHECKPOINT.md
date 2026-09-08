# Market Intelligence implementation checkpoint

Working log for the Market Intelligence milestone. Updated as work lands so the
session can resume after context compaction. Final status lives in
`docs/MARKET_INTELLIGENCE.md`; this file records what was actually run.

## Branches

| Repo | Branch | Base | Notes |
|------|--------|------|-------|
| FMP_SCREENER | `cursor/market-intelligence-v1-674b` | `main` @ `9d9d98708c06ef292cb663d91775cb8b44e1c05b` | all FMP work |
| quant-strategies | `cursor/market-intelligence-qc-674b` (planned) | PR #26 head `38612911f1a4613647951c71962a56aebbf98959` | stacked; isolated `research/market_intelligence/` only |

Workflow trigger audit (before any push):

- FMP: `deploy.yml` (push main only), `ingest_platform_research.yml`
  (repository_dispatch / hourly schedule / manual), `ingest_tlt_duration_momentum.yml`
  (manual), `platform_research_verify.yml` + `stage1_verify.yml` (workflow_run after
  deploy). No push trigger matches `cursor/market-intelligence-v1-674b`.
- QS (PR #26 tree): push patterns `smoke/**`, `stage1/**`, `bootstrap-research/**`,
  `smoke/PlatformResearch/**`, `cursor/platform-qc-*-674b`,
  `cursor/platform-research-tlt*`, `stage2-smoke/**`, `cursor/stage2-smoke-*-674b`,
  `stage2-window/**`, `cursor/stage2-window-*-674b`, `stage2-suite/**`,
  `cursor/stage2-suite-674b`, `cursor/stage2-suite-*-674b`,
  `cursor/stage2-inspect-*-674b`; `publish_platform_research.yml` fires on pushes
  touching `research/platform_smokes/**` or `research/platform_artifacts/**`.
  `cursor/market-intelligence-qc-674b` matches none, and the QS work must not touch
  those two paths.

## Environment

- Disposable PostgreSQL 16 on this VM: `postgresql://fmp_test:fmp_test@127.0.0.1:5432/fmp_test`
  (created for tests only; exported as `FMP_TEST_DATABASE_URL`).
- Baseline FMP suite on `main` (`9d9d987`): `python3 -m pytest -q tests` -> 131 passed.
- Protected artifact hashes before edits: `/tmp/protected_hashes_before.txt`
  (66 files under `stage1_results/`, `stage2_results/`, `qc_research/platform_artifacts/`).

## Completed work

(appended as it lands)

## Remaining blockers

(appended as discovered)
