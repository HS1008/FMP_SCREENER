#!/usr/bin/env bash
# Host-side Market Intelligence activation. Called over authorized SSH after
# the application commit is deployed. Never prints secret values.
#
#   scripts/activate_market_intelligence_host.sh --phase probe
#   scripts/activate_market_intelligence_host.sh --phase provision
#   scripts/activate_market_intelligence_host.sh --phase ingest
#   scripts/activate_market_intelligence_host.sh --phase ingest-fred|ingest-finra|ingest-legacy|ingest-analytics|ingest-morning
#   scripts/activate_market_intelligence_host.sh --phase verify
#   scripts/activate_market_intelligence_host.sh --phase schedule
#
# Required files (0600) when provisioning:
#   /root/FMP_SCREENER/.secrets/fred_api_key
#   /root/FMP_SCREENER/.secrets/finra_client_id
#   /root/FMP_SCREENER/.secrets/finra_client_secret
#   /root/FMP_SCREENER/.secrets/mi_readonly.pw
#   /root/FMP_SCREENER/.secrets/dashboard_readonly.pw   # Strategy Monitor; provision_dashboard_readonly.sh
#   /root/FMP_SCREENER/.secrets/ai_context_api_token
#
# CREATE ROLE uses an admin identity, never the dashboard writer:
#   1. MI_ADMIN_DATABASE_URL or ADMIN_DATABASE_URL, if set
#   2. local postgres peer (sudo/runuser) only when the writer host is loopback
set -euo pipefail

ROOT="/root/FMP_SCREENER"
ENV_FILE="/etc/fmp/market_intelligence.env"
DASHBOARD_ENV="/root/FMP_SCREENER/.env"
PHASE=""
FRED_KEY_FILE="/root/FMP_SCREENER/.secrets/fred_api_key"
FINRA_ID_FILE="/root/FMP_SCREENER/.secrets/finra_client_id"
FINRA_SECRET_FILE="/root/FMP_SCREENER/.secrets/finra_client_secret"
RO_PW_FILE="/root/FMP_SCREENER/.secrets/mi_readonly.pw"
DASH_RO_PW_FILE="/root/FMP_SCREENER/.secrets/dashboard_readonly.pw"
AI_TOKEN_FILE="/root/FMP_SCREENER/.secrets/ai_context_api_token"
API_ENV_FILE="/etc/fmp/ai_context_api.env"

while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --dashboard-env) DASHBOARD_ENV="$2"; shift 2 ;;
    --phase) PHASE="$2"; shift 2 ;;
    --fred-key-file) FRED_KEY_FILE="$2"; shift 2 ;;
    --finra-id-file) FINRA_ID_FILE="$2"; shift 2 ;;
    --finra-secret-file) FINRA_SECRET_FILE="$2"; shift 2 ;;
    --readonly-pw-file) RO_PW_FILE="$2"; shift 2 ;;
    --dashboard-readonly-pw-file) DASH_RO_PW_FILE="$2"; shift 2 ;;
    --ai-token-file) AI_TOKEN_FILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
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

writer_db_meta() {
  python3 - <<'PY'
import os
import urllib.parse

raw = (os.environ.get("DATABASE_URL") or "").strip()
if raw:
    parts = urllib.parse.urlsplit(raw)
    db = (parts.path or "/fmp").lstrip("/") or "fmp"
    host = (parts.hostname or "").lower()
else:
    db = os.environ.get("DB_NAME") or "fmp"
    host = (os.environ.get("DB_HOST") or "127.0.0.1").lower()
if host in {"127.0.0.1", "localhost", "::1"}:
    kind = "loopback"
elif not host:
    kind = "socket"
else:
    kind = "tcp"
print("{0}\t{1}".format(db, kind))
PY
}

