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
