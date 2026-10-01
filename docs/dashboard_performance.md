# Dashboard performance — 2026-10-01

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
