# Platform research (FMP_SCREENER)

PostgreSQL + Strategy Monitor half of the multi-asset research platform.

- Streamlit remains **read-only** (no QC, no training, no orders).
- Migration `005_platform_research.sql` is additive (`IF NOT EXISTS`).
- Stage 1 tables are unchanged.
- CrossSectionalFactorML V1 published JSON remains historical evidence.
- Missing metrics render as **Unavailable / Not applicable**, never 0.
- `SYNTHETIC_TEST_ONLY` artifacts are rejected at ingest.
- Reconstructed max drawdown is labeled monthly-sampled, not QuantConnect Max Drawdown.

See quant-strategies `research/PLATFORM.md` for the full architecture.

Local QuantConnect `/data/read` dataset download is optional and is not
required for normal cloud ML research. Ingest never downloads Object Store
model binaries. Provenance labels: `REAL_QC`, `LOCAL_LICENSED`, `LOCAL_TEST`,
`UNAVAILABLE`.

## Deployable ingest

When `DATABASE_URL` or `DB_HOST`/`DB_NAME`/`DB_USER` is available in an authorized
environment, ingest the already-proven REAL_QC artifacts and verify the
Strategy Monitor read model:

```
python -m qc_research.ingest_platform_artifacts --verify-monitor
```

`--dry-run` validates and wraps smoke records without PostgreSQL. If credentials
are unset, the command and `ingest_platform_research.yml` workflow skip with
exit 0. Do not invent a database URL. Unit tests use FakeConn.

