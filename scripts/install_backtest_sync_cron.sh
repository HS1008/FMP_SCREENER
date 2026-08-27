#!/usr/bin/env bash
# Idempotently install a one-minute QuantConnect *backtest* sync cron.
# Does NOT modify the existing ~10-minute live QuantConnect sync.
# Does NOT run unless you execute this script yourself.
set -euo pipefail

ROOT="${1:-/root/FMP_SCREENER}"
PYTHON="${ROOT}/venv/bin/python"
LOG="${ROOT}/outputs/backtest_sync.log"
LINE="* * * * * cd ${ROOT} && ${PYTHON} -m jobs.sync_quantconnect --backtests-only >> ${LOG} 2>&1"

mkdir -p "$(dirname "$LOG")"

existing="$(crontab -l 2>/dev/null || true)"

if printf '%s\n' "$existing" | grep -F "jobs.sync_quantconnect --backtests-only" >/dev/null; then
  echo "Backtest sync cron already installed. No change."
  printf '%s\n' "$existing" | grep -F "jobs.sync_quantconnect --backtests-only" || true
  exit 0
fi

{
  printf '%s\n' "$existing"
  echo "$LINE"
} | crontab -

echo "Installed backtest sync cron:"
echo "  $LINE"
echo
echo "Manual equivalent:"
echo "  cd ${ROOT} && ${PYTHON} -m jobs.sync_quantconnect --backtests-only"
echo
echo "The existing live QuantConnect sync cadence was not modified."
