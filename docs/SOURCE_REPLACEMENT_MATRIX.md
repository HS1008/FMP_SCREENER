# Source replacement matrix (FMP independence)

Current-context catalog. Licensing/entitlement is recorded, not invented. Dashboard pages
and the read-only AI gateway read PostgreSQL only; refresh reloads DB reads. Ingestion belongs
to the scheduled `fmp-mi-refresh` job, never to page render or to a gateway request.

Status vocabulary: **VALIDATED_LIVE** (a bounded live read from a Linux host succeeded and the
data matched the contract), **FIXTURE_ONLY** (contract proven on fixtures, no live read from a
production-representative host yet), **RETAINED** (unchanged producer), **UNAVAILABLE** (explicit
state, no guessed data), **PRESERVED** (sealed research evidence).

| Source / function | Current producer | Replacement producer | Production validation | Freshness (policy v2) | Data-rights status | Fallback | Remaining blocker |
|---|---|---|---|---|---|---|---|
| Treasury nominal par yields (3M…30Y) | FRED `DGS*` (lagged mirror) | Treasury Daily Par Yield Curve XML (`TREASURY`, `UST_NOM_*`), preferred by observation date, tie → TREASURY | **VALIDATED_LIVE** 2026-09-11 UTC: one bounded read, HTTP 200, complete same-date curve 2026-09-10, all 10 required tenors, no weekend/Labor-Day/future prints (`tests/test_treasury_xml_live.py`) | `US_TREASURY` calendar, indicative ~15:30 ET, `LATEST_AVAILABLE` / `AWAITING_RELEASE` / `INGESTION_OVERDUE` / `STALE` | `ATTRIBUTION_REQUIRED` (public U.S. government feed) | FRED `DGS*` by observation date (never by retrieval time) | Multi-session soak on the DigitalOcean host after cutover (`MI_TREASURY_ENABLED=1` is the default) |
| Treasury real yields (5Y…30Y) | FRED `DFII*` | Treasury real-yield XML (`UST_REAL_*`) | FIXTURE_ONLY for the real curve (nominal live-validated; same feed family) | as above | `ATTRIBUTION_REQUIRED` | FRED `DFII*` | Live soak |
| Derived nominal − real | n/a | Derived from same-date legs, labelled `derived_nominal_minus_real` | Fixture | inherits legs | `ATTRIBUTION_REQUIRED` (derived from public legs) | none | Never labelled as `T5YIE`/`T10YIE` |
| Macro (payrolls, CPI, GDP, liquidity, policy rates) | FRED | FRED (retained) | RETAINED (production since MI v1) | Release-calendar policies per series (`SERIES_POLICIES`) | `ATTRIBUTION_REQUIRED` (FRED terms) | none | — |
| Credit OAS (ICE BofA via FRED) | FRED | FRED (retained) | RETAINED | Daily, `US_TREASURY` calendar | `RESTRICTED_REDISTRIBUTION`: values never leave the host to a remote AI client without an explicit recorded right | none | Licence decision if remote value export is ever wanted (human) |
| Sector daily bars (SPY + 11 sector ETFs) | FMP nightly bundles (`FMP_LEGACY`, `outputs/precomputed`) | `EQUITY_EOD` adapter → `mi_market_bars` (Yahoo optional, fixture for tests, `UNAVAILABLE` default) | FIXTURE_ONLY; production adapter **not configured** (`MI_EQUITY_PROVIDER` unset → explicit `ENTITLEMENT_UNVERIFIED`) | `NYSE` calendar, last completed session | `INTERNAL_ONLY` until a provider whose terms permit storage/redistribution is chosen; IBKR Pro, QuantConnect research and Yahoo availability do **not** imply export rights | None (never silently FMP) | **Human/provider decision**: choose and license the production equity EOD provider |
| Sector 1D return / 1D RS vs SPY | FMP legacy metrics | Derived from `EQUITY_EOD` bars: `ret_1d = P[t]/P[prev aligned session] − 1`, `rs_1d = (P/B)[t]/(P/B)[t−1] − 1` | Fixture (aligned sessions, holiday gap, zero ≠ null) | inherits bars | inherits bars (`INTERNAL_ONLY`) | none | Same as bars |
| Longer sector windows (1W…12M RS) | FMP legacy | Derived from `EQUITY_EOD` bars | Fixture | inherits bars | inherits bars | none | Same as bars (+ history backfill depth) |
| Industry RS (sector → industry) | FMP legacy industry bundles | ETF comparisons vs sector ETF (SMH, XSD, KRE, XBI, XOP, XRT) from `EQUITY_EOD` | Fixture | inherits bars | inherits bars | none | Same as bars; sectors without a listed ETF comparison stay `UNAVAILABLE` |
| Subgroup rotation (semiconductor internals, themes) | not available | Curated equal-dollar daily-rebalanced baskets (`THEME_RS`), `research_eligible=false`, never GICS | Fixture | inherits bars | inherits bars | none | Same as bars; Consumer Staples / Materials / Real Estate / Utilities are explicitly `SUBGROUP_UNAVAILABLE` |
| Constituent dispersion, profiles, current caps, FMP universe | FMP | Not replaced | UNAVAILABLE | — | — | none | Licensed constituent source (human) |
| Order flow (bond aggregates) | FINRA Query API | FINRA (retained) | RETAINED | Daily aggregates | `INTERNAL_ONLY` (Query API terms) | none | — |
| Individual TRACE prints | not entitled | explicit `ENTITLEMENT_REQUIRED` | UNAVAILABLE | — | — | none | TRAQS / TRACE API entitlement (human) |
| IBKR quotes | Windows read-only collector | unchanged | RETAINED | intraday snapshots | `INTERNAL_ONLY` | none | No settings edits; never an export source |
| Stage 1 / CSFML / final holdout | Sealed research artifacts | Sealed | PRESERVED | — | never exported | — | Human research gates |

## Operating mode

FMP-off: `MI_FMP_FREE=1` (default). Legacy ingest is off and not a required-source alarm
(`RETIRED_OPTIONAL`). Set `MI_ALLOW_LEGACY_FMP=1` only as a migration reference. The dashboard
boots without `FMP_API_KEY` and without importing legacy provider engines.

## What is NOT yet replaceable

FMP is **not** fully replaceable until the production equity EOD provider is chosen and its
entitlement recorded. Until then sector/industry/subgroup rows are either absent (provider
unconfigured → explicit `UNAVAILABLE`) or come from the retired legacy bundles when a human
opts in. Do not cancel the FMP subscription on the basis of this matrix; the recommendation
becomes possible only after the equity provider decision and a multi-session live soak of
Treasury XML + equity EOD on the production host.
