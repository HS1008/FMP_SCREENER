#!/usr/bin/env bash
# Generic live DigitalOcean ingest for canonical platform research artifacts.
# Uses the droplet's existing authorized DB env. Never prints secrets.
# Does not launch QuantConnect. Does not restart production Streamlit.
set -euo pipefail

ROOT="${1:-/tmp/fmp-platform-ingest}"
DROPLET_ENV="${FMP_LIVE_ENV:-/root/FMP_SCREENER/.env}"
TARGET="${PLATFORM_INGEST_TARGET:-${2:-}}"
STRATEGY_ID="${STRATEGY_ID:-}"
CANONICAL_ONLY="${CANONICAL_ONLY:-1}"

if [ ! -d "$ROOT" ]; then
  echo "FAIL: ingest tree missing at $ROOT"
  exit 1
fi
if [ -z "$TARGET" ]; then
  TARGET="$ROOT/qc_research/platform_artifacts"
fi
if [ ! -e "$TARGET" ]; then
  echo "FAIL: ingest target missing at $TARGET"
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

echo "Verifying contract digests..."
python -m qc_research.contracts.digests

echo "Applying additive migrations..."
python -m jobs.apply_migrations

INGEST_ARGS=(--root "$TARGET" --verify-monitor)
if [ -d "$TARGET" ] && [ "$CANONICAL_ONLY" = "1" ]; then
  INGEST_ARGS+=(--canonical-only)
fi

echo "Ingesting platform artifact (first pass)..."
python -m qc_research.ingest_platform_artifacts "${INGEST_ARGS[@]}"

echo "Ingesting platform artifact (idempotent second pass)..."
python -m qc_research.ingest_platform_artifacts "${INGEST_ARGS[@]}"

if echo "${TARGET}${STRATEGY_ID}" | grep -Eq 'TLTDurationMomentum|tlt_duration_momentum'; then
  if [ ! -f /etc/fmp/fmp-dashboard.env ]; then
    echo "FAIL: /etc/fmp/fmp-dashboard.env is required for platform ingest query-back"
    exit 1
  fi
  (
    set -a
    # shellcheck disable=SC1091
    source /etc/fmp/fmp-dashboard.env
    set +a
    unset DATABASE_URL DB_PASSWORD DB_USER DB_HOST DB_NAME DB_PORT MARKET_INTELLIGENCE_DATABASE_URL DASHBOARD_ALLOW_WRITER_FALLBACK
    export FMP_IDENTITY_ENV_ONLY=1
    export FMP_DASHBOARD_ENV=/etc/fmp/fmp-dashboard.env
    echo "Query-back TLTDurationMomentum identity..."
    python -m qc_research.verify_tlt_monitor --live --root "$TARGET"
    echo "Strategy Monitor AppTest against live PostgreSQL..."
    python -m qc_research.verify_tlt_monitor --live --apptest --root "$TARGET"
  )
fi

DELIVERY_REPORT="$ROOT/delivery/report.json"
if [ -f "$DELIVERY_REPORT" ]; then
  echo "Recording research-delivery facts (remote status / fallback / artifact hashes) in PostgreSQL..."
  python -m qc_research.delivery_visibility record --report "$DELIVERY_REPORT" --require-postgres
else
  echo "No delivery report present (direct host invocation); delivery facts not recorded."
fi

echo "Platform research live Postgres ingest passed."
