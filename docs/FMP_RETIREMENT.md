# FMP retirement checklist

Goal: cancel the FMP subscription after replacements have proven coverage, freshness,
reliability, soak, fallback, and provenance. Do not remove FMP paths in this change.

| Page / consumer | Endpoint / data | Replacement | Status | Coverage | Freshness | Soak | Removal criteria |
|---|---|---|---|---|---|---|---|
| Legacy FMP comparison | nightly bundles / profiles | Equity EOD + sector snapshots | optional (`MI_ALLOW_LEGACY_FMP`) | incomplete vs historical FMP | bundle as-of | not started | FMP-free mode already hides the page |
| Sector breadth constituents | FMP profile universe | QC PIT internals | not replacement for current-universe breadth | current-universe only | snapshot | n/a | keep labeled CURRENT_UNIVERSE_CONTEXT_ONLY |
| Power / scratch dashboards | FMP prices (guarded) | IBKR / EOD adapter | refuse provider fetch by default | not proven | — | — | delete only after those entry points are unused |
| Valuation / rotation engines | FMP (legacy modules) | not migrated | transitional | unknown | unknown | no | inventory only; do not build new FMP calls |

Do not add new FMP dependencies.
