Proven REAL_QC platform artifacts for deployable Strategy Monitor ingest.

`ml_discovery_qqq.json` is the licensed QuantConnect ML_DISCOVERY infrastructure
proof (`ed9f39b897d569d90edd0939626e7286` / `faeb37c893642e17912921dc23992e0c`).
It is not an economic PASS. Do not retune it from OOS.

`ml_cloud_train_qqq.json` is the QC-cloud-first ML_TRAIN infrastructure proof
(`a63cb5082b1089599a2519fe0fdfb324` / `8394f03405ed86095bf9527d65f9b4b0` /
`3bbb5deee1950c77c793fdf9e47e77a9`). Training ran inside QuantConnect Cloud.
`data_read_used` is false. It is not an economic PASS.

Ingest when `DATABASE_URL` is available:

```
python -m qc_research.ingest_platform_artifacts --verify-monitor
```

Missing database credentials skip with exit 0. Do not invent credentials.
