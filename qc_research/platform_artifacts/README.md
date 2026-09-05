Proven REAL_QC platform artifacts for deployable Strategy Monitor ingest.

`ml_discovery_qqq.json` is the licensed QuantConnect ML_DISCOVERY infrastructure
proof (`ed9f39b897d569d90edd0939626e7286` / `faeb37c893642e17912921dc23992e0c`).
It is not an economic PASS. Do not retune it from OOS.

Ingest when `DATABASE_URL` is available:

```
python -m qc_research.ingest_platform_artifacts --verify-monitor
```

Missing database credentials skip with exit 0. Do not invent credentials.
