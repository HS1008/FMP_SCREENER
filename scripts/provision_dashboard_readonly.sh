#!/usr/bin/env bash
# Provision dashboard_readonly and materialize DASHBOARD_READONLY_URL.
# Everyday deploy and MI activate both call this. Never prints passwords or URLs.
#
#   scripts/provision_dashboard_readonly.sh
#   scripts/provision_dashboard_readonly.sh --require
#
# Exit 0: provisioned, or skipped because the password file is absent.
# Exit 3: password present but admin/peer apply or env materialize failed.
# Exit 64: usage.
#
# CREATE ROLE uses an admin identity, never the dashboard writer:
#   1. MI_ADMIN_DATABASE_URL or ADMIN_DATABASE_URL, if set
#   2. local postgres peer (sudo/runuser) only when the writer host is loopback
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PW_FILE="$ROOT/.secrets/dashboard_readonly.pw"
DASHBOARD_ENV="$ROOT/.env"
SYSTEMD_ENV="/etc/fmp/fmp-dashboard.env"
SQL_FILE="$ROOT/db/roles/dashboard_readonly.sql"
REQUIRE=0
URL_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --pw-file) PW_FILE="$2"; shift 2 ;;
    --dashboard-env) DASHBOARD_ENV="$2"; shift 2 ;;
    --systemd-env) SYSTEMD_ENV="$2"; shift 2 ;;
    --sql-file) SQL_FILE="$2"; shift 2 ;;
    --require) REQUIRE=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

cd "$ROOT"

load_host_env() {
  local f
  for f in \
    "$DASHBOARD_ENV" \
    /etc/fmp/market_intelligence.env \
    /etc/fmp/fmp-dashboard.env \
    "${FMP_DASHBOARD_ENV:-}"
  do
    if [ -n "${f:-}" ] && [ -f "$f" ]; then
      set -a
      # shellcheck disable=SC1090
      . "$f"
      set +a
    fi
  done
}

writer_db_meta() {
  python3 - <<'PY'
import os
import urllib.parse

raw = (os.environ.get("DATABASE_URL") or os.environ.get("ADMIN_DATABASE_URL") or os.environ.get("MI_ADMIN_DATABASE_URL") or "").strip()
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

apply_sql_as_postgres() {
  local db_name="$1"
  # The postgres OS user cannot read /root. Feed SQL on stdin.
  if command -v sudo >/dev/null 2>&1 && sudo -n -u postgres psql -d postgres -v ON_ERROR_STOP=1 -tAc "SELECT 1" >/dev/null 2>&1; then
    sudo -n -u postgres psql -d "$db_name" -v ON_ERROR_STOP=1 \
      -v ro_password="$(cat "$PW_FILE")" \
      -f - < "$SQL_FILE"
    return $?
  fi
  runuser -u postgres -- psql -d "$db_name" -v ON_ERROR_STOP=1 \
    -v ro_password="$(cat "$PW_FILE")" \
    -f - < "$SQL_FILE"
}

apply_dashboard_role_sql() {
  local meta db_name host_kind admin_url
  meta="$(writer_db_meta)"
  db_name="${meta%%	*}"
  host_kind="${meta#*	}"
  echo "dashboard_readonly_sql_db=${db_name}"
  echo "writer_host_kind=${host_kind}"

  admin_url="${MI_ADMIN_DATABASE_URL:-${ADMIN_DATABASE_URL:-}}"
  if [ -n "$admin_url" ]; then
    echo "dashboard_readonly_sql_via=admin_url"
    unset PGPASSWORD
    psql "$admin_url" -v ON_ERROR_STOP=1 \
      -v ro_password="$(cat "$PW_FILE")" \
      -f "$SQL_FILE"
    return
  fi

  if [ "$host_kind" = "loopback" ] || [ "$host_kind" = "socket" ]; then
    if postgres_peer_works; then
      echo "dashboard_readonly_sql_via=postgres_peer"
      unset PGPASSWORD
      apply_sql_as_postgres "$db_name"
      return
    fi
    echo "FAIL: writer database is local but postgres peer/admin auth is unavailable"
    echo "Need MI_ADMIN_DATABASE_URL / ADMIN_DATABASE_URL, or sudo -n -u postgres (or runuser) peer access."
    echo "The dashboard writer cannot CREATE ROLE."
    exit 3
  fi

  echo "FAIL: writer database is remote and no admin URL is configured"
  echo "Set MI_ADMIN_DATABASE_URL or ADMIN_DATABASE_URL to a CREATEROLE identity."
  echo "Do not use the dashboard writer."
  exit 3
}

cleanup_url_file() {
  if [ -n "${URL_FILE:-}" ]; then
    rm -f "$URL_FILE"
    URL_FILE=""
  fi
}

write_url_file() {
  URL_FILE="$(mktemp "${TMPDIR:-/tmp}/dashboard_readonly_url.XXXXXX")"
  chmod 0600 "$URL_FILE"
  trap cleanup_url_file EXIT
  export URL_FILE
  PW_FILE="$PW_FILE" URL_FILE="$URL_FILE" python3 - <<'PY'
import os
import pathlib
import urllib.parse

pw = pathlib.Path(os.environ["PW_FILE"]).read_text(encoding="utf-8").strip()
if not pw or any(ch.isspace() for ch in pw):
    raise SystemExit("password file is empty or contains whitespace")
raw = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("ADMIN_DATABASE_URL")
    or os.environ.get("MI_ADMIN_DATABASE_URL")
    or ""
).strip()
if raw:
    parts = urllib.parse.urlsplit(raw)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 5432
    db = (parts.path or "/fmp").lstrip("/") or "fmp"
else:
    host = os.environ.get("DB_HOST") or "127.0.0.1"
    port = int(os.environ.get("DB_PORT") or "5432")
    db = os.environ.get("DB_NAME") or "fmp"
url = "postgresql://dashboard_readonly:{0}@{1}:{2}/{3}".format(
    urllib.parse.quote(pw, safe=""), host, port, db
)
path = pathlib.Path(os.environ["URL_FILE"])
path.write_text(url + "\n", encoding="utf-8")
os.chmod(path, 0o600)
print("dashboard readonly url constructed (not printed)")
PY
}

