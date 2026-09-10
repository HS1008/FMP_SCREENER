# Market Intelligence platform

Canonical PostgreSQL for macro, rates, credit, sector and morning-context data; DB-only
Streamlit pages; a read-only AI context API served from frozen snapshots; a dry-run-only
research idea registry; and an isolated PIT sector-internals producer/consumer pair. This
document is the operator reference. The working log with what was actually run is
`docs/MARKET_INTELLIGENCE_CHECKPOINT.md`.

Status vocabulary used below (one value per capability, chosen from evidence, not intent):

* `IMPLEMENTED_AND_TESTED` — code + regression tests pass on a disposable PostgreSQL 16.
* `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` — tested on fixtures shaped like the real contract; the
  real provider / real files were not reachable from the build host.
* `CONFIGURATION_REQUIRED` — code complete; an operator-supplied variable is missing.
* `ENTITLEMENT_REQUIRED` — a licence / redistribution decision that no code can take.
* `DISABLED_BY_POLICY` — deliberately off; enabling is a human decision, not a config flip.
* `NOT_IMPLEMENTED` — no producer/consumer code exists; do not describe as "only credentials remain".

## Architecture (unchanged)

```
External sources -> isolated adapters -> backend ingestion / normalisation / validation
  -> canonical PostgreSQL (mi_*; revisions kept, NULL never zero)
  -> versioned analytics / curated mi_v_* views
  -> DB-only Streamlit (read-only role)          -> read-only AI context API (frozen snapshot, hashed envelope)

Path 2  morning snapshot -> idea (idea_spec_v2, spec_hash) -> SPEC_FROZEN -> RESEARCH_APPROVED (human, bound
        to version + spec_hash, spec COMPLETE) -> QUEUED_DRY_RUN -> idea_research_contract_v2
        -> quant-strategies research/market_intelligence (verify, manifest, local PIT producer; 0 QC creates)
        -> sector_internals_v1 artifact file -> jobs.ingest_pit_sector_internals -> mi_pit_sector_* -> page 17
```

Nothing in either repository launches a backtest, promotes a model, places an order, exposes a
write endpoint, or writes from an AI actor.

## Capability matrix

