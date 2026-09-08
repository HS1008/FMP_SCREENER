# Market Intelligence platform

Canonical PostgreSQL for macro, rates, credit, sector and morning-context data; DB-only
Streamlit pages; a read-only AI context API; and a dry-run-only research idea registry that
hands frozen, human-approved specifications to the existing QuantConnect research
infrastructure. This document is the operator reference. The working log with what was
actually run is `docs/MARKET_INTELLIGENCE_CHECKPOINT.md`.

## Two paths

```
Path 1  external providers -> ingestion + validation -> canonical PostgreSQL (mi_*)
        -> versioned analytics (mi_metric_snapshots, mi_credit_index_snapshots, mi_sector_snapshots)
        -> morning_context_v1 snapshot -> Streamlit pages (read-only role) / AI context API (read-only role)

Path 2  morning snapshot -> human or AI hypothesis -> research idea (versioned spec, spec_hash)
        -> SPEC_FROZEN -> RESEARCH_APPROVED (human, bound to spec_hash) -> QUEUED_DRY_RUN
        -> idea_research_contract_v1 -> quant-strategies research/market_intelligence (manifest, 0 QC creates)
        -> [human runs QC] -> canonical results -> research_runs -> Strategy Monitor -> human review
```

Path 2 stops at the contract. Nothing in either repository launches a backtest, promotes a
model, places an order, or writes from an AI actor.

## Capability matrix

| Capability | Status | Where | Gate / configuration |
|---|---|---|---|
| Schema `mi_*` (migrations 008-011, additive, idempotent) | Implemented | `db/migrations/008..011` | `python -m jobs.apply_migrations` (deploy already runs it) |
| FRED macro / rates / credit ingestion (44 series, `fred_catalog_v1`) | Implemented | `market_intelligence/{catalog,fred_client,ingest_fred}.py` | `FRED_API_KEY` (`CONFIGURATION_REQUIRED` until set) |
| Revision-preserving observations, NULL-not-zero, realtime_end sentinel | Implemented | `market_intelligence/store.py` | none |
| Freshness by cadence and US holidays, transport vs staleness | Implemented | `market_intelligence/freshness.py` | none |
| Transforms (YoY, 3m/6m annualised, bps changes, slopes, percentiles, z-scores) | Implemented | `market_intelligence/{transforms,analytics}.py` | none |
| ICE BofA credit series: ingested, restricted redistribution, redacted on export | Implemented | `catalog.py` export scope, `export_policy.py` | distribution restriction honoured automatically |
| Legacy FMP precomputed bridge -> `mi_sector_snapshots` / `mi_industry_snapshots` | Implemented | `market_intelligence/legacy_bridge.py`, `jobs.ingest_legacy_sector_precomputed` | reads saved bundles only; `MARKET_INTELLIGENCE_PRECOMPUTED_ROOT` optional |
| Sector label mapping (provider -> canonical, unknowns quarantined) | Implemented | `market_intelligence/sector_mapping.py` | none |
| Morning context snapshot (`morning_context_v1`, hashed, immutable) | Implemented | `market_intelligence/morning_context.py`, `jobs.build_morning_context` | none |
| Streamlit pages 10-16 (Market Pulse, Macro, Rates, Credit, Sector Rotation V2, Data Health, Morning Context) | Implemented, DB-only | `pages/1*.py`, `market_intelligence/pages_ui.py` | `DATABASE_READONLY_URL` (pages fail closed with a message otherwise) |
| Read-only role | SQL provided | `db/roles/market_intelligence_readonly.sql` | operator runs it once |
| AI context API (bearer token, GET only, export policy, no SQL) | Implemented | `ai_context_api.py` | `AI_CONTEXT_API_TOKEN`, `DATABASE_READONLY_URL` |
| Research delivery visibility (remote vs local fallback, hash, age) | Implemented | `qc_research/delivery_visibility.py`, `.github/workflows/ingest_platform_research.yml` | repo vars `QS_ARTIFACT_SOURCE_REF`, `QS_ARTIFACT_SOURCE_PATH` |
| Research idea registry + CLI (draft/freeze/approve/queue dry-run/link) | Implemented, dry-run only | `market_intelligence/ideas.py`, `jobs.research_ideas` | `queue --execute` is refused by code |
| QS `research/market_intelligence` (contract verify, manifest, sector diagnostics) | Implemented, 0 QC creates | quant-strategies PR #27 (stacked on #26) | `MI_RESEARCH_ACTIVATION_APPROVED=<spec_hash>` only yields `PLAN_APPROVED_FOR_HUMAN_EXECUTION` |
| Bond analytics (fixed-coupon bullets: accrued, YTM, durations, convexity, G/Z spread) | Implemented, bounded | `market_intelligence/bonds.py`, `jobs.bond_analytics` | needs source-verified terms + quotes in `mi_bond_*`; none are ingested yet |
| Bond OAS / callable duration / floaters | Not supported (flagged) | `bonds.py` `support_status` | requires an option / floating-rate model |
| IBKR market data | Disabled | `market_intelligence/adapters.py` | no client shipped; no order surface exists |
| FINRA TRACE | Disabled | `adapters.py` | `MI_TRACE_ENABLED` + FINRA credentials -> `ENTITLEMENT_REQUIRED` (still no client) |
| SEC EDGAR reference (submissions, company facts) | Opt-in | `adapters.py` | `SEC_USER_AGENT` with contact + `MI_EDGAR_ENABLED=1`; never called by the timer |
| Systemd timer + API service templates | Templates + dry-run installer | `deploy/market_intelligence/`, `scripts/install_market_intelligence_timers.sh` | operator runs `--apply`; deploy.yml does not |
| QC Market Intelligence activation on 2025+ data | Human gate | QS `ActivationGate` | not implemented as automation by design |
| Paper/live deployment, IBKR orders, model promotion, AI writes | Out of scope | — | — |

