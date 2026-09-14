# Data lineage

For each major dataset: provider → adapter → job → cadence → table/view → page → rights.

| Dataset | Provider | Adapter / job | Cadence | Canonical objects | Streamlit | Fallback | Rights | Freshness |
|---|---|---|---|---|---|---|---|---|
| FRED macro / rates / credit / commodities | FRED | `jobs/market_intelligence_refresh.py --fred` | release / daily | `mi_macro_*`, `mi_v_source_health` | Macro, Rates, Credit, Overview | none | attribution; ICE restricted | series policy v2 |
| Treasury curve | US Treasury XML | refresh `--treasury` | daily | Treasury observations in rates context | Rates, Fixed Income, Morning Brief | FRED `DGS*` labeled | public | Treasury calendar |
| FINRA Query aggregates | FINRA | refresh `--finra` | daily | FINRA tables / `order_flow_*` | Order Flow, Fixed Income, Overview | none | INTERNAL_ONLY | Query API |
| FINRA TRACE prints | FINRA TRAQS | `TraceAdapter` | — | none | Data Health only | none | ENTITLEMENT_REQUIRED | n/a |
| Equity EOD / RS | IBKR collector or configured EOD | Windows `fetch-eod` / equity adapter | post-close | `mi_market_bars`, sector snapshots | Sectors, Overview | FMP legacy | INTERNAL_ONLY until export review | NYSE session |
| IBKR quotes | TWS | Windows collector client 71 | heartbeat | quote tables | Overview / Data Health | none | INTERNAL_ONLY | intraday |
| IBKR options | TWS API client 73 | `ibkr_collector.options` (no timer) | off | not persisted | Data Health | Cboe if licensed | PROVIDER_SUPPORT_REQUIRED + storage RIGHTS_PENDING | n/a |
| OpenBB/Cboe options | Cboe delayed JSON | `openbb_provider` | off | `mi_openbb_*`, `mi_v_options_*` | Overview vol panel if stored | IBKR | RIGHTS_PENDING | NYSE |
| OpenBB/Cboe VIX | CFE | `openbb_provider` | off | `mi_openbb_*` VIX views | Overview if stored | none | AGREEMENT_REQUIRED | NYSE |
| MSRB/EMMA munis | MSRB | `MsrbEmmaAdapter` | off | none | Fixed Income empty states | manual calculator | NOT_CONFIGURED | n/a |
| CFTC COT | CFTC | `CftcCotAdapter` | off | none | Data Health | none | NOT_CONFIGURED | weekly if added |
| EIA energy | EIA | `EiaEnergyAdapter`; Power Producers cache | off for MI | none in MI | Power Producers | FRED WTI/HH/copper | NOT_CONFIGURED | publication |
| FRED commodities | FRED (EIA/IMF) | refresh `--fred` (`DCOILWTICO`, `DHHNGSP`, `PCOPPUSDM`) | daily / monthly | `mi_macro_*` | Overview, Macro | none | attribution | series policy |
| SEC EDGAR | SEC | `EdgarAdapter` | on demand | none scheduled | Data Health | none | user-agent required | n/a |
| QC research | QuantConnect | artifact ingest | on demand | research tables | Strategy Monitor | none | INTERNAL_ONLY; holdout sealed | n/a |
| FMP legacy | FMP | nightly bundles | transitional | precomputed | optional comparison page | keep until replacements soak | INTERNAL_ONLY | daily |

Delay labels on option views come from `quality_json.delay_label` (migration 034). OpenBB
defaults to `CBOE_DELAYED`. IBKR is not published into those views until storage rights exist.

Export: restricted raw OPRA/Cboe/TRACE/IBKR values stay off remote AI/MCP unless explicitly allowlisted.