postgres_peer_works() {
  if ! getent passwd postgres >/dev/null 2>&1; then
    return 1
  fi
  if command -v sudo >/dev/null 2>&1; then
    if sudo -n -u postgres psql -d postgres -v ON_ERROR_STOP=1 -tAc "SELECT 1" >/dev/null 2>&1; then
      return 0
    fi
  fi
  if command -v runuser >/dev/null 2>&1; then
    if runuser -u postgres -- psql -d postgres -v ON_ERROR_STOP=1 -tAc "SELECT 1" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

apply_readonly_sql_as_postgres() {
  local db_name="$1"
  local sql_file="$ROOT/db/roles/market_intelligence_readonly.sql"
  # The postgres OS user cannot read /root. Feed SQL on stdin; the current
  # shell expands the password file and opens the SQL file.
  if command -v sudo >/dev/null 2>&1 && sudo -n -u postgres psql -d postgres -v ON_ERROR_STOP=1 -tAc "SELECT 1" >/dev/null 2>&1; then
    sudo -n -u postgres psql -d "$db_name" -v ON_ERROR_STOP=1 \
      -v ro_password="$(cat "$RO_PW_FILE")" \
      -f - < "$sql_file"
    return $?
  fi
  runuser -u postgres -- psql -d "$db_name" -v ON_ERROR_STOP=1 \
    -v ro_password="$(cat "$RO_PW_FILE")" \
    -f - < "$sql_file"
}

apply_readonly_role_sql() {
  local meta db_name host_kind admin_url
  meta="$(writer_db_meta)"
  db_name="${meta%%	*}"
  host_kind="${meta#*	}"
  echo "readonly_sql_db=${db_name}"
  echo "writer_host_kind=${host_kind}"

  admin_url="${MI_ADMIN_DATABASE_URL:-${ADMIN_DATABASE_URL:-}}"
  if [ -n "$admin_url" ]; then
    echo "readonly_sql_via=admin_url"
    unset PGPASSWORD
    psql "$admin_url" -v ON_ERROR_STOP=1 \
      -v ro_password="$(cat "$RO_PW_FILE")" \
      -f "$ROOT/db/roles/market_intelligence_readonly.sql"
    return
  fi

  if [ "$host_kind" = "loopback" ] || [ "$host_kind" = "socket" ]; then
    if postgres_peer_works; then
      echo "readonly_sql_via=postgres_peer"
      unset PGPASSWORD
      apply_readonly_sql_as_postgres "$db_name"
      return
    fi
    echo "FAIL: writer database is local but postgres peer/admin auth is unavailable"
    echo "Need MI_ADMIN_DATABASE_URL / ADMIN_DATABASE_URL, or sudo -n -u postgres (or runuser) peer access."
    echo "The dashboard writer cannot CREATE ROLE. FRED/env files may already be written; timers were not enabled."
    exit 3
  fi

  echo "FAIL: writer database is remote and no admin URL is configured"
  echo "Set MI_ADMIN_DATABASE_URL or ADMIN_DATABASE_URL to a CREATEROLE identity."
  echo "Do not use the dashboard writer. FRED/env files may already be written; timers were not enabled."
  exit 3
}

phase_probe() {
  echo "PHASE probe"
  load_writer_env
  echo "whoami=$(id -un)"
  echo "postgres_os_user=$(getent passwd postgres >/dev/null && echo present || echo absent)"
  echo "sudo_present=$(command -v sudo >/dev/null && echo yes || echo no)"
  echo "runuser_present=$(command -v runuser >/dev/null && echo yes || echo no)"
  echo "psql_present=$(command -v psql >/dev/null && echo yes || echo no)"
  echo "admin_url_env=$([ -n "${MI_ADMIN_DATABASE_URL:-${ADMIN_DATABASE_URL:-}}" ] && echo present || echo absent)"
  echo "dashboard_env_present=$([ -f "$DASHBOARD_ENV" ] && echo yes || echo no)"
  echo "mi_env_present=$([ -f "$ENV_FILE" ] && echo yes || echo no)"
  echo "fred_key_file=$([ -s "$FRED_KEY_FILE" ] && echo present || echo absent)"
  echo "finra_id_file=$([ -s "$FINRA_ID_FILE" ] && echo present || echo absent)"
  echo "finra_secret_file=$([ -s "$FINRA_SECRET_FILE" ] && echo present || echo absent)"
  echo "readonly_pw_file=$([ -s "$RO_PW_FILE" ] && echo present || echo absent)"
  echo "dashboard_readonly_pw_file=$([ -s "$DASH_RO_PW_FILE" ] && echo present || echo absent)"
  echo "ai_token_file=$([ -s "$AI_TOKEN_FILE" ] && echo present || echo absent)"
  if [ -z "${DATABASE_URL:-}" ] && { [ -z "${DB_HOST:-}" ] || [ -z "${DB_NAME:-}" ]; }; then
    echo "writer_db=missing"
  else
    meta="$(writer_db_meta)"
    echo "writer_db=present"
    echo "readonly_sql_db=${meta%%	*}"
    echo "writer_host_kind=${meta#*	}"
  fi
  if postgres_peer_works; then
    echo "postgres_peer=ok"
  else
    echo "postgres_peer=unavailable"
  fi
  if [ -n "${DATABASE_URL:-}" ]; then
    unset PGPASSWORD
    if role_state="$(psql "$DATABASE_URL" -tAc "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mi_readonly') THEN 'present' ELSE 'absent' END" 2>/dev/null)"; then
      echo "mi_readonly_role=$(printf '%s' "$role_state" | tr -d '[:space:]')"
    else
      echo "mi_readonly_role=unknown"
    fi
  elif [ -n "${DB_HOST:-}" ] && [ -n "${DB_NAME:-}" ] && [ -n "${DB_USER:-}" ]; then
    export PGPASSWORD="${DB_PASSWORD:-}"
    if role_state="$(psql -h "${DB_HOST}" -p "${DB_PORT:-5432}" -U "${DB_USER}" -d "${DB_NAME}" -tAc "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mi_readonly') THEN 'present' ELSE 'absent' END" 2>/dev/null)"; then
      echo "mi_readonly_role=$(printf '%s' "$role_state" | tr -d '[:space:]')"
    else
      echo "mi_readonly_role=unknown"
    fi
    unset PGPASSWORD
  else
    echo "mi_readonly_role=unknown"
  fi
  echo "probe complete"
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
  # The refresh unit loads only /etc/fmp/market_intelligence.env, not the dashboard
  # .env. Copy a writer URL from DATABASE_URL or DB_* so systemd has a writer identity.
  if [ -f "$ROOT/scripts/materialize_mi_writer_url.py" ]; then
    python3 "$ROOT/scripts/materialize_mi_writer_url.py" --output /tmp/mi_writer_url
  else
    python3 - <<'PY'
import os, pathlib, urllib.parse
def usable(value):
    text = (value or "").strip()
    return "" if (not text or "CHANGE_ME" in text) else text
url = usable(os.environ.get("MARKET_INTELLIGENCE_DATABASE_URL"))
source = "dedicated"
if not url:
    url = usable(os.environ.get("DATABASE_URL"))
    source = "database_url"
if not url:
    host = (os.environ.get("DB_HOST") or "").strip()
    name = (os.environ.get("DB_NAME") or "").strip()
    user = (os.environ.get("DB_USER") or "").strip()
    if host and name and user:
        url = "postgresql://{0}:{1}@{2}:{3}/{4}".format(
            urllib.parse.quote(user, safe=""),
            urllib.parse.quote(os.environ.get("DB_PASSWORD") or "", safe=""),
            host,
            (os.environ.get("DB_PORT") or "5432").strip() or "5432",
            name,
        )
        source = "db_star"
if not url:
    print("writer_url_source=missing")
    raise SystemExit(3)
pathlib.Path("/tmp/mi_writer_url").write_text(url + "\n", encoding="utf-8")
os.chmod("/tmp/mi_writer_url", 0o600)
print("writer_url_source={0}".format(source))
PY
  fi
  python3 "$ROOT/scripts/update_protected_env.py" \
    --env-file "$ENV_FILE" --key MARKET_INTELLIGENCE_DATABASE_URL --value-file /tmp/mi_writer_url
  rm -f /tmp/mi_writer_url
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

  if [ -s "$FINRA_ID_FILE" ] && [ -s "$FINRA_SECRET_FILE" ]; then
    python3 "$ROOT/scripts/update_protected_env.py" \
      --env-file "$ENV_FILE" --key FINRA_CLIENT_ID --value-file "$FINRA_ID_FILE"
    python3 "$ROOT/scripts/update_protected_env.py" \
      --env-file "$ENV_FILE" --key FINRA_CLIENT_SECRET --value-file "$FINRA_SECRET_FILE"
    printf '1\n' > /tmp/mi_finra_enabled
    chmod 600 /tmp/mi_finra_enabled
    python3 "$ROOT/scripts/update_protected_env.py" \
      --env-file "$ENV_FILE" --key MI_FINRA_ENABLED --value-file /tmp/mi_finra_enabled
    rm -f /tmp/mi_finra_enabled
    echo "finra_query_credentials=present"
  else
    echo "finra_query_credentials=absent"
  fi

  python3 "$ROOT/scripts/materialize_ai_context_env.py" \
    --source "$ENV_FILE" \
    --dest "$API_ENV_FILE" \
    --create-from "$ROOT/deploy/market_intelligence/ai_context_api.env.example"
  python3 "$ROOT/scripts/update_protected_env.py" \
    --env-file "$API_ENV_FILE" --key AI_CONTEXT_API_TOKEN --value-file "$AI_TOKEN_FILE" \
    --create-from "$ROOT/deploy/market_intelligence/ai_context_api.env.example"
  python3 "$ROOT/scripts/update_protected_env.py" \
    --env-file "$API_ENV_FILE" --key DATABASE_READONLY_URL --value-file /tmp/mi_readonly_url
  rm -f /tmp/mi_readonly_url
  chmod 0600 "$API_ENV_FILE"

  load_writer_env
  echo "Applying mi_readonly grants via admin/peer (password file, not printed)"
  apply_readonly_role_sql
  bash "$ROOT/scripts/provision_dashboard_readonly.sh" \
    --root "$ROOT" \
    --pw-file "$DASH_RO_PW_FILE" \
    --dashboard-env "$DASHBOARD_ENV" \
    --systemd-env /etc/fmp/fmp-dashboard.env
  unset PGPASSWORD
  echo "provision complete"
}

run_with_heartbeat() {
  local label="$1"
  shift
  local hb_pid rc
  echo "starting ${label} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  (
    while sleep 15; do
      echo "${label}_heartbeat=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    done
  ) &
  hb_pid=$!
  set +e
  "$@"
  rc=$?
  set -e
  kill "$hb_pid" 2>/dev/null || true
  wait "$hb_pid" 2>/dev/null || true
  echo "finished ${label} rc=${rc} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  return "$rc"
}