materialize_env() {
  local wrote=0
  if [ -n "${SYSTEMD_ENV:-}" ]; then
    local parent
    parent="$(dirname "$SYSTEMD_ENV")"
    if [ ! -d "$parent" ]; then
      if mkdir -p "$parent" 2>/dev/null; then
        chmod 0755 "$parent" || true
      else
        echo "systemd_env=skipped_parent_absent"
        SYSTEMD_ENV=""
      fi
    fi
  fi
  if [ -n "${SYSTEMD_ENV:-}" ]; then
    python3 "$ROOT/scripts/update_protected_env.py" \
      --env-file "$SYSTEMD_ENV" \
      --key DASHBOARD_READONLY_URL \
      --value-file "$URL_FILE" \
      --create-from "$ROOT/deploy/fmp-dashboard.env.example"
    echo "systemd_env=written"
    wrote=1
  fi
  if [ -n "${DASHBOARD_ENV:-}" ]; then
    if [ ! -f "$DASHBOARD_ENV" ]; then
      mkdir -p "$(dirname "$DASHBOARD_ENV")"
      printf '# Streamlit / deploy identity (writer URL must not be added here by this script)\n' > "$DASHBOARD_ENV"
      chmod 0600 "$DASHBOARD_ENV"
    fi
    python3 "$ROOT/scripts/update_protected_env.py" \
      --env-file "$DASHBOARD_ENV" \
      --key DASHBOARD_READONLY_URL \
      --value-file "$URL_FILE"
    echo "dashboard_env=written"
    wrote=1
  fi
  cleanup_url_file
  if [ "$wrote" != 1 ]; then
    echo "FAIL: no env file could be written for DASHBOARD_READONLY_URL"
    exit 3
  fi
}

if [ ! -s "$PW_FILE" ]; then
  if [ "$REQUIRE" = 1 ]; then
    echo "FAIL: dashboard_readonly password file absent and --require is set"
    exit 3
  fi
  echo "dashboard_readonly=skipped (password file absent)"
  exit 0
fi

if [ ! -f "$SQL_FILE" ]; then
  echo "FAIL: dashboard_readonly SQL is missing"
  exit 3
fi

load_host_env
apply_dashboard_role_sql
write_url_file
materialize_env
unset PGPASSWORD
echo "dashboard_readonly=provisioned"
