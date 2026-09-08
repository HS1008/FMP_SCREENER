#!/usr/bin/env bash
# Host-side Market Intelligence activation. Called over authorized SSH after
# the application commit is deployed. Never prints secret values.
#
#   scripts/activate_market_intelligence_host.sh --phase provision
#   scripts/activate_market_intelligence_host.sh --phase ingest
#   scripts/activate_market_intelligence_host.sh --phase verify
#   scripts/activate_market_intelligence_host.sh --phase schedule
#
# Required files (0600) when provisioning:
#   /root/FMP_SCREENER/.secrets/fred_api_key
#   /root/FMP_SCREENER/.secrets/mi_readonly.pw
#   /root/FMP_SCREENER/.secrets/ai_context_api_token
set -euo pipefail

ROOT="/root/FMP_SCREENER"
ENV_FILE="/etc/fmp/market_intelligence.env"
DASHBOARD_ENV="/root/FMP_SCREENER/.env"
PHASE=""
FRED_KEY_FILE="/root/FMP_SCREENER/.secrets/fred_api_key"
RO_PW_FILE="/root/FMP_SCREENER/.secrets/mi_readonly.pw"
AI_TOKEN_FILE="/root/FMP_SCREENER/.secrets/ai_context_api_token"

while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --dashboard-env) DASHBOARD_ENV="$2"; shift 2 ;;
    --phase) PHASE="$2"; shift 2 ;;
    --fred-key-file) FRED_KEY_FILE="$2"; shift 2 ;;
    --readonly-pw-file) RO_PW_FILE="$2"; shift 2 ;;
    --ai-token-file) AI_TOKEN_FILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/venv/bin/activate"
export PYTHONUNBUFFERED=1

load_writer_env() {
  if [ -f "$DASHBOARD_ENV" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$DASHBOARD_ENV"
    set +a
  fi
  if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
}

phase_provision() {
  echo "PHASE provision"
  mkdir -p /etc/fmp
  "$ROOT/scripts/provision_digitalocean_mi_secrets.sh" --apply \
    --env-file "$ENV_FILE" --fred-key-file "$FRED_KEY_FILE" --root "$ROOT"
  python3 "$ROOT/scripts/update_protected_env.py" \
    --env-file "$ENV_FILE" --key AI_CONTEXT_API_TOKEN --value-file "$AI_TOKEN_FILE" \
    --create-from "$ROOT/deploy/market_intelligence/market_intelligence.env.example"
  load_writer_env
  if [ -z "${DATABASE_URL:-}" ] && { [ -z "${DB_HOST:-}" ] || [ -z "${DB_NAME:-}" ]; }; then
    echo "FAIL: writer database settings missing from dashboard env"
    exit 3
  fi
  # Copy writer URL into the MI env file so the refresh unit has it, without printing.
  if [ -n "${DATABASE_URL:-}" ]; then
    printf '%s\n' "$DATABASE_URL" > /tmp/mi_writer_url
    chmod 600 /tmp/mi_writer_url
    python3 "$ROOT/scripts/update_protected_env.py" \
      --env-file "$ENV_FILE" --key MARKET_INTELLIGENCE_DATABASE_URL --value-file /tmp/mi_writer_url
    rm -f /tmp/mi_writer_url
  fi
  RO_PW_FILE="$RO_PW_FILE" python3 - <<'PY'
import os, pathlib, urllib.parse
pw = pathlib.Path(os.environ["RO_PW_FILE"]).read_text(encoding="utf-8").strip()
raw = (os.environ.get("DATABASE_URL") or "").strip()
if raw:
    parts = urllib.parse.urlsplit(raw)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 5432
    db = (parts.path or "/fmp").lstrip("/") or "fmp"
else:
    host = os.environ.get("DB_HOST") or "127.0.0.1"
    port = int(os.environ.get("DB_PORT") or "5432")
    db = os.environ.get("DB_NAME") or "fmp"
url = "postgresql://mi_readonly:{0}@{1}:{2}/{3}".format(urllib.parse.quote(pw, safe=""), host, port, db)
pathlib.Path("/tmp/mi_readonly_url").write_text(url + "\n", encoding="utf-8")
os.chmod("/tmp/mi_readonly_url", 0o600)
print("readonly url constructed (not printed)")
PY
  python3 "$ROOT/scripts/update_protected_env.py" \
    --env-file "$ENV_FILE" --key DATABASE_READONLY_URL --value-file /tmp/mi_readonly_url
  python3 "$ROOT/scripts/update_protected_env.py" \
    --env-file "$DASHBOARD_ENV" --key DATABASE_READONLY_URL --value-file /tmp/mi_readonly_url
  rm -f /tmp/mi_readonly_url

  load_writer_env
  echo "Applying mi_readonly grants (password file, not printed)"
  export PGPASSWORD="${DB_PASSWORD:-}"
  if [ -n "${DATABASE_URL:-}" ]; then
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
      -v ro_password="$(cat "$RO_PW_FILE")" \
      -f "$ROOT/db/roles/market_intelligence_readonly.sql"
  else
    psql -h "${DB_HOST}" -p "${DB_PORT:-5432}" -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 \
      -v ro_password="$(cat "$RO_PW_FILE")" \
      -f "$ROOT/db/roles/market_intelligence_readonly.sql"
  fi
  unset PGPASSWORD
  echo "provision complete"
}

phase_ingest() {
  echo "PHASE ingest"
  load_writer_env
  python -m jobs.apply_migrations
  python -m jobs.market_intelligence_refresh --probe-config --json
  python -m jobs.market_intelligence_refresh --all-configured --dry-run --json
  python -m jobs.market_intelligence_refresh --fred --mode full --json
  if [ -d "$ROOT/outputs/precomputed" ] && [ "$(find "$ROOT/outputs/precomputed" -type f | wc -l)" -gt 0 ]; then
    python -m jobs.market_intelligence_refresh --legacy-sector --json || echo "legacy sector ingest returned non-zero (recorded; continuing)"
  else
    echo "legacy precomputed bundles absent — sector pages will show honest unavailable/empty"
  fi
  python -m jobs.market_intelligence_refresh --build-analytics --backfill-analytics-from 2019-01-01 --json
  python -m jobs.build_morning_context --json
  echo "ingest complete"
}

phase_verify() {
  echo "PHASE verify"
  load_writer_env
  systemctl is-active --quiet fmp-dashboard
  echo "dashboard_active=yes"
  python -m jobs.verify_mi_dashboard --json
  echo "verify complete"
}

phase_schedule() {
  echo "PHASE schedule"
  bash "$ROOT/scripts/install_market_intelligence_timers.sh" --apply --with-api --root "$ROOT" --env-file "$ENV_FILE"
  systemctl is-enabled fmp-mi-refresh.timer
  systemctl is-active fmp-ai-context-api.service
  systemctl list-timers fmp-mi-refresh.timer --no-pager
  echo "schedule complete"
}

case "$PHASE" in
  provision) phase_provision ;;
  ingest) phase_ingest ;;
  verify)
    systemctl restart fmp-dashboard
    systemctl is-active --quiet fmp-dashboard
    phase_verify
    ;;
  schedule) phase_schedule ;;
  *) echo "usage: --phase provision|ingest|verify|schedule" >&2; exit 64 ;;
esac
