#!/usr/bin/env bash
# Backward-compatible wrapper. Official TLT ingest now uses the generic path.
set -euo pipefail
ROOT="${1:-/tmp/fmp-platform-ingest}"
export STRATEGY_ID="${STRATEGY_ID:-TLTDurationMomentum}"
exec bash "$(dirname "$0")/ingest_platform_live.sh" "$ROOT" \
  "$ROOT/qc_research/platform_artifacts/tlt_duration_momentum.json"
