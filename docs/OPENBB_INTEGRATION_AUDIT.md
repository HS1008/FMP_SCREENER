# OpenBB / Cboe options + VIX integration audit

Branch: `cursor/openbb-cboe-options-vix`
Worktree: `C:\Users\dipka\Documents\FMP_SCREENER_OPENBB`
Base: `origin/main` `43f45273`
Date: 2026-09-14

This file records evidence, not activation. Production flags stay off.

## Verdict

**DONE for the gated vertical slice, clean extra install, disposable PostgreSQL ingest, bounded live Cboe query-back, and read-only consumer proof.**
**NOT a production enablement.** Collection flags remain `0`. `MI_OPENBB_CBOE_RIGHTS_ACK` stays unset in production.
**Stage 1 production-verification dispatch was blocked** (no GitHub CLI / MCP auth in this session).
**OUT_OF_SCOPE:** production enablement, FMP cancellation, QC/research methodology, IBKR setting changes, 2025+ / FINAL_HOLDOUT.

## Checklist

| Item | Status | Evidence |
|---|---|---|
| A. Isolated worktree; dirty `FMP_SCREENER` / quant trees untouched | DONE | Implementation is in `FMP_SCREENER_OPENBB` only |
| B. Clean reproducible OpenBB extra | DONE | Worktree `.venv` (Python 3.14.4 locally; CI/deploy is 3.12) installed from `requirements.txt` + `requirements-dev.txt` + `requirements-openbb.txt`. Pins: `openbb==4.7.2`, `openbb-cboe==1.6.1` (companions `openbb-core==1.6.13`, `openbb-derivatives==1.6.2`, `openbb-yfinance==1.6.3`). No editable/path dependency on `C:\OpenBB` or `Documents\OpenBB`. `OpenBBClient` imports from this interpreter. |
| C. Optional extra + lazy import | DONE | `requirements-openbb.txt`; package import does not load `openbb`; CI/deploy install the extra; fetch stays flag-gated |
| D. Dual source ids, INTERNAL_ONLY, rights ack | DONE | `OPENBB_CBOE_OPTIONS` / `OPENBB_CBOE_VIX`; `MI_OPENBB_CBOE_RIGHTS_ACK`; not on remote allowlist |
| E. Migration 033 + readonly grants | DONE | `db/migrations/033_openbb_options_vix.sql`; applied twice on disposable PG (second pass skipped/idempotent); conditional GRANTs on four `mi_v_*` views |
| F. Normalize / analytics (session DTE, IV decimal, GEX proxy, VX month labels) | DONE | Fixture tests: Friday-as-Friday, adjusted OCC, IV decimal, ATM / 30D interpolation, GEX 1000/sign cancel, VIX month precision |
| G. Refresh / read models / UI / morning / export | DONE | `--options` / `--vix`; `--all-configured` skips when off; Market Pulse + Data Health + Morning Brief read stored snapshots; options section excluded from morning completeness |
| H. Offline unit tests | DONE | Targeted `tests/test_openbb_*.py` plus adapter/env contract tests |
| I. Disposable PostgreSQL ingest / replay / isolation | DONE | Ephemeral PostgreSQL 16.15 at `%TEMP%\pgsql16`, listen `127.0.0.1:55432`, role `fmp_test`. Not production (non-5432, temp datadir). Session-only `FMP_TEST_DATABASE_URL`. |
| J. Bounded live Cboe → stored snapshot → query-back | DONE | Live normalize + ingest + replay (opt-in `OPENBB_LIVE_INGEST=1`): SPY 12956/12956/0, QQQ 11280/11280/0, IWM 5538/5538/0, VX_EOD 9/9/0. Replay status `REPLAY`, same `snapshot_id`. Four COMPLETE snapshots; contract/point counts match kept rows. Pandas `NaT` last-trade is null. OCC/float strike quantized to 0.001. |
| K. Streamlit / Morning Context / export on stored data | DONE (code, not visual browser) | AST scan: only `openbb_provider/client.py` imports `openbb`. `pages_ui.py`, `read_models.py`, `morning_context.py`, `export_policy.py`, `ai_context_api.py`, and `pages/*.py` do not. Monkeypatch: read models explode if fetch is called. External export redacts nested symbols/vix. |
| L. Docs | DONE | `SOURCE_REPLACEMENT_MATRIX.md`, `MARKET_INTELLIGENCE.md`, `AI_GATEWAY.md`, this audit |
| M. Production activation | OUT_OF_SCOPE | Flags remain `0`. No merge to `main`, no host enable |

## What this does not replace

Treasury Daily XML, FINRA Query, IBKR collector / equity EOD, FRED, and QC research paths are unchanged. OpenBB yfinance is not a dashboard producer. FMP is not cancelled.

## Recurring collection gate

Both of these are required before any scheduled fetch:

1. `MI_OPENBB_OPTIONS_ENABLED=1` and/or `MI_OPENBB_VIX_ENABLED=1`
2. `MI_OPENBB_CBOE_RIGHTS_ACK=1`

`--all-configured` skips when either is missing. Explicit `--options` / `--vix` fail closed.

## OpenBB venv leftover (not used by this branch)

`C:\Users\dipka\Documents\OpenBB\.venv` is a relocated leftover. Activate scripts still set `VIRTUAL_ENV=C:\OpenBB\.venv`. This worktree does not depend on it. Project-level references to `C:\OpenBB` exist only as documentation of that stale leftover.

## GEX

OI-derived gamma-exposure **proxy** (`GEX_PROXY` / `gex_proxy_v1`). Sign convention `CALL_PLUS_PUT_MINUS_V1` (calls +, puts −). Units `delta_notional_per_1pct`. `dealer_gex` is always `False`. Missing gamma/OI/multiplier are excluded, never zero-filled.

## Local full-suite note (Windows host)

With disposable PG + `psql` on PATH: **46 failed, 957 passed, 19 skipped**. Failures are host limitations, not OpenBB regressions:

- `core.autocrlf=true` CRLF vs LF: sealed `STAGE1_SPYTrend_c04553d8` digest and migration `001` checksum baseline
- WinError 1314 symlink privilege (cutover/`current` tests)
- `bash` not on PATH (deploy/identity scripts, systemd-analyze)
- Morning snapshot timezone (`-05:00` vs UTC `+00:00`)

CI (Ubuntu, PostgreSQL service, `psql`, LF working tree) is the merge gate for those tests. Do not rewrite sealed research artifacts or weaken tests to obtain a local green.

## Stage 1 production verification (`43f45273`)

YAML on main is correctly wired: `workflow_run` after `Deploy FMP Dashboard` plus `workflow_dispatch`. Sibling Platform Research and MI research workspace verifiers were observed succeeding on this baseline; Stage 1 was not observed succeeding here.

This session could not list or dispatch GitHub Actions (`gh` unauthenticated; GitHub MCP auth skipped). No Stage 1 YAML change is mixed into this OpenBB commit. Likely operational (QC sync + host flock), not a shared root cause with OpenBB.

## Next authorized operator actions

1. Human Cboe rights decision before any production flag is flipped.
2. After merge to `main`, soak collection on a non-production host if rights are granted — not this PR.
3. Manually dispatch **Stage 1 Production Verification** against `43f45273` when GitHub auth is available. Do not launch the 81-experiment suite.
