# Market Intelligence AI Gateway

Read-only REST + MCP access to the **same PostgreSQL views** used by the Streamlit dashboard.
ChatGPT, Cursor, Claude Desktop and future clients share one service layer
(`ai_gateway/services.py`). Nothing here ingests, trades, launches QuantConnect jobs, trains,
promotes, or opens the Stage 2 final holdout.

```
QuantConnect / research      Treasury XML   FRED   FINRA   equity EOD adapter
        │                          │          │      │            │
        └──── canonical artifacts ─┴── scheduled ingestion (fmp-mi-refresh) ──┘
                                   │
                            PostgreSQL (canonical)
                          ┌────────┴────────┐
                     Streamlit          AI gateway (this)
                     read-only          read-only REST /api/v1 + MCP /mcp
                     dashboard_readonly mi_readonly
```

Operator reference for the rest of Market Intelligence: [`MARKET_INTELLIGENCE.md`](MARKET_INTELLIGENCE.md).
Source status: [`SOURCE_REPLACEMENT_MATRIX.md`](SOURCE_REPLACEMENT_MATRIX.md).

## Architecture

* Canonical data stays in PostgreSQL. The gateway does not copy it into an AI database and
  never scrapes Streamlit.
* Both consumers are read-only. The gateway uses the `mi_readonly` role (curated `mi_v_*`
  views only), one `READ ONLY` transaction per request, statement timeout, bounded rows.
* Frozen morning snapshots stay on `/v1/context/*` (`ai_context_api.py`). Live semantic tools
  live on `/api/v1/*` and MCP `/mcp`. Both are mounted in the same `fmp-ai-context-api` unit.
* Research views come from **migration 027** (`027_ai_gateway_strategy_views.sql`) and the
  fail-closed run proof in **migration 028** (`028_gateway_holdout_failclosed.sql`). The
  concurrent gateway PR's `020_…` number belonged to the deploy-hardening lineage and was not
  reused; applied SQL is never renumbered.

## Data path the gateway describes

| Tool | Canonical source | Notes |
|---|---|---|
| `get_rates_curve` | Treasury Daily Par Yield Curve XML (`TREASURY`) preferred by observation date; FRED `DGS*/DFII*` fallback per leg | `complete_curve_date` is the latest date with every required tenor; `partial_newer` lists newer tenors separately; slopes use same-date legs; `derived_nominal_minus_real` is labelled derived and is not `T5YIE/T10YIE` |
| `get_macro_overview` / `get_macro_series` | FRED (macro) + `UST_*` Treasury series | publication-aware freshness v2 |
| `get_credit_overview` | ICE BofA via FRED | `RESTRICTED_REDISTRIBUTION`: identity/dates/status only for remote clients |
| `get_sector_rotation` / `get_sector_detail` | `EQUITY_EOD` adapter (independent daily bars) → `ret_1d`, `rs_1d`, 1W…12M | aligned sessions only, missing ≠ zero, provider provenance and explicit `UNAVAILABLE` when no entitled provider is configured; `FMP_LEGACY` rows are labelled, never a live FMP call |
| `get_industry_rotation` | ETF comparisons vs sector ETF (SMH, XSD, KRE, XBI, XOP, XRT) | current-context hierarchy, not GICS |
| `get_subindustry_rotation` | Curated equal-dollar baskets (semiconductor internals, themes) | `official_gics_subindustry=false`; sectors without a defensible mapping are `SUBGROUP_UNAVAILABLE` |
| `get_order_flow` | FINRA Query API aggregates | not an order book |
| `get_strategy_*` | migrations 027 + 028 views | holdout fail-closed (below) |
| `get_data_health` | `mi_v_source_health` | freshness v2 vocabulary, observation vs ingestion timestamps, transport status |

## Threat model

| Threat | Control |
|---|---|
| Arbitrary SQL / table / path | No SQL tool; parameterized service queries over named views; series ids and sectors validated against allowlists |
| Database writes | `mi_readonly` grants (views only) + `SET TRANSACTION READ ONLY`; POST is accepted only on `/mcp` (JSON-RPC) and `/oauth/*` |
| Public PostgreSQL | Gateway binds `127.0.0.1`; Postgres, TWS/IBKR and the writer API are never proxied |
| Anonymous access | Fail-closed bearer / OAuth access token; `/health` is liveness only |
| Secret leakage | Tokens/URLs never logged (redaction), export policy strips credential keys, model binaries and object-store keys |
| Licensed redistribution | **Remote sessions always get `external`**: `INTERNAL_ONLY` and `RESTRICTED_REDISTRIBUTION` values are redacted, unknown scope fails closed. `owner` applies only to a proven local session. Source-specific remote rights are an explicit operator allowlist |
| Research holdout | SQL views + Python double filter + adversarial tests (below) |
| Execution / research launch | No order, backtest, compile, promotion, holdout or brokerage capability exists; the dispatcher refuses the names and `register()` refuses to bind them |
| Over-fetch / abuse | history/list caps, statement timeout, 64 KiB request cap, 400 000-char response cap, per-identity rate limit |
| CORS | disabled unless origins are listed; `*` is never honoured |

