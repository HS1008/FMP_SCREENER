# Market Intelligence AI Gateway

Read-only REST + MCP access to the same PostgreSQL views used by the Streamlit
dashboard. This is not a ChatGPT-specific product; ChatGPT, Cursor, and other
clients share one service layer.

```
External providers -> ingestion -> PostgreSQL (canonical)
                                   /            \
                            Streamlit            AI gateway
                            read-only            read-only
                                                 REST + MCP
                                                 ChatGPT / Cursor / future clients
```

Operator reference for the rest of Market Intelligence:
[`MARKET_INTELLIGENCE.md`](MARKET_INTELLIGENCE.md).

## Architecture

* Canonical data stays in PostgreSQL. The gateway does not copy it into an
  AI-specific database and does not scrape Streamlit.
* The dashboard remains read-only. The gateway is also read-only.
* Existing `mi_v_*` views are the AI-facing schemas. Redundant `ai_*` views
  were not added.
* The production database identity is `mi_readonly` (the requested AI reader
  capability). It can `SELECT` curated views only.
* Frozen morning snapshots remain on `/v1/context/*` (`ai_context_api.py`).
  Live semantic tools live on `/api/v1/*` and MCP. Both share
  `market_intelligence.read_models`.

## Threat model

| Threat | Control |
|---|---|
| Arbitrary SQL | No SQL tool. Parameterized service queries only |
| Database writes | `mi_readonly` grants + `SET TRANSACTION READ ONLY` + no write routes |
| Public PostgreSQL | Gateway binds `127.0.0.1`. Postgres is not proxied |
| Anonymous data access | Fail-closed bearer / OAuth. `/health` is liveness only |
| Secret leakage | Tokens never logged; export policy strips credential keys |
| Licensed redistribution | `export_mode=external` redacts ICE/FMP values; `owner` is authenticated owner-only |
| Research holdout | Stage 2 windows ending 2025-01-01+ and holdout test types are filtered |
| Execution | No order, backtest, promotion, or brokerage tools exist |
| Over-fetch | History/list caps, statement timeout, response size limit, rate limit |

## Intentionally unsupported

```
NO database writes
NO arbitrary SQL
NO QuantConnect backtest launches
NO model promotion
NO trade execution
NO Stage 2 final holdout
NO model.pkl / model binaries
NO live FMP API calls from the gateway
```

## MCP tools

| Tool | Purpose |
|---|---|
| `get_morning_context` | Compact frozen morning snapshot + live stale list |
| `get_market_pulse` | Sector 1D leadership, rates, credit identity, IBKR quotes if stored |
| `get_rates_curve` | Treasury tenors and slopes with FRED provenance |
| `get_macro_overview` | Canonical macro catalog latest values |
| `get_macro_series` | Bounded history for one catalog `series_id` |
| `get_credit_overview` | IG/HY/rating OAS (owner mode includes values) |
| `get_sector_rotation` | Sector ETF RS vs SPY including 1D absolute return |
| `get_sector_detail` | One sector plus industries |
| `get_industry_rotation` | Industry RS vs parent sector ETF |
| `get_subindustry_rotation` | Honest `DATA_NOT_AVAILABLE`; industry is the current leaf |
| `get_order_flow` | FINRA TRACE aggregates; not an order book |
| `get_strategy_summary` | Ingested Stage 1/2 status. COMPLETE ≠ PASS |
| `get_strategy_oos_windows` | Non-holdout OOS windows |
| `get_strategy_experiments` | Non-holdout experiments; artifact SHA only |
| `get_data_health` | Cadence-aware freshness / stale list |
| `get_market_changes` | Factual session and multi-horizon changes |

## REST endpoints

Authenticated unless noted.