| Capability | Status | Where | Gate / configuration |
|---|---|---|---|
| Schema `mi_*` migrations 008-015 (additive; clean apply, upgrade from 011, second apply no-op all tested) | IMPLEMENTED_AND_TESTED | `db/migrations/008..015`, `jobs.apply_migrations` | deploy.yml already runs `apply_migrations` after a merge to `main` |
| FRED catalog `fred_catalog_v2`: every series checked against official metadata (units, frequency, SA, aggregation); WTREGEN/WRESBAL = millions of dollars, weekly average ending Wednesday | IMPLEMENTED_AND_TESTED (catalog) | `market_intelligence/catalog.py`, `tests/fixtures/fred_official_metadata.json` | — |
| FRED ingestion with publication gate: `validate_metadata` MISMATCH / missing metadata -> observations quarantined (`mi_macro_observation_quarantine`), last valid data kept, run `METADATA_REJECTED`, other series unaffected | IMPLEMENTED_NOT_EXTERNALLY_VALIDATED | `market_intelligence/{fred_client,ingest_fred,store}.py` | `FRED_API_KEY` -> CONFIGURATION_REQUIRED on this build host |
| Observation revisions (`revision_seq`, `is_current`), `.`/non-finite -> NULL, future observation dates rejected, LATEST_REVISED labelling for backfilled history (not ALFRED vintages) | IMPLEMENTED_AND_TESTED | `store.py`, `ingest_fred.py` | — |
| Display-unit conversion versioned (`display_scale_v1`), raw provider units retained | IMPLEMENTED_AND_TESTED | `analytics.py` | — |
| Transforms with calendar-period matching, gap labelling, comparison dates/status in payloads, bps conversions, zero-variance and coverage guards | IMPLEMENTED_AND_TESTED | `transforms.py`, `analytics.py` | — |
| Bounded idempotent history backfill + revision-aware incremental recompute of page metrics | IMPLEMENTED_AND_TESTED | `analytics.build_analytics(history_start=...)`, `jobs.market_intelligence_refresh --backfill-analytics-from` | first production run should backfill (see steps) |
| Freshness: observation vs retrieval vs attempt vs success, per-dataset cadence tolerance, health recomputed against the reader's clock (no ingestion needed), `stale_after_estimate` never shown as a release date | IMPLEMENTED_AND_TESTED | `freshness.py`, `read_models.source_health` | — |
| Morning context `morning_context_v2`: current-only builder, DB capture time (`REPEATABLE READ`), exact input refs, content hash, explicit supersession, historical reconstruction refused | IMPLEMENTED_AND_TESTED | `morning_context.py`, `jobs.build_morning_context` | — |
| Export policy `export_safe_v2`: explicit behaviour per scope, unknown scope fails closed, nested/derived/ordering redaction, recursive secret exclusion | IMPLEMENTED_AND_TESTED | `export_policy.py` | ICE BofA and legacy FMP content = ENTITLEMENT_REQUIRED for external AI consumers |
| AI context API: one response contract hashed after every field is set; sections served from the frozen snapshot; live data-health route labelled live with its own hash; fail-closed auth; readiness probes views | IMPLEMENTED_AND_TESTED (HTTP tests) | `ai_context_api.py`, `readonly_db.probe_readonly` | `AI_CONTEXT_API_TOKEN`, `DATABASE_READONLY_URL` |
| AI gateway (REST `/api/v1` + MCP Streamable HTTP + owner OAuth): semantic tools over the same `mi_v_*` views; no arbitrary SQL; no execution | IMPLEMENTED_AND_TESTED | `ai_gateway/`, `docs/AI_GATEWAY.md` | same token + `DATABASE_READONLY_URL`; public HTTPS is CONFIGURATION_REQUIRED |
| Legacy FMP bridge `legacy_bridge_v2`: session-calendar coverage, stale instruments, PARTIAL health on quarantine, hashed revision provenance; reads saved bundles only | IMPLEMENTED_NOT_EXTERNALLY_VALIDATED | `legacy_bridge.py`, `jobs.ingest_legacy_sector_precomputed` | real `outputs/precomputed` bundles not present on the build host |
| Sector label mapping (provider -> canonical; themes separate; unknowns quarantined) | IMPLEMENTED_AND_TESTED | `sector_mapping.py` | — |
| Streamlit pages 10-17 (Market Pulse, Macro, Rates, Credit, Sector Rotation V2, Data Health, Morning Context, PIT Sector Internals) | IMPLEMENTED_AND_TESTED (AppTest populated / empty / unconfigured) | `pages/1*.py`, `pages_ui.py` | `DATABASE_READONLY_URL`; no writer fallback |
| Read-only role (`mi_readonly`): SELECT on `mi_v_*` only; repeatable grants without password reset; real-role denial tests through psql | IMPLEMENTED_AND_TESTED | `db/roles/market_intelligence_readonly.sql` | operator provisions once (see steps) |
| Research idea registry `idea_spec_v2` / contract v2: completeness gate for approval, structured date ranges, idea holdout never loosened, duplicate spec refused, honest adapter statuses | IMPLEMENTED_AND_TESTED | `ideas.py`, `jobs.research_ideas`, migration 013 | `queue --execute` refused by code; `economic_gate = NOT_DEFINED` unless a human supplies thresholds |
| QS `research/market_intelligence`: contract v2 verify, dry-run manifest, frozen-spec RS diagnostics, `sector_internals_v1` producer (PIT membership calendar + prices + optional PIT caps) | IMPLEMENTED_AND_TESTED on synthetic pre-holdout fixtures | quant-strategies PR #27 | QC research project that exports real PIT inputs: NOT_IMPLEMENTED; activation on current data: DISABLED_BY_POLICY |
| PIT sector consumer: hash/schema/units/definitions/PIT flags/date-gate validation, revision-aware canonical ingest, artifact registry, views, page 17 | IMPLEMENTED_AND_TESTED | `pit_sector.py`, `jobs.ingest_pit_sector_internals`, migration 014 | consumes local artifact files only; SYNTHETIC_TEST_ONLY artifacts are `research_eligible = FALSE` |
| Bond analytics `bond_analytics_v2`: verified brackets + re-pricing tolerance, honest domains (`UNSUPPORTED_*` statuses), first-coupon stubs, LAST never labelled MID, Z-spread only with documented curve + repricing | IMPLEMENTED_AND_TESTED (analytical cross-checks) | `bonds.py`, `jobs.bond_analytics` | no bond terms/quotes are ingested: every bond is a skip until a source exists |
| Bond OAS / callable duration / floaters | NOT_IMPLEMENTED (flagged `UNSUPPORTED_NO_OPTION_MODEL`) | `bonds.py` | needs an option / floating-rate model |
| IBKR market data (quotes) | IMPLEMENTED_AND_TESTED (Windows collector + ingest); live TWS handshake verified | `ibkr_collector/`, `ibkr_ingest/`, migration 016 | existing TWS session; delayed data if unentitled |
| IBKR orders / account / positions | DISABLED_BY_POLICY | `adapters.py`, `ibkr_collector/readonly_client.py` | no order surface exists |
| FINRA TRACE | DISABLED_BY_POLICY -> ENTITLEMENT_REQUIRED when enabled | `adapters.py` | `MI_TRACE_ENABLED` + FINRA credentials (still no client) |
| SEC EDGAR reference | CONFIGURATION_REQUIRED (opt-in) | `adapters.py` | `SEC_USER_AGENT` with contact + `MI_EDGAR_ENABLED=1`; never called by the timer |
| Systemd timer + API service templates, dry-run installer; DST/units verified with `systemd-analyze` | IMPLEMENTED_AND_TESTED (templates) | `deploy/market_intelligence/`, `scripts/install_market_intelligence_timers.sh` | operator runs `--apply`; deploy.yml does not |
| PR validation CI: offline tests + disposable PostgreSQL 16, no secrets, `pull_request` only | IMPLEMENTED_AND_TESTED locally (workflow audited by tests) | `.github/workflows/pr_validation.yml` (both repos) | first real run happens when the PR head is pushed |
| FRED live validation (manual): bounded real FRED requests on disposable PostgreSQL, `secrets.FRED_API_KEY` received explicitly, export contract checked | IMPLEMENTED_AND_TESTED (workflow + script; live run is a human dispatch) | `.github/workflows/fred_validation.yml`, `jobs.validate_fred_live` | GitHub cannot dispatch a brand-new workflow until it exists on `main`. Merge the minimal workflow PR first; do not merge this full PR only to make the workflow runnable. |
| DigitalOcean secret provisioning (env file only; no activation) | IMPLEMENTED_AND_TESTED (script dry-run / apply on a temp file) | `scripts/provision_digitalocean_mi_secrets.sh`, `deploy/market_intelligence/DIGITALOCEAN_SECRETS.md` | operator `--apply` writes `FRED_API_KEY`; timers stay off |
| Research artifact delivery: remote fetch, otherwise committed copy labelled `LAST_KNOWN_GOOD` | IMPLEMENTED_AND_TESTED | `qc_research/delivery_visibility.py`, `ingest_platform_research.yml` | repo vars `QS_ARTIFACT_SOURCE_REF`, `QS_ARTIFACT_SOURCE_PATH` (see below) |
| QC activation on 2025+ data, ML_FINAL_HOLDOUT, paper/live, AI writes | DISABLED_BY_POLICY | QS `ActivationGate`, FMP `queue --execute` | never automated |