## Export policy: remote vs local owner

Authentication proves *who* is asking. It does not prove that a provider licence permits
transmitting stored values from our server to a third-party AI service. Therefore:

* `AI_GATEWAY_EXPORT_MODE=external` (default) — every session receives the restrictive policy.
* `AI_GATEWAY_EXPORT_MODE=owner` — permission for **local** sessions only: stdio MCP
  (`python -m ai_gateway --stdio`) or a **direct** HTTP client on loopback whose `Host` is
  loopback, with **no** reverse-proxy headers (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`,
  `X-Forwarded-Host`, `X-Forwarded-Proto`, `Via`) while the gateway is bound to loopback.
  nginx on the same host forwards remote clients from 127.0.0.1 *with* those headers and a
  public `Host`; those requests are always remote. `AI_GATEWAY_TRUST_PROXY=0` cannot restore
  owner mode. A non-loopback bind refuses owner mode entirely. Cache entries are partitioned
  by the effective mode.
* `AI_GATEWAY_REMOTE_VALUE_SOURCES=` — comma-separated `source_id`s whose `INTERNAL_ONLY` /
  `RESTRICTED_REDISTRIBUTION` values a human has confirmed may be transmitted remotely under the
  provider's terms. Empty by default. This is the only mechanism for exposing such values to a
  remote client; providers are never relabelled `PUBLIC` to make redaction disappear.

Every envelope records `export_mode`, `owner_session`, `remote_value_sources`,
`restricted_entries`, and `export_sha256` over the delivered JSON.

Current remote posture with the shipped registry: Treasury and public FRED series carry
values; equity-EOD derived sector/industry/subgroup values, ICE BofA credit values and FINRA
aggregates are identity/date/status only until an entitlement decision is recorded.

## Holdout fail-closed (defense in depth)

1. **SQL** (`027_…` plus `028_…`): a run is exposed only when holdout access is **proven**
   (`holdout_accessed IS FALSE`, `holdout_status` is `LOCKED` for Stage 2 / `LOCKED` or
   `EXPOSED_PRIOR_TO_STAGE1` for Stage 1, and Stage 2 `holdout_exposure_status` is
   `PRISTINE`/`NEVER_ACCESSED`). NULL or unknown run-level flags are hidden. An
   experiment needs `research_is_holdout IS FALSE` (NULL = unknown = hidden), a known test type
   without `HOLDOUT`, and a **known** `test_end < 2025-01-01`. An artifact must be non-model
   (type/path/key/transport) and bound to a visible experiment, or run-scoped with *every*
   experiment of the run visible; runs with zero experiments or unknown lineage are hidden;
   `payload_json` is never selected.
2. **Gateway** (`ai_gateway/services.py`): `_holdout_blocked` and `_artifact_blocked` re-check
   every row; `holdout_excluded=true` is emitted only after both layers ran.
3. **Tests** (`tests/test_ai_gateway_db.py`): adversarial rows — `diagnostics` belonging to
   `ML_FINAL_HOLDOUT`, NULL test dates in a `HOLDOUT` phase, 2025+ OOS, `model.pkl` behind an
   innocuous type, `weights.pkl` behind an innocuous path, unknown Stage 2 lineage, NULL
   `research_is_holdout` — assert that only the pre-2025 non-holdout class escapes and that no
   holdout value or identifier appears in any response.

The read-only role cannot read `backtests`, `research_artifacts` or `research_oos_windows`
directly (SQLSTATE 42501), so there is no path around the views.

## MCP tools

`get_morning_context`, `get_market_pulse`, `get_rates_curve`, `get_macro_overview`,
`get_macro_series`, `get_credit_overview`, `get_sector_rotation`, `get_sector_detail`,
`get_industry_rotation`, `get_subindustry_rotation`, `get_order_flow`,
`get_strategy_summary`, `get_strategy_oos_windows`, `get_strategy_experiments`,
`get_data_health`, `get_market_changes`.

Forbidden names with **no implementation path**: `execute_sql`, `query_database`, `run_sql`,
`place_order`, `execute_trade`, `rebalance_portfolio`, `submit_order`, `connect_ibkr`,
`launch_backtest`, `create_backtest`, `compile_quantconnect`, `promote_model`,
`promote_strategy`, `run_holdout`, `open_holdout`, `ml_final_holdout`, `download_model`,
`get_model_binary`.

## REST endpoints

All under `/api/v1`, bearer required: `context/morning`, `markets/pulse`, `rates`, `macro`,
`macro/{series_id}`, `credit`, `sectors`, `sectors/{sector}`, `industries`, `subindustries`,
`order-flow`, `strategies`, `strategies/{strategy}`, `strategies/{strategy}/oos`,
`strategies/{strategy}/experiments`, `data-health`, `changes`, `tools`. `/api/v1/openapi.json`
is unauthenticated schema only. `/ready` (authenticated) reports the DB probe and the export
policy the caller would receive.

## Authentication

* Static owner bearer: `Authorization: Bearer $AI_CONTEXT_API_TOKEN`.
* OAuth 2.1 authorization code + PKCE (S256) for ChatGPT / MCP clients: dynamic registration
  at `/oauth/register`, `/oauth/authorize` asks the owner for the API token, `/oauth/token`
  mints an HS256 access token whose audience is **this gateway's `/mcp` resource**; a foreign
  `resource` is refused (`invalid_target`). Access tokens expire after 1 hour. Metadata:
  `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource`.

## Environment variables

See `deploy/market_intelligence/ai_context_api.env.example`. Only `DATABASE_READONLY_URL`
(the `mi_readonly` role) is ever loaded — the unit never sees the writer URL.

## Local development

```bash
export DATABASE_READONLY_URL=postgresql://mi_readonly:...@127.0.0.1:5432/quant_monitor
export AI_CONTEXT_API_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
python -m ai_gateway                 # REST + MCP on 127.0.0.1:8765 (external policy)
python -m ai_gateway --stdio         # local stdio MCP for Cursor / Claude Desktop
AI_GATEWAY_EXPORT_MODE=owner python -m ai_gateway --stdio   # owner values, local only
```

## Production deployment (private first)

1. Merge and deploy the release; `scripts/deploy_host.sh` applies migrations 027 and 028 once from the
   staged SHA and restarts `fmp-ai-context-api.service` when the unit exists.
2. Refresh grants: `psql "$ADMIN_DATABASE_URL" -f db/roles/market_intelligence_readonly.sql`
   (idempotent; adds the four gateway research views).
3. Verify privately on the host:

```bash
curl -s http://127.0.0.1:8765/health                                   # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/v1/rates          # 401
curl -s -H "Authorization: Bearer $AI_CONTEXT_API_TOKEN" http://127.0.0.1:8765/ready  # ready:true, export_policy
curl -s -H "Authorization: Bearer $AI_CONTEXT_API_TOKEN" http://127.0.0.1:8765/api/v1/rates | jq '.body.complete_curve_date, .body.legs_by_source'
curl -s -H "Authorization: Bearer $AI_CONTEXT_API_TOKEN" http://127.0.0.1:8765/api/v1/strategies/CrossSectionalFactorML/experiments | jq '.body.holdout_filter'
ss -ltnp | grep -E '8765|5432'                                        # both loopback only
```

Postgres stays on loopback/private; only the gateway is fronted by nginx.

## ChatGPT connection (after the private checks pass)

1. Point a DNS name (e.g. `mcp.example.com`) at the droplet, obtain a TLS certificate, and
   install `deploy/market_intelligence/nginx-ai-gateway.conf.example` (HTTPS only, 64k body
   cap, proxies to `127.0.0.1:8765`, forwards `X-Forwarded-For`).
2. Set `AI_GATEWAY_PUBLIC_BASE_URL=https://mcp.example.com` in `/etc/fmp/ai_context_api.env`;
   keep `AI_GATEWAY_EXPORT_MODE=external`; restart the unit.
3. Verify from outside: `curl https://mcp.example.com/.well-known/oauth-authorization-server`
   returns the metadata and `curl -X POST https://mcp.example.com/mcp` returns 401.
4. In ChatGPT: Settings → Connectors (or a Custom GPT → Actions) → add MCP server with URL
   `https://mcp.example.com/mcp`, authentication **OAuth**. ChatGPT registers dynamically, opens
   `/oauth/authorize`; enter the owner API token once; the access token it receives is bound to
   `https://mcp.example.com/mcp` and expires hourly.
5. Confirm in the audit log (`journalctl -u fmp-ai-context-api`) that calls arrive with
   `export_mode=external`, `transport=http`, and that `tools/list` shows only the semantic tools.

Do not connect ChatGPT while the gateway is still loopback-only; do not expose port 5432.

## Testing

```bash
python -m pytest -q tests/test_ai_gateway.py                # unit, no DB
FMP_TEST_DATABASE_URL=... python -m pytest -q tests/test_ai_gateway_db.py tests/test_migration_paths_pg.py
```