Access statuses written to `mi_source_registry.access_status` and shown on Data Health:
`CONFIGURED`, `CONFIGURATION_REQUIRED`, `DISABLED`, `ENTITLEMENT_REQUIRED`.

## Data contracts

* Hash convention: SHA-256 over canonical JSON (sorted keys, compact separators, NaN/NaT/Infinity
  normalised to `null` first). Shared by snapshots, exports, idea specs, contracts, and the QS package
  (`research/platform.hash_payload` parity is tested on both sides).
* Observations: `(series_id, observation_date, revision_seq)`; only one `is_current` row per date;
  a changed value inserts a new revision and supersedes the old one. `.` and empty tokens store NULL
  with the raw token retained.
* FRED `realtime_end = 9999-12-31` is kept as a plain date sentinel and never converted to a timestamp.
* Analytics rows carry `analytics_version`, units (`pct`, `bps`, `fraction`, `index`), window and
  `history_status` (`OK`, `LIMITED_TO_1Y`, `AVAILABLE_ONLY`, `INSUFFICIENT_HISTORY`). ICE BofA windows are truncated at
  one year of history on purpose.
* Morning snapshot `morning_context_v1`: `snapshot_sha256`, `input_refs` (per-section run ids and
  observation dates), `sections_status`, `completeness`. Replays with identical inputs produce the
  same hash.
* Export policy: `RESTRICTED_REDISTRIBUTION` series (ICE BofA) have values removed and a restriction reason attached; metadata stays; attribution
  is attached; `export_sha256` covers the exported body and is verifiable.
* Idea contract `idea_research_contract_v1`: `spec_hash`, `approval` (actor, hash bound),
  `execution_mode = DRY_RUN`, `holdout_policy.access = NONE`, all dates `< 2025-01-01`,
  `artifact_sha256`. The QS side refuses anything else.

## Operator steps (production host)

Prerequisites already present: `/root/FMP_SCREENER` checkout with `venv`, PostgreSQL, the
`fmp-dashboard` service, deploy.yml applying migrations on push to `main`.

1. Merge the FMP PR; the existing deploy applies migrations 008-011 (additive, no data change) and
   restarts Streamlit. Pages 10-16 appear and report `DATABASE_READONLY_URL` missing until step 3.