Access statuses written to `mi_source_registry.access_status` and shown on Data Health:
`CONFIGURED`, `CONFIGURATION_REQUIRED`, `DISABLED`, `ENTITLEMENT_REQUIRED`.

## Data contracts

* Hash convention: SHA-256 over canonical JSON (sorted keys, compact separators, NaN/NaT/Infinity
  -> `null`). The internal artifact hash excludes only the top-level `artifact_sha256` field and
  preserves list order. Shared by snapshots, idea specs, contracts, `sector_internals_v1`, and the
  QS package (`research/platform/hashing.hash_payload` parity is tested on both sides).
* Observations: `(series_id, observation_date, revision_seq)`; one `is_current` row per date; a
  changed value inserts a new revision and supersedes the old one. `.`, empty and non-finite tokens
  store NULL with the raw token retained. Observation dates after the retrieval date are quarantined.
* Publication gate: a series whose FRED metadata does not match the catalog (`units`,
  `frequency`, `seasonal_adjustment`, identity) or is missing is `METADATA_REJECTED`; its payload is
  kept in `mi_macro_observation_quarantine`, its last validated observations stay current, and
  `mi_v_macro_quarantine_summary` reports it. Other series continue.
* Analytics rows carry `analytics_version`, `transform_version`, display conversion version, raw and
  display units, window coverage, and the comparison anchor actually used (`comparison_date`,
  `gap_days` / `anchor_lag_days`, status `OK`, `MULTI_SESSION_GAP`, `GAP_IN_WINDOW`,
  `INSUFFICIENT_HISTORY`, `INSUFFICIENT_DATA` with a reason). Nothing is forward-filled. Historical
  backfills are labelled `LATEST_REVISED`.
