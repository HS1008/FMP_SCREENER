# IBKR options-chain prototype

Reviewed 2026-09-14. Collector exists. Recurring production collection is **off**.
Export scope: `INTERNAL_ONLY`. Not on `AI_GATEWAY_REMOTE_VALUE_SOURCES`.
Data Health: `IBKR_OPTIONS = PROVIDER_SUPPORT_REQUIRED`. `IBKR_OPTIONS_STORAGE = RIGHTS_PENDING`.

## Post-OPRA API probe (2026-09-14 after 16:00 ET, client 73)

Client Portal: OPRA Top of Book (L1) Active, API acknowledgement signed, TWS restarted.
Qualified SMART contract: `SPY 260915C00761000` conId `922515140`.

| reqMarketDataType | marketDataType callback | bid/ask/last | volume/OI | model IV/Greeks | Errors |
|---|---|---|---|---|---|
| 1 live | none | none | none | none | 354, 10091, 2186 on underlying |
| 2 frozen | **2** | **none** | none | none | **354**, 10091 (`SPY ARCA/TOP`) |
| 3 delayed | 3 on single SMART/IBUSOPT | **none** (no ticks 66–68) | none | intermittent 80–83 | 10167, 10091 |

Frozen Type 2 after the close is **Case B**: OPRA entitlement is not reaching the TWS socket API. Do not buy another package. Open an IBKR ticket with this table if Client Portal still shows Active. Do not enable collection.

## Earlier live TWS probe (2026-09-14, client id 73, `127.0.0.1:7496`)

Bounded `fetch-options` against this username, market hours, SPY/QQQ/IWM. **Did not POST. Did not subscribe OPRA. Did not enable production collection.**

| Check | Result |
|---|---|
| Handshake / `reqSecDefOptParams` | Works. SPY SMART 34 expiries × 483 strikes; QQQ 33 × 523; IWM 33 × 197 |
| Qualify via `reqContractDetails` | 20/20 ATM specs resolved after using delayed SPY spot **762.79** |
| Delayed US equity L1 | Yes (`reqMarketDataType(3)`). Error 10167 = not live-subscribed, showing delayed |
| Live OPRA | No |
| Delayed OPRA bid/ask/last to the API | **Not delivered.** 20 qualified SPY contracts: `quoted=0`, no `marketDataType` callback, no delayed ticks 66–68 after 12s |
| Error 10091 | “Part of requested market data requires additional subscription for API … Delayed market data is available. SPY ARCA/TOP/” |
| Generic ticks 100,101,106 on delayed | Error 2187. IBKR delayed generic allow-list is **101,106** (OI, 30d IV), not 100. Collector now sends 101,106 only when delayed |
| Greeks / per-contract IV | None. IBKR documents live Greeks as requiring live OPRA **and** live underlying |
| OI / volume | None on this session (no delayed ticks; generic 100 invalid on delayed fallback) |
| Line limit (error 101) | Not hit at 10 concurrent lines |
| Cboe OPTIONS/VIX | Unchanged; collection flags stay 0 |

**Preference vs Cboe website JSON:** IBKR is still the cheaper path **if** delayed OPRA L1 actually reaches this API client (listed free 15-minute OPRA L1; live OPRA L1 USD 1.50/mo non-pro). Cboe website JSON still needs written consent + a signed licence before PostgreSQL storage. **Do not enable IBKR production collection until delayed or live option ticks are observed and a storage-rights review is recorded.** Human next step: in TWS, confirm a SPY option quote shows delayed (brown) data, then Account Management → Market Data for OPRA delayed or live L1 on **this API username**. Re-run `fetch-options`. Do not purchase from this PR.

## Why IBKR vs Cboe website JSON

Cboe delayed-quotes JSON used by OpenBB requires prior written consent and a signed license before PostgreSQL storage (`docs/OPENBB_CBOE_RIGHTS.md`). Cboe Options Top internal distribution is published at $9,000/month — a different product.