2. Environment file:
   `install -m 0600 deploy/market_intelligence/market_intelligence.env.example /etc/fmp/market_intelligence.env`
   and fill `FRED_API_KEY`, writer identity, `AI_CONTEXT_API_TOKEN` (long random), later
   `DATABASE_READONLY_URL`.
3. Read-only role: `psql "$ADMIN_DATABASE_URL" -v ro_password='<secret>' -v DBNAME=<db> -f db/roles/market_intelligence_readonly.sql`,
   then set `DATABASE_READONLY_URL=postgresql://mi_readonly:<secret>@127.0.0.1:5432/<db>` in
   `/etc/fmp/market_intelligence.env` and in the `fmp-dashboard` environment (the role has SELECT on
   `mi_v_*` views only; the tests assert it cannot read raw tables or write).
4. First refresh, manually and visibly:
   `set -a; . /etc/fmp/market_intelligence.env; set +a; venv/bin/python -m jobs.market_intelligence_refresh --all-configured --dry-run --json`
   (plan only), then `--all-configured --mode full --json`. Exit 0 ok, 2 partial (per-step JSON says
   which configured source failed), 75 lock contention. Check `pages/15_Data_Health`.
5. Timers: `scripts/install_market_intelligence_timers.sh` (dry run prints the rendered units and
   actions), then `--apply` (and `--with-api` for the AI context API). The schedule is weekdays
   09:15 and 18:30 America/New_York with a randomised delay; `Persistent=true` catches missed runs.
   Nothing else on the host (dashboard service, cron lines, live QC sync) is modified.
6. AI context API smoke: `curl -sS -H "Authorization: Bearer $AI_CONTEXT_API_TOKEN" http://127.0.0.1:8765/v1/context/morning/latest | head -c 400`.
   `/health` is unauthenticated and minimal; every `/v1` route (`/v1/ready`,
   `/v1/context/{morning,macro,rates,credit,sectors,strategies}/latest`, `/v1/context/data-health`)
   requires the token; POST/PUT/DELETE return 405.
7. Research ideas (dry run): `venv/bin/python -m jobs.research_ideas validate --spec idea.json`,
   `register --spec idea.json --actor <human>`, `freeze --idea <id> --actor <human>`,
   `approve --idea <id> --approved-by <human> --version <n> --spec-hash <hash>`,
   `queue --idea <id> --actor <human> --contract-out contract.json` (dry run is the only mode).
   Copy the contract to the QS checkout and run
   `python -m research.market_intelligence.cli plan --contract contract.json --out research/market_intelligence_manifests/`,
   then `verify --manifest <file>`. Any QC launch on the plan is a human action outside these tools.

Rollback: disable the timer (`systemctl disable --now fmp-mi-refresh.timer`), stop the API. Tables
are additive; leaving them in place is safe. No migration is destructive.

## Boundaries preserved

* No Stage 1 / Stage 2 rerun; no changes to `stage1_results/`, `stage2_results/`,
  `qc_research/platform_artifacts/`, QC ids, economic fingerprints, or TLTDurationMomentum
  (hash diff recorded in the checkpoint).
* No 2025+ or final-holdout access anywhere in Path 2: spec validation, contract, manifest and
  `ActivationGate` all enforce `< 2025-01-01`.
* No model binaries in GitHub, PostgreSQL, or FMP.
* Writer identity only in backend jobs; pages and API use the read-only role with no fallback.

## Known limits / CONFIGURATION_REQUIRED

* Real FRED validation on this VM was not possible (no `FRED_API_KEY`); ingestion is exercised with
  the fixture client that mirrors the API contract (pagination, `.` tokens, sentinel dates, metadata
  mismatch). First production run should be `--mode full` and inspected on Data Health.
* Legacy bridge parity was validated against synthetic bundles shaped like `outputs/precomputed`;
  the production bundles may include labels that land in quarantine (visible on Sector Rotation V2).
* Bond tables have no ingestion source yet; `jobs.bond_analytics` reports every bond as a skip until
  source-verified terms and quotes exist.
* Holiday calendar covers US federal holidays only.