* Morning snapshot `morning_context_v2`: `snapshot_sha256` (full body), `content_sha256`
  (body without generation timestamps), `cutoff_at` (database transaction clock, not a supplied value),
  `input_refs` (series revisions, credit rows, metric rows, sector rows, run ids), `sections_status`
  (presence, required-field coverage, captured freshness, provider status), `superseded_by` /
  `publication_state = SUPERSEDED` on the older row. Old snapshots
  are never edited; `--cutoff` in the past is refused (`HistoricalReconstructionUnsupported`).
* API envelope (`export_safe_v2`): `export_schema_version`, `provenance`, `source_snapshot_hash`,
  `section`, `available`, `unavailable_reason`, `degraded`, `latest_snapshot`, `delivery_health`,
  `restricted_entries`, `body`, `export_sha256` = SHA-256 of the whole envelope minus `export_sha256`.
  Verified on the actual HTTP JSON in tests (no field stripping).
* Idea contract `idea_research_contract_v2`: `spec_hash`, `spec_completeness = COMPLETE`,
  `effective_holdout_start` (min of idea policy and platform 2025-01-01), structured `date_ranges`,
  `economic_gate` (`NOT_DEFINED` unless `HUMAN_SUPPLIED`), `approval` bound to version + hash,
  `execution_mode = DRY_RUN`, `holdout_policy.access = NONE`, `artifact_sha256`. The QS side refuses
  v1, INCOMPLETE specs, looser boundaries and any date on/after the boundary.
* `sector_internals_v1` (QS producer -> FMP consumer): aggregates only (constituent-level keys are
  rejected), `pit.membership_pit/classification_pit = true`, `constituent_level_data_included =
  false`, units `fraction_0_1` / `simple_return_fraction` / `sum_of_squared_weights_0_1`,
  `definitions` (trailing vs held returns, weights, denominators), `boundary.effective_holdout_start`
  (every row strictly earlier), `lineage` (input hashes, sources, method version), `coverage`,
  `provenance` (`SYNTHETIC_TEST_ONLY` is stored but `research_eligible = FALSE`). Consumer key is
  `(decision_date, sector, method_version)`; a changed row becomes `revision_seq + 1`, nothing is
  deleted, identical artifacts are no-ops.

## What external AI consumers cannot receive yet (and what would change it)

* ICE BofA credit levels/changes/percentiles/history (`RESTRICTED_REDISTRIBUTION`): FRED's terms
  restrict redistribution of these series. Identity, dates and a restriction reason are exported.
  Change requires a redistribution entitlement from ICE/FRED — ENTITLEMENT_REQUIRED.
* Legacy FMP constituent/sector analytics and QC-derived sector aggregates (`INTERNAL_ONLY`): no
  redistribution decision has been taken for FMP-derived or QC-derived data. Change requires a
  documented policy decision and, for FMP, a licence review. Internal DB-only pages show everything.

## Operator steps (production host; every step is a human action)

Prerequisites already present: `/root/FMP_SCREENER` checkout with `venv`, PostgreSQL, the
`fmp-dashboard` service, deploy.yml applying migrations on push to `main`.

