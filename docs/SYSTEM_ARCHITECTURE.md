# System architecture

Authoritative map of the Market Intelligence platform. Implementation details that differ
from earlier prompts follow the repository.

```
EXTERNAL / MARKET / RESEARCH SOURCES
                ↓
         PROVIDER ADAPTERS
                ↓
           NORMALIZATION
                ↓
   VALIDATION / QUALITY / PROVENANCE
                ↓
             POSTGRESQL
         CANONICAL SOURCE
                ↓
      ┌─────────┼──────────┐
      ↓         ↓          ↓
 STREAMLIT   AI/MCP      MONITORING
 READ-ONLY   CONTROLLED  / EXPORT
```

QuantConnect research stays isolated: artifacts may be ingested; Streamlit never launches
backtests, never trains models, and never writes market data.

## Invariants

- Streamlit is read-only PostgreSQL. No OpenBB, IBKR, FINRA, FRED, or QC fetch from pages.
- Missing values stay missing. Zero is not a substitute.
- Rights/entitlement blockers are Data Health states, not `FAILED`.
- Trading / paper execution / model promotion stay off.
- Stage 1 (81 experiments) and Stage 2 (`CrossSectionalFactorML`, 2025+ sealed holdout) are unchanged.

## Source precedence

| Dataset | Primary | Secondary / fallback | Notes |
|---|---|---|---|
| Current Treasury yields | Fiscal Data / Treasury Daily XML (`TREASURY`) | FRED `DGS*` | Do not mix dates into one synthetic curve |
| Historical rates / real yields / breakevens | FRED | — | ALFRED when vintage work is added |
| Corporate bond activity | FINRA Query API | IBKR quotes (not configured) | Individual TRACE prints are a separate entitlement |
| Municipal bonds | none configured | IBKR muni (not configured) | MSRB/EMMA is not TRACE |
| Credit OAS | ICE BofA via FRED | — | `RESTRICTED_REDISTRIBUTION` |
| Equity EOD / sectors | configured adapter (`EQUITY_EOD`) | FMP legacy (transitional) | Do not cancel FMP until soak |
| Options | IBKR if API OPRA + storage rights | OpenBB/Cboe if licensed | Both currently gated |
| VIX curve | CFE via OpenBB | — | `AGREEMENT_REQUIRED` |
| Macro | FRED / official agencies | — | |
| Research PIT | QuantConnect | — | Never replay today's screener universe |

## Streamlit navigation

Overview, Markets (Sectors, Rates, Credit, Order Flow, Fixed Income, Commodities), Economy (Macro),
Research (Strategy Monitor, Power Producers), System (Data Health, Morning Brief, Methodology).

Fixed Income tabs: Overview, Corporates/TRACE, Municipals, Relative value calculator, Ladder builder.
Calculator inputs are session-local and do not mutate PostgreSQL.

## Data Health semantics

Rights and entitlement states are not platform outages:

`HEALTHY`, `STALE`, `DEGRADED`, `FAILED`, `DISABLED`, `ENTITLEMENT_REQUIRED`,
`PROVIDER_SUPPORT_REQUIRED`, `RIGHTS_PENDING`, `AGREEMENT_REQUIRED`, `NOT_CONFIGURED`.

Streamlit never fetches providers. Missing values stay missing.

## Scheduling

Existing DigitalOcean / Windows collector / GitHub Actions jobs. Do not add a second
options or muni timer until rights and entitlement are proven. Initial options cadence,
if later authorized: one weekday snapshot after 16:05 America/New_York.
