Proven REAL_QC platform artifacts for deployable Strategy Monitor ingest.

`ml_discovery_qqq.json` is the licensed QuantConnect ML_DISCOVERY infrastructure
proof (`ed9f39b897d569d90edd0939626e7286` / `faeb37c893642e17912921dc23992e0c`).
It is not an economic PASS. Do not retune it from OOS.

`ml_cloud_train_qqq.json` is the QC-cloud-first ML_TRAIN infrastructure proof
(`a63cb5082b1089599a2519fe0fdfb324` / `8394f03405ed86095bf9527d65f9b4b0` /
`3bbb5deee1950c77c793fdf9e47e77a9`). Training ran inside QuantConnect Cloud.
`data_read_used` is false. It is not an economic PASS.

`ml_cloud_train_zn.json` is the ZN AddFuture cloud ML_TRAIN infrastructure proof
(`e50648ec34036c247420a07d8cd5fb76` / `d84daf842ce013f1aa9a9b1010e4ebbd` /
`478351b0cf8954bb2c1be4e5d066c2f0`). Cost model is `US_FUTURES_TICKS_V1`.
`data_read_used` is false. Negative short-window Sharpe is not an economic PASS.

`ml_ridge_transport.json` is the Ridge-fixed learned-model transport proof
(`db10b77fb65ff64fe764c25025a5ff52` / `448701c68fe33bd31e8069f2202a1d8f` /
`9f5f3ab464c7e77ef51854f2f3b0ac73`). Ridge was fixed by design, not selected
from OOS. `data_read_used` is false. OOS Sharpe 2.134 vs baseline 2.297 is not
an economic PASS and must not retune the search space.

Ingest when `DATABASE_URL` is available:

```
python -m qc_research.ingest_platform_artifacts --verify-monitor
```

Missing database credentials skip with exit 0. Do not invent credentials.
