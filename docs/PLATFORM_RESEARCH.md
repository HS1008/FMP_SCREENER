# Platform research (FMP_SCREENER)

PostgreSQL + Strategy Monitor half of the multi-asset research platform.

```
Cursor thesis → frozen non-holdout spec → QC research → canonical artifact
    → quant-strategies publish_platform_research.yml
    → FMP repository_dispatch platform-research-ingest
    → authorized DigitalOcean / PostgreSQL
    → Strategy Monitor
```

- Streamlit remains **read-only** (no QC, no training, no orders).
- Migrations `005_platform_research.sql` and `006_platform_lifecycle.sql` are additive (`IF NOT EXISTS`).
- Stage 1 tables are unchanged.
- CrossSectionalFactorML V1 published JSON remains historical evidence.
- Missing metrics render as **Unavailable / Not applicable**, never 0.
- `SYNTHETIC_TEST_ONLY` artifacts are rejected at ingest.
- Reconstructed max drawdown is labeled monthly-sampled, not QuantConnect Max Drawdown.
- `research_status=COMPLETE` is the research terminal. `HUMAN_REVIEW_REQUIRED` is the **promotion** gate.
- `economic_gate` stays `NOT_DEFINED` until a human defines thresholds.
- Results stay visible regardless of `promotion_gate`.
- Paper / live / IBKR / holdout / model promotion stay locked until explicit human authorization.

See quant-strategies `research/PLATFORM.md` for the full architecture.

A canonical artifact with `strategy_id`, lineage, `research_kind`, family,
asset class, research mode, run status, artifacts, and OOS windows is enough
for Strategy Monitor to discover and render the strategy. Do not add Streamlit
code per strategy.

Local QuantConnect `/data/read` dataset download is optional and is not
required for normal cloud ML research. Ingest never downloads Object Store
model binaries. Provenance labels: `REAL_QC`, `LOCAL_LICENSED`, `LOCAL_TEST`,
`UNAVAILABLE`.

## Deployable ingest

One generic workflow: `.github/workflows/ingest_platform_research.yml`.

Normal path is `repository_dispatch` event `platform-research-ingest` from
quant-strategies after a complete canonical artifact is published. Live writes
use the authorized DigitalOcean host and droplet `.env` / `DB_*`. Never commit
database credentials. Do not add a workflow per strategy.
`ingest_tlt_duration_momentum.yml` is a superseded manual override.

When `DATABASE_URL` or `DB_HOST`/`DB_NAME`/`DB_USER` is available in an authorized
environment, ingest already-proven REAL_QC artifacts and verify the
Strategy Monitor read model:

```
python -m qc_research.ingest_platform_artifacts --verify-monitor --canonical-only
```

`--dry-run` validates and wraps records without PostgreSQL. If credentials
are unset, local CLI ingest skips with exit 0. Do not invent a database URL.
Unit tests use FakeConn.

TLTDurationMomentum V0 uses the official 10-window artifact
`qc_research/platform_artifacts/tlt_duration_momentum.json`. Ingest wraps it
into `run_summary` / `oos_aggregate` / `trials` / `experiment_manifest` with
`research_kind=platform_research`, `research_status=COMPLETE`,
`economic_gate=NOT_DEFINED`, `promotion_gate=HUMAN_REVIEW_REQUIRED`,
`holdout_status=LOCKED`, `economic_pass=NULL`, and `holdout_accessed=false`.
Do not launch QuantConnect and do not alter the frozen TLT V0 IDs.

