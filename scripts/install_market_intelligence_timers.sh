#!/usr/bin/env bash
# Render and (optionally) install the Market Intelligence systemd units.
#
#   scripts/install_market_intelligence_timers.sh                 # DRY RUN: render units, list actions, change nothing
#   scripts/install_market_intelligence_timers.sh --apply         # write units, daemon-reload, enable + start the refresh timer
#   scripts/install_market_intelligence_timers.sh --apply --with-api   # also enable + start the read-only AI context API
#
# Options:
#   --root DIR          checkout root (default /root/FMP_SCREENER)
#   --user NAME         service user (default root)
#   --env-file PATH     EnvironmentFile for the refresh job (default /etc/fmp/market_intelligence.env)
#   --api-env-file PATH EnvironmentFile for the AI context API (default /etc/fmp/ai_context_api.env)
#   --systemd-dir DIR   where units are written (default /etc/systemd/system)
#   --no-systemctl      write files but skip daemon-reload/enable/start (for tests and staging)
#
# This script is NOT run by deploy.yml. Installing timers on the production host is an explicit
# operator step. It never edits existing units (fmp-dashboard, cron lines) and refuses to overwrite a
# unit whose current content differs unless --apply is given (then the diff is printed first).
set -euo pipefail

ROOT="/root/FMP_SCREENER"
SERVICE_USER="root"
ENV_FILE="/etc/fmp/market_intelligence.env"
API_ENV_FILE="/etc/fmp/ai_context_api.env"
SYSTEMD_DIR="/etc/systemd/system"
APPLY=0
WITH_API=0
USE_SYSTEMCTL=1

while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --api-env-file) API_ENV_FILE="$2"; shift 2 ;;
    --systemd-dir) SYSTEMD_DIR="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    --with-api) WITH_API=1; shift ;;
    --no-systemctl) USE_SYSTEMCTL=0; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES="$(cd "$HERE/.." && pwd)/deploy/market_intelligence"
UNITS=(fmp-mi-refresh.service fmp-mi-refresh.timer)
if [ "$WITH_API" = 1 ]; then
  UNITS+=(fmp-ai-context-api.service)
fi

render() {
  sed -e "s#__ROOT__#${ROOT}#g" -e "s#__USER__#${SERVICE_USER}#g" -e "s#__ENV_FILE__#${ENV_FILE}#g" -e "s#__API_ENV_FILE__#${API_ENV_FILE}#g" "$TEMPLATES/$1"
}

mode="DRY RUN (no changes)"
[ "$APPLY" = 1 ] && mode="APPLY"
echo "Market Intelligence timer installer: $mode"
echo "  root=$ROOT user=$SERVICE_USER env_file=$ENV_FILE api_env_file=$API_ENV_FILE systemd_dir=$SYSTEMD_DIR with_api=$WITH_API"
echo

# Preflight: report, do not fix.
status=0
[ -x "$ROOT/venv/bin/python" ] || { echo "  WARN: $ROOT/venv/bin/python not found (venv missing?)"; status=1; }
[ -f "$ENV_FILE" ] || echo "  WARN: $ENV_FILE missing; copy deploy/market_intelligence/market_intelligence.env.example and fill it (chmod 0600)"
if [ "$WITH_API" = 1 ]; then
  [ -f "$API_ENV_FILE" ] || echo "  WARN: $API_ENV_FILE missing; copy deploy/market_intelligence/ai_context_api.env.example (read-only DB URL + API token only)"
fi
if [ -f "$ENV_FILE" ]; then
  perms="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo '?')"
  case "$perms" in 600|400) ;; *) echo "  WARN: $ENV_FILE has mode $perms; expected 0600" ;; esac
fi
command -v systemctl >/dev/null 2>&1 || { echo "  WARN: systemctl not available on this host"; USE_SYSTEMCTL=0; }
echo

for unit in "${UNITS[@]}"; do
  target="$SYSTEMD_DIR/$unit"
  rendered="$(render "$unit")"
  echo "=== $target ==="
  if [ -f "$target" ]; then
    if [ "$(cat "$target")" = "$rendered" ]; then
      echo "(unchanged)"
    else
      echo "(would replace; diff current -> rendered)"
      diff -u "$target" <(printf '%s\n' "$rendered") || true
    fi
  else
    echo "(new)"
    printf '%s\n' "$rendered"
  fi
  echo
  if [ "$APPLY" = 1 ]; then
    mkdir -p "$SYSTEMD_DIR"
    printf '%s\n' "$rendered" > "$target"
    chmod 0644 "$target"
  fi
done

actions=(
  "systemctl daemon-reload"
  "systemctl enable --now fmp-mi-refresh.timer"
  "systemctl list-timers fmp-mi-refresh.timer"
)
if [ "$WITH_API" = 1 ]; then
  actions+=("systemctl enable --now fmp-ai-context-api.service" "systemctl is-active fmp-ai-context-api.service")
fi

echo "Actions:"
for a in "${actions[@]}"; do echo "  $a"; done
echo

if [ "$APPLY" = 1 ] && [ "$USE_SYSTEMCTL" = 1 ]; then
  systemctl daemon-reload
  systemctl enable --now fmp-mi-refresh.timer
  systemctl list-timers fmp-mi-refresh.timer --no-pager || true
  if [ "$WITH_API" = 1 ]; then
    systemctl enable --now fmp-ai-context-api.service
    systemctl is-active fmp-ai-context-api.service
  fi
  echo "Installed. Existing fmp-dashboard service and cron lines were not modified."
elif [ "$APPLY" = 1 ]; then
  echo "Units written to $SYSTEMD_DIR; systemctl steps skipped (--no-systemctl or unavailable)."
else
  echo "Dry run complete. Re-run with --apply to write units and enable the timer."
fi

echo
echo "Manual first run (recommended before enabling the timer):"
echo "  cd $ROOT && set -a && . $ENV_FILE && set +a && venv/bin/python -m jobs.market_intelligence_refresh --all-configured --dry-run --json"
exit $status
