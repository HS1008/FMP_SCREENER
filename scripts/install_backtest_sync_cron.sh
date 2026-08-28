#!/usr/bin/env bash
# Idempotently install a one-minute QuantConnect *backtest* sync cron.
#
# Deploy (`.github/workflows/deploy.yml`) runs this automatically after
# migrations. Re-running is safe: an identical flock-protected line is
# left unchanged.
#
# Behavior:
#   - one-minute `--backtests-only` ingest
#   - protected by `flock -n` on outputs/backtest_sync.flock
#   - a second invocation exits immediately if a sync is still running
#   - does NOT modify the existing ~10-minute live QuantConnect cadence
#
# Production verification uses the SAME lock file (with `flock -w`) so
# cron and verifier never write PostgreSQL at the same time.
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
