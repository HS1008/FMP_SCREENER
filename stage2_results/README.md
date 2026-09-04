# Stage 2 results import tree

QuantConnect Object Store export is unavailable on this account.
Canonical Stage 2 JSON is published to the private `stage2-results` branch.
This public tree is the copy DigitalOcean can ingest without a new secret:

    stage2_results/<strategy_id>/<research_run_id>/run_manifest.json
    stage2_results/<strategy_id>/<research_run_id>/run_summary.json
    stage2_results/<strategy_id>/<research_run_id>/<window>/training_summary.json
    stage2_results/<strategy_id>/<research_run_id>/<window>/model_metadata.json
    stage2_results/<strategy_id>/<research_run_id>/<window>/oos_diagnostics.json
    stage2_results/<strategy_id>/<research_run_id>/<window>/baseline_oos_diagnostics.json

JSON only. Never `model.pkl`.
