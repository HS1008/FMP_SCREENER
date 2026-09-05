#!/usr/bin/env bash
# Live DigitalOcean ingest for the official TLTDurationMomentum V0 artifact.
# Uses the droplet's existing authorized DB env. Never prints secrets.
# Does not restart production Streamlit. Does not launch QuantConnect.
set -euo pipefail

ROOT="${1:-/tmp/fmp-tlt-ingest}"
DROPLET_ENV="${FMP_LIVE_ENV:-/root/FMP_SCREENER/.env}"
ARTIFACT="${ROOT}/qc_research/platform_artifacts/tlt_duration_momentum.json"

if [ ! -d "$ROOT" ]; then
  echo "FAIL: ingest tree missing at $ROOT"
  exit 1
fi
if [ ! -f "$ARTIFACT" ]; then
  echo "FAIL: official TLT artifact missing"
  exit 1
fi
if [ ! -f "$DROPLET_ENV" ]; then
  echo "FAIL: authorized DB env file is not present on this host"
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$DROPLET_ENV"
set +a

if [ -z "${DATABASE_URL:-}" ] && { [ -z "${DB_HOST:-}" ] || [ -z "${DB_NAME:-}" ] || [ -z "${DB_USER:-}" ]; }; then
  echo "FAIL: sourced env has neither DATABASE_URL nor DB_HOST/DB_NAME/DB_USER"
  exit 1
fi

if [ -f /root/FMP_SCREENER/venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source /root/FMP_SCREENER/venv/bin/activate
fi

cd "$ROOT"
export PYTHONPATH="$ROOT"
export PYTHONUNBUFFERED=1

echo "Applying additive migrations..."
python -m jobs.apply_migrations

echo "Ingesting official TLT V0 artifact (first pass)..."
python -m qc_research.ingest_platform_artifacts --root "$ARTIFACT" --verify-monitor

echo "Ingesting official TLT V0 artifact (idempotent second pass)..."
python -m qc_research.ingest_platform_artifacts --root "$ARTIFACT" --verify-monitor

echo "Query-back TLTDurationMomentum identity..."
python -m qc_research.verify_tlt_monitor --live --root "$ARTIFACT"

echo "Strategy Monitor AppTest against live PostgreSQL..."
python -m qc_research.verify_tlt_monitor --live --apptest --root "$ARTIFACT"

echo "TLT V0 live Postgres → Strategy Monitor round-trip passed."
