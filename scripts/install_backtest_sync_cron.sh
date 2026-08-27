#!/usr/bin/env bash
# Idempotently install a one-minute QuantConnect *backtest* sync cron.
# Does NOT modify the existing ~10-minute live QuantConnect sync.
# Does NOT run unless you execute this script yourself.
set -euo pipefail

ROOT="${1:-/root/FMP_SCREENER}"
PYTHON="${ROOT}/venv/bin/python"
LOG="${ROOT}/outputs/backtest_sync.log"
LOCK="${ROOT}/outputs/backtest_sync.flock"
MARKER="jobs.sync_quantconnect --backtests-only"
LINE="* * * * * flock -n ${LOCK} -c 'cd ${ROOT} && ${PYTHON} -m jobs.sync_quantconnect --backtests-only >> ${LOG} 2>&1'"

mkdir -p "$(dirname "$LOG")"
mkdir -p "$(dirname "$LOCK")"

existing="$(crontab -l 2>/dev/null || true)"

filtered="$(
  printf '%s\n' "$existing" | grep -Fv "$MARKER" || true
)"

if printf '%s\n' "$existing" | grep -F "$MARKER" >/dev/null; then
  if printf '%s\n' "$existing" | grep -F "flock -n" | grep -F "$MARKER" >/dev/null; then
    echo "Backtest sync cron already installed with non-blocking flock. No change."
    printf '%s\n' "$existing" | grep -F "$MARKER" || true
    exit 0
  fi
  echo "Replacing existing backtest sync cron with a flock-protected line."
fi

{
  printf '%s\n' "$filtered"
  echo "$LINE"
} | crontab -

echo "Installed backtest sync cron:"
echo "  $LINE"
echo
echo "A second one-minute invocation exits immediately if a sync is still running."
echo
echo "Manual equivalent:"
echo "  flock -n ${LOCK} -c 'cd ${ROOT} && ${PYTHON} -m jobs.sync_quantconnect --backtests-only'"
echo
echo "The existing live QuantConnect sync cadence was not modified."