| Method | Path | Tool |
|---|---|---|
| GET | `/health` | liveness (no auth) |
| GET | `/ready` and `/v1/ready` | DB view probe |
| GET | `/api/v1/context/morning` | `get_morning_context` |
| GET | `/api/v1/markets/pulse` | `get_market_pulse` |
| GET | `/api/v1/rates` | `get_rates_curve` |
| GET | `/api/v1/macro` | `get_macro_overview` |
| GET | `/api/v1/macro/{series_id}` | `get_macro_series` |
| GET | `/api/v1/credit` | `get_credit_overview` |
| GET | `/api/v1/sectors` | `get_sector_rotation` |
| GET | `/api/v1/sectors/{sector}` | `get_sector_detail` |
| GET | `/api/v1/industries` | `get_industry_rotation` |
| GET | `/api/v1/subindustries` | `get_subindustry_rotation` |
| GET | `/api/v1/order-flow` | `get_order_flow` |
| GET | `/api/v1/strategies` | `get_strategy_summary` |
| GET | `/api/v1/strategies/{strategy}` | `get_strategy_summary` |
| GET | `/api/v1/strategies/{strategy}/oos` | `get_strategy_oos_windows` |
| GET | `/api/v1/strategies/{strategy}/experiments` | `get_strategy_experiments` |
| GET | `/api/v1/data-health` | `get_data_health` |
| GET | `/api/v1/changes` | `get_market_changes` |
| GET | `/api/v1/tools` | tool catalog |
| GET | `/api/v1/openapi.json` | schema for Custom GPT setup (no data) |
| POST | `/mcp` | MCP Streamable HTTP |

Legacy frozen-snapshot routes on `/v1/context/*` are unchanged.

## Authentication

1. **Bearer token** — `Authorization: Bearer $AI_CONTEXT_API_TOKEN`
2. **OAuth 2.1 + PKCE** for ChatGPT / MCP discovery:
   * `/.well-known/oauth-protected-resource`
   * `/.well-known/oauth-authorization-server`
   * `/oauth/register` (dynamic client registration)
   * `/oauth/authorize` (owner pastes the same API token)
   * `/oauth/token`

The server fails closed (503) if `AI_CONTEXT_API_TOKEN` is unset.

Rotate the token by writing a new value to
`/root/FMP_SCREENER/.secrets/ai_context_api_token` and
`/etc/fmp/ai_context_api.env`, then
`systemctl restart fmp-ai-context-api`. Never put the token in git.

## Environment variables

Reused:

* `DATABASE_READONLY_URL`
* `AI_CONTEXT_API_TOKEN`
* `AI_CONTEXT_API_HOST` (default `127.0.0.1`)
* `AI_CONTEXT_API_PORT` (default `8765`)

Gateway-only:

* `AI_GATEWAY_PUBLIC_BASE_URL` — public HTTPS origin used in OAuth metadata
* `AI_GATEWAY_EXPORT_MODE` — `owner` (default) or `external`
* `AI_GATEWAY_RATE_LIMIT_PER_MINUTE` — default `60`
* `AI_GATEWAY_CORS_ORIGINS` — empty disables CORS
* `AI_GATEWAY_OAUTH_ENABLED` — default `1`

## Local development

```bash
export AI_CONTEXT_API_TOKEN=dev-token
export DATABASE_READONLY_URL=postgresql://mi_readonly:...@127.0.0.1:5432/fmp
python -m ai_gateway --host 127.0.0.1 --port 8765
# or
uvicorn ai_context_api:app --host 127.0.0.1 --port 8765
```

Local MCP stdio (Cursor):

```bash
python -m ai_gateway --stdio
```

Cursor MCP config example:

```json
{
  "mcpServers": {
    "fmp-market-intelligence": {
      "command": "python",
      "args": ["-m", "ai_gateway", "--stdio"],
      "env": {
        "AI_CONTEXT_API_TOKEN": "dev-token",
        "DATABASE_READONLY_URL": "postgresql://mi_readonly:...@127.0.0.1:5432/fmp"
      }
    }
  }
}
```

## Production deployment

The gateway is the existing `fmp-ai-context-api.service` (localhost:8765).
`deploy.yml` restarts that unit when it is installed.

1. Apply migrations (`020_ai_gateway_strategy_views.sql` is additive).
2. Refresh `mi_readonly` grants:
   `psql "$ADMIN_DATABASE_URL" -f db/roles/market_intelligence_readonly.sql`