1. Merge the FMP PR. The existing deploy applies migrations 008-016 (additive, no data change) and
   restarts Streamlit. Pages 10-17 appear and report `DATABASE_READONLY_URL` missing until step 3.
   Rollback of a bad merge = revert + redeploy; the `mi_*` tables can stay (no view or table is dropped).
2. Environment file:
   `install -m 0600 deploy/market_intelligence/market_intelligence.env.example /etc/fmp/market_intelligence.env`
   and fill the writer identity, `FRED_API_KEY`, `AI_CONTEXT_API_TOKEN` (long random), later
   `DATABASE_READONLY_URL`.
3. Read-only role (password never in argv or shell history). The dashboard writer cannot
   `CREATE ROLE`. Use `MI_ADMIN_DATABASE_URL` / `ADMIN_DATABASE_URL`, or local postgres peer
   (`sudo -n -u postgres`) when the writer host is loopback. Then:
   `psql "$ADMIN_DATABASE_URL" -f db/roles/market_intelligence_readonly.sql` (prompts for the
   password only on first creation), or non-interactively
   `psql "$ADMIN_DATABASE_URL" -v ro_password="$(cat /etc/fmp/mi_readonly.pw)" -f db/roles/market_intelligence_readonly.sql`
   with a 0600 password file. Re-run the same file after every merge that adds `mi_v_*` views (grant
   refresh only; the password is untouched). Then set
   `DATABASE_READONLY_URL=postgresql://mi_readonly:...@127.0.0.1:5432/<db>` in the env file and in
   the `fmp-dashboard` environment. Verify:
   `psql "$DATABASE_READONLY_URL" -c 'SELECT COUNT(*) FROM mi_v_source_health'` succeeds and
   `psql "$DATABASE_READONLY_URL" -c 'SELECT COUNT(*) FROM mi_macro_observations'` is
   `permission denied`. On PostgreSQL < 15, also `REVOKE CREATE ON SCHEMA public FROM PUBLIC` if the
   database still grants it (the tests document that a per-role REVOKE does not cancel PUBLIC).
4. First refresh, manually and visibly (`set -a; . /etc/fmp/market_intelligence.env; set +a` first):
   * `venv/bin/python -m jobs.market_intelligence_refresh --probe-config --json` (read-only probe).
   * `venv/bin/python -m jobs.market_intelligence_refresh --all-configured --dry-run --json` (plan).
   * `venv/bin/python -m jobs.market_intelligence_refresh --fred --mode full --json`, then check
     `pages/15_Data_Health`: every series `VALIDATED`; any `METADATA_REJECTED` row is a catalog/metadata
     disagreement to inspect, not data loss (quarantine summary on the same page).
   * `venv/bin/python -m jobs.market_intelligence_refresh --legacy-sector --json` if
     `outputs/precomputed` bundles exist (bridge; no FMP calls). Quarantined bundles show as PARTIAL.
   * `venv/bin/python -m jobs.market_intelligence_refresh --build-analytics --backfill-analytics-from 2019-01-01 --json`
     once, so Credit/Rates charts have stored history on first render (bounded, idempotent,
     labelled LATEST_REVISED). Later runs use `--build-analytics` (incremental, revision-aware).
   * `venv/bin/python -m jobs.build_morning_context --json` and open `pages/16_Morning_Context`.
   Exit codes: 0 ok, 2 partial (JSON says which configured source failed), 75 lock contention.
5. Timers: `scripts/install_market_intelligence_timers.sh` (dry run prints rendered units and
   actions), then `--apply` (and `--with-api` for the AI context API). Schedule: weekdays 09:15 and
   18:30 America/New_York (DST-aware, verified with `systemd-analyze calendar`), randomised delay,
   `Persistent=true`. Overlap is impossible (oneshot unit + shared PostgreSQL advisory lock -> exit 75).
   Nothing else on the host (dashboard service, cron lines, live QC sync) is modified. Verify with
   `systemctl list-timers fmp-mi-refresh.timer`, `systemctl status fmp-mi-refresh.service`, and the
   job log `outputs/market_intelligence_refresh.log` (stdout is appended there; secrets are never printed).