phase_ingest_fred() {
  echo "PHASE ingest-fred"
  load_writer_env
  python -m jobs.apply_migrations
  python -m jobs.market_intelligence_refresh --probe-config --json
  python -m jobs.market_intelligence_refresh --all-configured --dry-run --json
  run_with_heartbeat fred_ingest python -m jobs.market_intelligence_refresh --fred --mode full --wait-lock --json
}

phase_ingest_finra() {
  echo "PHASE ingest-finra"
  load_writer_env
  python -m jobs.apply_migrations
  if [ -z "${FINRA_CLIENT_ID:-${FINRA_API_CLIENT_ID:-}}" ] || [ -z "${FINRA_CLIENT_SECRET:-${FINRA_API_CLIENT_SECRET:-}}" ]; then
    echo "FINRA Query API credentials absent — Order Flow will show CONFIGURATION_REQUIRED/NEVER_ATTEMPTED"
    return 0
  fi
  run_with_heartbeat finra_ingest python -m jobs.market_intelligence_refresh --finra --mode full --wait-lock --json
}

phase_ingest_legacy() {
  echo "PHASE ingest-legacy"
  load_writer_env
  if [ -d "$ROOT/outputs/precomputed" ] && [ "$(find "$ROOT/outputs/precomputed" -type f | wc -l)" -gt 0 ]; then
    run_with_heartbeat legacy_sector python -m jobs.market_intelligence_refresh --legacy-sector --wait-lock --json \
      || echo "legacy sector ingest returned non-zero (recorded; continuing)"
  else
    echo "legacy precomputed bundles absent — sector pages will show honest unavailable/empty"
  fi
}

