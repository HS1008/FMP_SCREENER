# Dashboard performance

## US Equities lazy stock board — 2026-10-13

Same Streamlit AppTest fixture before and after, on the current catalog
(54 groups, 252 memberships, 179 unique tickers). Quotes and daily bars were
empty, so these times are Python and component-mount cost, not SQL. `MI_PERF`
spans were printed; wall-clock figures are one local run and are not a
pass/fail gate.

| Collapsed initial view | Before (`bb94f4a`) | After |
| --- | --- | --- |
| Cold AppTest run | 1.494 s | 0.848 s |
| Warm AppTest run | 0.177 s | 0.051 s |
| Heatmap mounts | 56 (54 stock + sector + subsector) | 2 (sector + subsector) |
| Individual-stock heatmap mounts | 54 | 0 |
| `stock_risk_metrics` calls | 1 call, 252 memberships | 0 |
| `dashboard_price_bars` | one unfiltered read | one batch: `SMH`, `IGV`, `CIBR`, `SPY` |
| `aligned_us_equity_returns` | full panel, including stock baskets | `include_stock_baskets=False` |

Opening the three groups that contain ORCL (search) mounted 3 stock heatmaps,
calculated risk once for that ticker, and read daily bars for ORCL only. The
other 176 catalog tickers were not in that read. Refresh kept the search and
did not recalculate the cached risk.

Disposable `fmp_mi_verify` (100 stored symbols, not the full 179-name catalog;
450-day window; local PostgreSQL, not production):

| Read | Rows | Time |
| --- | --- | --- |
| Unfiltered `dashboard_price_bars` | 30,476 | 1.503 s |
| Tech subsector ETFs + SPY | 1,228 | 0.046 s |

In-process preparation of 260 synthetic sessions for all 179 unique tickers,
including one missing session and one provisional bar, took 0.919 s. One
semiconductor group took 0.038 s, and the cached repeat took 0.010 s. The
missing session stayed `N/A` (`missing session 2026-08-14`); it was not filled.

Not measured: production `EXPLAIN`, browser paint time, and a full-catalog
SQL read (the fixture database does not hold every thematic ticker). At a
390px viewport the open heatmap host was 324px wide, the canvas was 768px,
and `#chart-scroll` used `overflow-x: auto`, so the extra columns scroll
inside the chart.

## 2026-10-01

Local measurements only. This machine has no production PostgreSQL, so query
plans and pool waits were not timed. `MI_PERF=1` prints server span timings to
stderr. It stays off unless that variable is set.

## US Equities, full page

Synthetic stored quotes and 280 daily bars for every approved symbol.
Streamlit AppTest, two runs.

| | Before | After |
| --- | --- | --- |
| Cold server run | 0.420 s | 0.365 s |
| Warm server run | 0.187 s | 0.105 s |
| Display toggle | | 0.103 s |
| Sector absolute/relative toggle | | 0.103 s |
| ECharts mounts per run | 19 | 19 |
| Time-series mounts per run | 5 | 5 |
| JavaScript copied into chart protobufs | 22.76 MB | 0 bytes inline (`bundle.js` URL) |
| Time to serialize that JavaScript | 0.014 s | ~0 s |

The page still mounts the same charts. The browser fetches each library once
from `/_stcore/bidi-components/<chart>/bundle.js` and caches it.

## Other local timings

| Work | Time |
| --- | --- |
| `price_horizons` for 92 symbols, 320 bars | 0.0529 s |
| `price_horizons` for 50 stock-group memberships | 0.0288 s |
| Clip five 2,000-point series to a date window | 0.0028 s |
| Twelve ECharts component registrations, before the file-backed change | 0.0039 s |

## Left unchanged after measurement

- Monitor history stays one cached read and is clipped in Python. Clipping is
  about 3 ms, and the default range is the full history. A range-aware SQL
  query would add a round trip whenever the date changes.
- 1W through 1Y returns still use `price_horizons`. Preparing the whole
  universe is about 53 ms. A stored horizon table was not added.
- The read-only pool stays at `pool_size=2` and `max_overflow=2`.
- No indexes were added. `EXPLAIN (ANALYZE, BUFFERS)` was not run.
