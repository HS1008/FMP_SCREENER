# Source replacement matrix (FMP independence)

Current-context catalog. Licensing/entitlement is recorded, not invented.
Dashboard pages read PostgreSQL only. Refresh reloads DB reads.

| Category | Legacy / FMP | Replacement | Cadence | Entitlement | Status |
|---|---|---|---|---|---|
| Treasury nominal / real par yields | FRED DGS*/DFII* (lagged mirror) | Treasury daily XML (`TREASURY`) preferred; FRED fallback by observation date | EOD, ~15:30 ET quotation | Public official feed | Implemented |
| Other FRED macro (payrolls, CPI, GDP, liquidity, OAS) | FRED | FRED / ALFRED remain | Release calendar | FRED API key | Retained |
| FINRA bond aggregates | FINRA Query API | Unchanged | Daily aggregates, not live prints | FINRA app credentials | Retained |
| Individual TRACE | Not entitled | Explicit `ENTITLEMENT_REQUIRED` | n/a | TRAQS / TRACE API | Unavailable |
| Sector / industry ETF prices | FMP nightly + `outputs/precomputed` | `EQUITY_EOD` adapter → `mi_market_bars` | Last completed session | Yahoo optional / unverified; QC research ≠ export; IBKR Pro ≠ always-on export | Adapter + UNAVAILABLE if unconfigured |
| Profiles / current caps / FMP universe | FMP | Not replaced. Constituent dispersion stays unavailable without a licensed source | n/a | Missing access | Explicit unavailable |
| IBKR quotes | Windows collector | Unchanged snapshots, not daily bars | Intraday snapshot | Existing collector only | Unchanged (no settings edits) |
| Stage 1 / CSFML / holdout | Sealed | Sealed. Dashboard ingestion cannot unlock holdout | n/a | n/a | Preserved |

FMP-off: `MI_FMP_FREE=1` (default). Legacy ingest is off and not a required-source alarm (`RETIRED_OPTIONAL`). Set `MI_ALLOW_LEGACY_FMP=1` only as a migration reference. Do not delete historical evidence or cancel the FMP subscription from this change.