phase_ingest_analytics() {
  echo "PHASE ingest-analytics"
  load_writer_env
  run_with_heartbeat analytics_backfill python -m jobs.market_intelligence_refresh \
    --build-analytics --backfill-analytics-from 2019-01-01 --wait-lock --json
}

phase_ingest_morning() {
  echo "PHASE ingest-morning"
  load_writer_env
  run_with_heartbeat morning_context python -m jobs.build_morning_context --wait-lock --json
}

phase_ingest() {
  echo "PHASE ingest"
  phase_ingest_fred
  phase_ingest_finra
  phase_ingest_legacy
  phase_ingest_analytics
  phase_ingest_morning
  echo "ingest complete"
}

phase_verify() {
  echo "PHASE verify"
  load_writer_env
  systemctl is-active --quiet fmp-dashboard
  echo "dashboard_active=yes"
  python -m jobs.verify_mi_dashboard --json
  bash "$ROOT/scripts/verify_dashboard_identity.sh"
  echo "verify complete"
}

sanitize_unit_journal() {
  local unit="$1"
  journalctl -u "$unit" -n 40 --no-pager -o cat 2>/dev/null \
    | grep -Ei 'error|traceback|address already|permission|modulenotfound|importerror|failed|started|uvicorn|listening|application startup' \
    | grep -viE 'postgres(ql)?://|bearer |password=|api_key|token=' \
    || true
}