IBKR delayed **OPRA** is listed as free 15-minute delayed L1 on IBKR’s market-data pricing page ([Market Data Pricing](https://www.interactivebrokers.com/en/pricing/market-data-pricing.php), retrieved 2026-09-14). Live OPRA Top of Book is **USD 1.50/month** non-professional (USD 32.75 professional), waived at USD 20 commissions. That is materially cheaper than a Cboe licensed feed **if** this TWS username actually receives delayed or live OPRA ticks.

This prototype does **not** subscribe you to OPRA. Entitlement is whatever the existing TWS user already has. Confirm with a local `fetch-options` run (error 354 / 10167 / 10091 / empty quotes = not entitled for API quotes).

## TWS API used

Authoritative: [Option chains](https://interactivebrokers.github.io/tws-api/options.html), [tick types](https://interactivebrokers.github.io/tws-api/tick_types.html), [delayed data](https://interactivebrokers.github.io/tws-api/delayed_data.html), [market data lines](https://interactivebrokers.github.io/tws-api/market_data.html).

1. Qualify the underlying (`reqContractDetails`, STK/SMART) and take delayed L1 **without** generic ticks (same pattern as the quote diagnostic).
2. `reqSecDefOptParams(symbol, "", "STK", underlyingConId)` — expiries and strikes without the `reqContractDetails` chain throttle. The strike×expiry grid is a union, not a cartesian product of live contracts.
3. Qualify each selected OPT with `reqContractDetails` (skip error 200) so ATM weeklies that do not exist are dropped.
4. `reqMarketDataType(3)` — delayed if unentitled; TWS still returns live when the session has it.
5. Bounded `reqMktData` on qualified OPT contracts. Delayed generic ticks are `101,106` only (OI, 30d IV). Live uses `100,101,106`. Snapshots cannot request generic ticks, so this uses a short stream then `cancelMktData`.
6. Greeks / per-contract IV from `tickOptionComputation` (ticks 10–13 live, 80–83 delayed) **when the session is entitled**. Bid/ask/last from default ticks (or 66–68 delayed).

`exerciseOptions` is blocked on the read-only client. Client id **73** so it does not collide with quotes (71) or EOD (72). Localhost TWS only.

## Fields

| Need | IBKR field | This username (2026-09-14) |
|---|---|---|
| Bid / ask / last | ticks 1/2/4 or delayed 66/67/68 | Equity delayed yes; option delayed **not observed on API** |
| Size | 0/3/5 or delayed 69/70/71 | Same |
| Contract volume | tick 8 / delayed 74 (not generic 100 on delayed) | Not observed |
| Open interest | delayed generic **101** → ticks 27/28 | Not observed; 2187 if 100 is included |
| Underlying 30d IV | delayed generic **106** → tick 24 | Not observed |
| Per-contract IV / greeks | `tickOptionComputation` 10–13 / delayed 80–83 | Requires live OPRA + live underlying |
| Chain geometry | `reqSecDefOptParams` + `reqContractDetails` | **Yes** |

Underlying ETFs (SPY/QQQ/IWM) still need the usual US equity permissions this collector already uses. Live US option NBBO needs **OPRA**. Delayed OPRA is 15 minutes.

## Market-data lines

Default max is **100 simultaneous top-of-book lines**, shared with TWS windows and the quote collector (5 symbols). This prototype uses at most **20** concurrent option lines, one underlying at a time, then cancels. Error 101 is classified as `line_limit`. Do not subscribe the full SPY chain (thousands of contracts).

## Safe sampling (if later authorized)

- Symbols: SPY, QQQ, IWM only.
- Next **6** expirations, **5** strikes each side of ATM, calls and puts (~132 contracts/underlying), after `reqContractDetails` drops missing combos.
- One weekday snapshot after 16:05 America/New_York — not streaming, not intraday polling.
- Do not run while the quote collector already saturates the line budget; use client id 73.
- Recurring host storage stays off until delayed or live option ticks are proven **and** a storage-rights review is recorded.

## Schema reuse

Quotes map to `NormalizedChain` via `market_intelligence.ibkr_options.normalize_ibkr_chain` (`source_id=IBKR_OPTIONS`, `provider=ibkr`). Market Pulse analytics (`compute_options_metrics`) can consume that object. Views `mi_v_options_latest` / `mi_v_options_contracts_latest` read `quality_json.delay_label` (migration 034); OpenBB defaults to `CBOE_DELAYED`. Do not persist IBKR snapshots until storage rights are explicit. Catalog row is `PROVIDER_SUPPORT_REQUIRED`.

## How to probe (Windows, TWS logged in)

```
# Use the collector venv (official ibapi 10.50+), from the repo root:
%LOCALAPPDATA%\FMP_SCREENER\ibkr-collector\venv\Scripts\python.exe -m ibkr_collector fetch-options --symbols SPY,QQQ,IWM --max-expirations 2 --atm-strikes 2 --max-lines 10
```

Prints counts and entitlement errors only. **Does not POST.** **Does not enable the timer.**

## Do not enable yet

- `MI_IBKR_OPTIONS_ENABLED` stays `0`
- Not wired to `jobs.market_intelligence_refresh`
- Cboe OPTIONS/VIX flags stay `0`
- No AI/MCP raw chain export