6. AI context API smoke (localhost only):
   `curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/v1/ready` -> 401 without token;
   `curl -sS -H "Authorization: Bearer $AI_CONTEXT_API_TOKEN" http://127.0.0.1:8765/v1/ready` ->
   `{"status":"OK",...}` only when every required view answers; `.../v1/context/morning/latest`
   returns an envelope whose `export_sha256` verifies with
   `python -c 'import json,sys; from market_intelligence.export_policy import verify_export_hash; print(verify_export_hash(json.load(sys.stdin)))'`.
   POST/PUT/DELETE return 405. Nothing is exposed publicly; front with a private/TLS proxy if needed.
7. Research ideas (dry run only): `venv/bin/python -m jobs.research_ideas validate --spec idea.json`
   (reports `completeness`, `missing_fields`, `effective_holdout_start`, `economic_gate`),
   `register`, `freeze`, `approve --approved-by <human> --version <n> --spec-hash <hash>` (refused
   while INCOMPLETE), `queue --contract-out contract.json`. Copy the contract to the QS checkout:
   `python -m research.market_intelligence.cli plan --contract contract.json --out manifest.json`
   then `verify --manifest manifest.json`. Any QC launch is a human action outside these tools.
8. PIT sector internals (local, pre-holdout inputs only): in QS
   `python -m research.market_intelligence.cli internals --contract contract.json --membership membership.json --prices prices.json --out internals.json`
   (inputs must declare PIT membership/classification; current-universe inputs are refused), then in FMP
   `venv/bin/python -m jobs.ingest_pit_sector_internals --artifact internals.json --validate-only`
   and without `--validate-only` to store. Page 17 labels provenance; `SYNTHETIC_TEST_ONLY` is never
   research evidence. Exit codes: 0 ok / unchanged, 2 rejected (recorded as a FAILED run), 3 config, 75 lock.
9. Research artifact delivery (`ingest_platform_research.yml`): the hourly schedule pulls
   `research/platform_smokes` from QS at `QS_ARTIFACT_SOURCE_REF`; that path does not exist on QS
   `main` today, so until PR #26 merges (or the variable points at its branch) every scheduled run
   re-ingests the committed FMP copies and reports `LAST_KNOWN_GOOD`, not a fresh delivery. Set the
   repository variables `QS_ARTIFACT_SOURCE_REF` / `QS_ARTIFACT_SOURCE_PATH` to an already-published
   ref/path; never point them at a research branch to "repair" delivery.

10. Windows-local IBKR collector (optional; uses the existing TWS session, never IB Gateway or
    an IBKR password). After migration 016 is on the host:
    * Re-run `db/roles/market_intelligence_readonly.sql` (new `mi_v_ibkr_*` grants).
    * Create `mi_ibkr_ingest` (not `quantuser` / admin): `psql -d quant_monitor -v DBNAME=quant_monitor -v ingest_password="$(cat /etc/fmp/mi_ibkr_ingest.pw)" -f db/roles/ibkr_ingest.sql`.
    * Copy `deploy/market_intelligence/ibkr_ingest.env.example` to `/etc/fmp/ibkr_ingest.env` (0600);
      set `IBKR_INGEST_TOKEN` from Windows Credential Manager target `FMP_SCREENER/ibkr-ingest` and
      `IBKR_INGEST_DATABASE_URL` for `mi_ibkr_ingest@127.0.0.1/quant_monitor`. Bind
      `IBKR_INGEST_HOST=100.91.192.77` `IBKR_INGEST_PORT=8771` (Tailscale only; TWS stays localhost).
    * `scripts/install_ibkr_ingest.sh --apply`. Deploy.yml restarts this unit only if it already exists.
    * On the Windows collector host, from the repo with the collector venv:
      `python -m ibkr_collector install` then `start` / `stop` / `status` / `uninstall`.
      Uninstall removes the logon task only; PostgreSQL rows stay.
    Data Health (`pages/15_Data_Health`) shows heartbeat age from the database clock
    (`COLLECTOR_OFFLINE` if heartbeats are older than 90s). Closing TWS does not delete stored quotes.

