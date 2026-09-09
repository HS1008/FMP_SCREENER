#!/usr/bin/env bash
# Render and optionally install the private IBKR ingest API systemd unit.
#
#   scripts/install_ibkr_ingest.sh                 # dry run
#   scripts/install_ibkr_ingest.sh --apply
#
# Does not modify fmp-dashboard, FRED timers, or the read-only AI context API.
set -euo pipefail

ROOT="/root/FMP_SCREENER"
SERVICE_USER="root"
ENV_FILE="/etc/fmp/ibkr_ingest.env"
SYSTEMD_DIR="/etc/systemd/system"
APPLY=0
USE_SYSTEMCTL=1
UNIT="fmp-ibkr-ingest.service"

while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --systemd-dir) SYSTEMD_DIR="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    --no-systemctl) USE_SYSTEMCTL=0; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$(cd "$HERE/.." && pwd)/deploy/market_intelligence/${UNIT}"

render() {
  sed -e "s#__ROOT__#${ROOT}#g" -e "s#__USER__#${SERVICE_USER}#g" -e "s#__ENV_FILE__#${ENV_FILE}#g" "$TEMPLATE"
}

mode="DRY RUN (no changes)"
[ "$APPLY" = 1 ] && mode="APPLY"
echo "IBKR ingest installer: $mode"
echo "  root=$ROOT user=$SERVICE_USER env_file=$ENV_FILE systemd_dir=$SYSTEMD_DIR"
echo
[ -f "$ENV_FILE" ] || echo "  WARN: $ENV_FILE missing; copy deploy/market_intelligence/ibkr_ingest.env.example (chmod 0600)"

target="$SYSTEMD_DIR/$UNIT"
rendered="$(render)"
echo "=== $target ==="
if [ -f "$target" ]; then
  if [ "$(cat "$target")" = "$rendered" ]; then
    echo "(unchanged)"
  else
    echo "(would replace)"
    diff -u "$target" <(printf '%s\n' "$rendered") || true
  fi
else
  echo "(would create)"
  printf '%s\n' "$rendered"
fi

if [ "$APPLY" = 1 ]; then
  mkdir -p "$SYSTEMD_DIR"
  printf '%s\n' "$rendered" > "$target"
  if [ "$USE_SYSTEMCTL" = 1 ] && command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl enable --now "$UNIT"
  fi
fi
