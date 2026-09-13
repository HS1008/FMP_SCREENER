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
| Sector daily bars (SPY + 11 sector ETFs) | FMP nightly bundles (`FMP_LEGACY`, `outputs/precomputed`) | `EQUITY_EOD` adapter → `mi_market_bars`. Providers: unset → `UNAVAILABLE`; `fixture` (tests); `yahoo` (optional); `ibkr`/`ibkr_collector` → Windows collector push (`ADJUSTED_LAST`, `IBKR_ADJUSTED_LAST`); DigitalOcean consumes stored bars and never opens TWS | **Windows live 2026-09-11 ET:** TWS `127.0.0.1:7496` client 72, 5/5 smoke + 46/46 universe `ADJUSTED_LAST` (501 bars/symbol, 2024-09-12…2026-09-11). Disposable local ingest: 23046 bars, 11 sector + 6 industry + 12 basket snapshots, `research_eligible=false`. Production remains `MI_EQUITY_PROVIDER` unset. **RIGHTS_PENDING. Soak NOT_STARTED. Do not cancel FMP.** | `NYSE` calendar, last completed session (transport success ≠ observation freshness ≠ universe coverage) | `INTERNAL_ONLY`. IBKR Pro / TWS access / paid market-data subscriptions do **not** imply remote AI redistribution rights | None (never silently FMP or Yahoo) | **Human gates**: (1) multi-session soak; (2) IBKR/exchange terms before any remote export; (3) only then consider production collector enablement |
| Sector 1D return / 1D RS vs SPY | FMP legacy metrics | Derived from `EQUITY_EOD` bars: `ret_1d = P[t]/P[prev aligned session] − 1`, `rs_1d = (P/B)[t]/(P/B)[t−1] − 1` | Fixture (aligned sessions, holiday gap, zero ≠ null) | inherits bars | inherits bars (`INTERNAL_ONLY`) | none | Same as bars |
| Longer sector windows (1W…12M RS) | FMP legacy | Derived from `EQUITY_EOD` bars | Fixture | inherits bars | inherits bars | none | Same as bars (+ history backfill depth) |
| Industry RS (sector → industry) | FMP legacy industry bundles | ETF comparisons vs sector ETF (SMH, XSD, KRE, XBI, XOP, XRT) from `EQUITY_EOD` | Fixture | inherits bars | inherits bars | none | Same as bars; sectors without a listed ETF comparison stay `UNAVAILABLE` |
| Subgroup rotation (semiconductor internals, themes) | not available | Curated equal-dollar daily-rebalanced baskets (`THEME_RS`), `research_eligible=false`, never GICS | Fixture | inherits bars | inherits bars | none | Same as bars; Consumer Staples / Materials / Real Estate / Utilities are explicitly `SUBGROUP_UNAVAILABLE` |
| Constituent dispersion, profiles, current caps, FMP universe | FMP | Not replaced | UNAVAILABLE | — | — | none | Licensed constituent source (human) |
| Order flow (bond aggregates) | FINRA Query API | FINRA (retained) | RETAINED | Daily aggregates | `INTERNAL_ONLY` (Query API terms) | none | — |
| Individual TRACE prints | not entitled | explicit `ENTITLEMENT_REQUIRED` | UNAVAILABLE | — | — | none | TRAQS / TRACE API entitlement (human) |
| IBKR quotes | Windows read-only collector (`client_id=71`, TWS `127.0.0.1:7496`) | unchanged | RETAINED | intraday snapshots | `INTERNAL_ONLY` | none | No settings edits; never an export source |
| IBKR daily bars (dashboard universe, 46 symbols) | not previously ingested | Windows `python -m ibkr_collector fetch-eod` (`client_id=72`) → Tailscale `POST /v1/equity_bars` + finalize → `EQUITY_EOD` / provider `IBKR` | **Code + live TWS + full universe + disposable ingest validated 2026-09-11 ET.** `ADJUSTED_LAST` accepted (IBKR info 2188 only; no TRADES fallback). Coverage 46/46. Production collection **off**. Remote export **UNRESOLVED**. | `NYSE`, last completed session; incremental `1 W` after `2 Y` backfill; coverage completeness is separate from transport and observation freshness | `INTERNAL_ONLY`; not in `AI_GATEWAY_REMOTE_VALUE_SOURCES` | none (retain last stored bars) | Multi-session soak on the Windows host; human data-rights decision before remote AI; do not cancel FMP |
| Stage 1 / CSFML / final holdout | Sealed research artifacts | Sealed | PRESERVED | — | never exported | — | Human research gates |

## Operating mode

FMP-off: `MI_FMP_FREE=1` (default). Legacy ingest is off and not a required-source alarm
(`RETIRED_OPTIONAL`). Set `MI_ALLOW_LEGACY_FMP=1` only as a migration reference. The dashboard
boots without `FMP_API_KEY` and without importing legacy provider engines.

## What is NOT yet replaceable

FMP is **not** fully replaceable. IBKR EOD code is implemented, a bounded Windows TWS session
returned 46/46 `ADJUSTED_LAST` series with enough history for 200DMA and 252-session metrics,
and one disposable local PostgreSQL ingest rebuilt sector/industry/subgroup snapshots. That is
**not** a production soak, **not** a DigitalOcean ingest, and **not** a data-rights approval.
Do not cancel the FMP subscription. Required before FMP retirement discussion: multi-session
soak, production reliability, unexplained-gap review, sector/industry/subgroup parity vs FMP,
and an explicit human rights decision. Production `MI_EQUITY_PROVIDER` remains unset.