3. Confirm the API env file contains only the allowlisted keys
   (`scripts/materialize_ai_context_env.py`).
4. Keep PostgreSQL on loopback / private network.
5. If ChatGPT needs a public URL, front **only** the gateway with HTTPS
   (`deploy/market_intelligence/nginx-ai-gateway.conf.example`).
6. Set `AI_GATEWAY_PUBLIC_BASE_URL=https://<your-host>`.

Disable external access: stop `fmp-ai-context-api`, or leave it on localhost
and do not publish a reverse proxy.

Verify PostgreSQL is not public:

```bash
ss -ltn | awk '$4 ~ /:5432/ {print}'
# expect 127.0.0.1:5432 or a private address, never 0.0.0.0:5432
```

## ChatGPT connection

Backend status after a public HTTPS proxy is in place: ready for a manual
ChatGPT account action.

**Option A — ChatGPT Developer Mode / custom MCP**

1. Create a public HTTPS hostname that proxies to `127.0.0.1:8765`.
2. Set `AI_GATEWAY_PUBLIC_BASE_URL` to that origin and restart the unit.
3. In ChatGPT: enable developer / custom MCP connectors.
4. Add server:
   * Name: `FMP Market Intelligence`
   * MCP URL: `https://<host>/mcp`
5. Complete the OAuth prompt by pasting the owner API token
   (from `/etc/fmp/ai_context_api.env` on the host — do not commit it).
6. Approve read-only tools.
7. Test: “Call get_data_health and tell me which sources are stale.”

**Option B — Custom GPT Actions**

Use `https://<host>/api/v1/openapi.json` plus Bearer
`AI_CONTEXT_API_TOKEN`.

## Data sources and freshness

| Domain | Canonical source | Fallback | Freshness | FMP? |
|---|---|---|---|---|
| Market pulse | Sector ETF snapshots + IBKR quotes + FRED rates | none | business-day daily | Sector values may be `FMP_LEGACY` stored bundles |
| Rates | FRED `DGS*` | none | daily, business-day | no |
| Macro | FRED catalog `fred_catalog_v2` | none | cadence-aware | no |
| Credit | ICE BofA via FRED | none | daily | no |
| Sector | `mi_v_sector_latest` | none | daily | stored `FMP_LEGACY` only; no live FMP call |
| Industry | `mi_v_industry_latest` | none | daily | stored `FMP_LEGACY` only |
| Subindustry | not in canonical store | industry leaf | n/a | not reintroduced |
| TRACE / order flow | FINRA Query API aggregates | none | daily | no |
| Strategies | `research_runs` / holdout-filtered views | none | on ingest | no |

FiscalData / Treasury.gov is **not implemented**. FRED is the current
Treasury producer. Returned payloads say so.

Daily sources are not marked stale solely because of a weekend or US federal
holiday (`freshness_policy_v1`).

## Testing

```bash
python -m pytest -q tests/test_ai_gateway.py tests/test_mi_pipeline.py tests/test_stage1_research.py tests/test_stage2_ml_research.py
```

CI (`pr_validation.yml`) runs the full offline + disposable PostgreSQL suite.
It does not use live secrets.

## Troubleshooting

* `503 server token not configured` — `AI_CONTEXT_API_TOKEN` is empty.
* `503 read-only database unavailable` — `DATABASE_READONLY_URL` missing or
  `mi_readonly` cannot see required views. Re-run the role SQL after
  migrations.
* Sector/industry values missing in `/v1/context/sectors/latest` — that legacy
  route uses `export_mode=external`. Use `/api/v1/sectors` (owner mode).
* ChatGPT cannot discover tools — confirm `/mcp` is reachable on HTTPS and
  OAuth metadata is served from `AI_GATEWAY_PUBLIC_BASE_URL`.
* Stale daily series on Saturday — expected only if the last business-day
  observation is older than the cadence tolerance, not merely because today
  is a weekend.
