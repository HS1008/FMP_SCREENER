# OpenBB Cboe options / VIX checkpoint

Implementation branch: `cursor/openbb-cboe-options-vix`
Worktree: `C:\Users\dipka\Documents\FMP_SCREENER_OPENBB`
Base: `origin/main` `43f45273d94aef6dfa0b9b4cd0bbe46ff0446265`

Do not use this file for credentials or live payloads.

## Phase

INSPECT (done) → IMPLEMENT (done) → TESTS (offline + disposable PG + bounded live Cboe done) → DOCS/AUDIT (done) → PR (do not merge; production flags stay off)

## Isolated bases (do not disturb)

| Path | Branch | HEAD | Notes |
|---|---|---|---|
| `C:\Users\dipka\Documents\FMP_SCREENER` | `cursor/mi-platform-v1` | `3248eef` | Dirty; uncommitted EIA/COT/SEC/033–036. Untouched. |
| `C:\Users\dipka\Documents\FMP_SCREENER_PROD_REPAIR` | `cursor/prod-verify-force-checkout` | `744824d` | Untouched. |
| `C:\Users\dipka\Documents\quant-strategies` | local main | behind origin | Research reference only. |
| `C:\Users\dipka\Documents\quant-strategies-csfml-impact` | PR #31 work | preserved | No research reruns. |

Later cleanup: remove this worktree after merge; do not delete the dirty MI tree. If this branch and `cursor/mi-platform-v1` both land, migration `033_openbb_options_vix.sql` must be reconciled with that tree's uncommitted `033`–`036`.

## Reproducible extra (authoritative for this branch)

Install into the worktree / CI / deploy venv:

```
pip install -r requirements.txt -r requirements-dev.txt -r requirements-openbb.txt
```

Pins: `openbb==4.7.2`, `openbb-cboe==1.6.1`. Companions resolved from those pins: `openbb-core==1.6.13`, `openbb-derivatives==1.6.2`, `openbb-yfinance==1.6.3`.

Do not depend on `C:\OpenBB`, `Documents\OpenBB`, or an editable OpenBB checkout. App CI/production Python is **3.12**. A local 3.14 worktree venv is acceptable for validation only.

## Relocated leftover (non-authoritative)

`C:\Users\dipka\Documents\OpenBB` is a relocated venv, not an app checkout. Activate scripts still set `VIRTUAL_ENV=C:\OpenBB\.venv`. Production code never depends on that path.

## Cboe / OpenBB facts used by the adapter

- Chains: `https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json`
- OpenBB `dte` uses local `now` + 1 day. We recompute DTE from the snapshot NYSE session date.
- `use_cache` caches symbol directories 24h, not chain quotes.
- Adjusted OCC symbols (captured expiration length > 6) are dropped upstream.
- IV / Greeks arrive as decimals (not percent). `change_percent` is already `/100`.
- GEX is not in the Cboe fetcher; OpenBB computes it later. We compute our own stored proxy.
- VX_EOD: 4pm ET levels; current path returns `YYYY-MM` expiration + price only.
- Website terms (cboe.com/terms): personal viewing/download; other copy/store/transmit/derived-product use needs permission. Recurring host collection + remote AI export stay **disabled** until a human rights decision.

## Design choices (as shipped)

- Source ids `OPENBB_CBOE_OPTIONS` and `OPENBB_CBOE_VIX`. Export scope `INTERNAL_ONLY`. Not on `AI_GATEWAY_REMOTE_VALUE_SOURCES`.
- Recurring fetch requires a dataset enable flag **and** the matching product rights flag (`MI_OPENBB_OPTIONS_RIGHTS_ACK` or `MI_OPENBB_VIX_RIGHTS_ACK`). The legacy `MI_OPENBB_CBOE_RIGHTS_ACK` umbrella is ignored. `--all-configured` skips when off. See `docs/OPENBB_CBOE_RIGHTS.md`.
- Optional install: `requirements-openbb.txt`. Deploy/CI install the extra when the file is present; that does **not** enable fetching.
- No OpenBB import on Streamlit / dry-run / read-model / planning paths.
- Migration: `033_openbb_options_vix.sql`.
- Morning Context keeps `morning_context_v2`. `options_volatility` is optional and does not fail an otherwise complete brief.
- Equity/Treasury/FINRA/IBKR/QC paths unchanged. OpenBB yfinance SPY history is a smoke check only.
- GEX method: `CALL_PLUS_PUT_MINUS_V1` proxy. No gamma-flip model. Units `delta_notional_per_1pct`. Never labelled dealer inventory.

## Disposable PostgreSQL + live query-back (this validation)

- Facility: ephemeral PostgreSQL 16.15, `%TEMP%\pgsql16`, `127.0.0.1:55432`, role `fmp_test`.
- Migration 033 applied then skipped (idempotent).
- Live kept counts: SPY 12956, QQQ 11280, IWM 5538, VX_EOD 9. Replay did not duplicate canonical snapshot rows.

## Remaining (operator, not this run)

- Human Cboe rights decision before production flags.
- GitHub-authenticated Stage 1 Production Verification dispatch against `43f45273` (blocked here).
- Do not merge as activation. Do not set product rights acks or `MI_OPENBB_INSTALL_EXTRA=1` in production from implementation PRs.