Rollback / recovery (canonical data preserved): `systemctl disable --now fmp-mi-refresh.timer`
and `fmp-ai-context-api.service`; revert the merge if needed. Migrations are additive: no table,
column or view is dropped, so no data is lost and re-applying is a no-op. Quarantined payloads stay
in `mi_macro_observation_quarantine` for diagnosis. Superseded morning snapshots remain readable via
`mi_v_morning_context_index`. `systemctl disable --now fmp-ibkr-ingest.service` stops private ingest
without deleting `mi_market_quotes` / `mi_collector_status`.

## Verification procedure (what the tests and CI do; repeatable by an operator)

* Migrations: clean apply, upgrade from a schema at 011 with reviewed-head rows, then a second
  no-op apply (`tests/test_mi_schema_store.py`, CI step "Clean-database migration apply").
* Read-only role: real role created through `db/roles/*.sql` via psql with a test-unique name;
  SELECT on views passes, raw tables / writes / CREATE / research tables are `permission denied`
  even after `SET default_transaction_read_only = off`; PUBLIC CREATE remediation documented.
* HTTP: envelope hash verifies on the raw response JSON; tampering fails; missing snapshot yields
  `available = false` with a reason and still a valid hash; unauthenticated `/v1` is 401; `/health`
  minimal; readiness fails when a required view is unreadable.
* Freshness: clock advanced without ingestion turns FRESH into STALE per cadence; partial source
  failures isolate; empty DB / catalog-only states render.
* Legacy serialisation: fixtures emitted by the real `nightly_refresh` serialiser layout, NaN
  handling, stale instruments, quarantined labels, hash on the stored canonical body.
* Ideas: approval bound to version + hash; INCOMPLETE refused; stricter idea holdout flows into
  the contract; QS refuses tampered/looser/v1 contracts.
* Bonds: bracket/convergence/repricing checks; the 5% 2025-01-01 bond at dirty 1.00 returns a
  bounded YTM (no 200% artefact), and impossible prices return `UNSUPPORTED_*` statuses.
* PIT sector: validation matrix (hash, schema, units, definitions, PIT flags, constituent keys,
  boundary/window/date gate, ranges), ingest/no-op/revision/rejection, read model, CLI exit codes,
  page render on populated/empty/unconfigured states.
* Timers: `systemd-analyze calendar` for EST/EDT transitions and weekends; `systemd-analyze verify`
  on rendered units; no credential on any `ExecStart` line.

## Boundaries preserved

* No Stage 1 / Stage 2 rerun; no changes to `stage1_results/`, `stage2_results/`,
  `qc_research/platform_artifacts/`, QC ids, economic fingerprints (TLT `d8f43c83ddec8d70`), or
  any QS strategy tree (hash diff recorded in the checkpoint).
* No 2025+ or final-holdout access anywhere in Path 2: spec validation, contract, manifest,
  producer and consumer all enforce `< effective_holdout_start <= 2025-01-01`.
* No model binaries in GitHub, PostgreSQL, or FMP. No constituent-level licensed data leaves QS.
* Writer identity only in backend jobs; pages and API use the read-only role with no fallback.
* No QuantConnect backtest was launched by this work; CI never carries QC/provider/DB secrets.

## Known limits

* Real FRED validation is a **manually dispatched** GitHub Actions workflow
  (`fred_validation.yml`) that receives `secrets.FRED_API_KEY`. It is not on `main` until the
  minimal workflow PR is merged. Ordinary PR tests remain secretless. DigitalOcean secret
  provisioning is a separate dry-run-default script and does not activate production.
* Legacy bridge parity was validated against fixtures shaped by the real serialiser; the production
  bundles may carry labels that land in quarantine (visible on Sector Rotation V2, health PARTIAL).
* `sector_internals_v1` has only been produced from synthetic PIT fixtures. A QuantConnect research
  project that exports real PIT membership/prices/caps for the producer is NOT_IMPLEMENTED; nothing
  in this milestone activates QC.
* Bond tables have no ingestion source; `jobs.bond_analytics` reports every bond as a skip.
* Holiday calendar covers US federal holidays only.