wait_for_local_api() {
  local i code
  for i in $(seq 1 40); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health || true)"
    echo "api_health_wait=${i} code=${code}"
    if [ "$code" = "200" ]; then
      return 0
    fi
    sleep 1
  done
  echo "api_unit=$(systemctl is-active fmp-ai-context-api.service || true)"
  echo "api_listen=$(ss -ltn 2>/dev/null | awk '$4 ~ /127.0.0.1:8765/ {print \"yes\"; found=1} END {if (!found) print \"no\"}')"
  echo "api_journal_sanitized:"
  sanitize_unit_journal fmp-ai-context-api.service
  return 1
}

phase_schedule() {
  echo "PHASE schedule"
  bash "$ROOT/scripts/install_market_intelligence_timers.sh" --apply --with-api --root "$ROOT" --env-file "$ENV_FILE" --api-env-file "$API_ENV_FILE"
  systemctl is-enabled fmp-mi-refresh.timer
  systemctl is-active fmp-ai-context-api.service
  systemctl list-timers fmp-mi-refresh.timer --no-pager
  if ! wait_for_local_api; then
    echo "FAIL: private AI API did not become ready on 127.0.0.1:8765"
    exit 4
  fi
  no_token="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/v1/ready || true)"
  echo "ai_ready_without_token=${no_token}"
  if [ "$no_token" != "401" ]; then
    echo "FAIL: private AI API did not return 401 without a token"
    sanitize_unit_journal fmp-ai-context-api.service
    exit 4
  fi
  with_token="$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $(cat "$AI_TOKEN_FILE")" \
    http://127.0.0.1:8765/v1/ready || true)"
  echo "ai_ready_with_token=${with_token}"
  if [ "$with_token" != "200" ]; then
    echo "FAIL: private AI API did not return 200 with the configured token"
    sanitize_unit_journal fmp-ai-context-api.service
    exit 4
  fi
  echo "schedule complete"
}

case "$PHASE" in
  probe) phase_probe ;;
  provision) phase_provision ;;
  ingest) phase_ingest ;;
  ingest-fred) phase_ingest_fred ;;
  ingest-finra) phase_ingest_finra ;;
  ingest-legacy) phase_ingest_legacy ;;
  ingest-analytics) phase_ingest_analytics ;;
  ingest-morning) phase_ingest_morning ;;
  verify)
    systemctl restart fmp-dashboard
    systemctl is-active --quiet fmp-dashboard
    phase_verify
    ;;
  schedule) phase_schedule ;;
  *) echo "usage: --phase probe|provision|ingest|ingest-fred|ingest-finra|ingest-legacy|ingest-analytics|ingest-morning|verify|schedule" >&2; exit 64 ;;
esac
