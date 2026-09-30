#!/usr/bin/env bash
# Idempotently install the server Yahoo dashboard quote collector.
# Streamlit does not run this job. flock prevents a second collector.
set -euo pipefail

ARG_ROOT="${1:-/root/FMP_SCREENER}"
STAGED_ROOT="${2:-}"
LOCK_ROOT="$ARG_ROOT"
CODE_ROOT="$ARG_ROOT"
if [ -n "$STAGED_ROOT" ] && [ -f "$STAGED_ROOT/jobs/yahoo_dashboard_quotes.py" ] && [ -x "$STAGED_ROOT/venv/bin/python" ]; then
  CODE_ROOT="$STAGED_ROOT"
elif [ "$ARG_ROOT" = "/root/FMP_SCREENER" ] && [ -d /opt/fmp/current ] && [ -f /opt/fmp/current/jobs/yahoo_dashboard_quotes.py ]; then
  CODE_ROOT="/opt/fmp/current"
fi
if [ -x "${CODE_ROOT}/venv/bin/python" ]; then
  PYTHON="${CODE_ROOT}/venv/bin/python"
elif [ -x "${LOCK_ROOT}/venv/bin/python" ]; then
  PYTHON="${LOCK_ROOT}/venv/bin/python"
else
  PYTHON="${CODE_ROOT}/venv/bin/python"
fi
LOG="${LOCK_ROOT}/outputs/yahoo_dashboard_quotes.log"
LOCK="${LOCK_ROOT}/outputs/yahoo_dashboard_quotes.flock"
CHECKOUT_ENV="${LOCK_ROOT}/.env"
WRITER_ENV="/etc/fmp/fmp-writer.env"
MARKER="jobs.yahoo_dashboard_quotes"
LINE="*/15 * * * * flock -n ${LOCK} -c 'set -a; [ -f ${CHECKOUT_ENV} ] && . ${CHECKOUT_ENV}; [ -f ${WRITER_ENV} ] && . ${WRITER_ENV}; set +a; unset FMP_STREAMLIT_READONLY STREAMLIT_ALLOW_PROVIDER_FETCH DASHBOARD_ALLOW_WRITER_FALLBACK; cd ${CODE_ROOT} && ${PYTHON} -m jobs.yahoo_dashboard_quotes >> ${LOG} 2>&1'"

mkdir -p "$(dirname "$LOG")"
mkdir -p "$(dirname "$LOCK")"

existing="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$existing" | grep -Fv "$MARKER" || true)"

if printf '%s\n' "$existing" | grep -Fx "$LINE" >/dev/null; then
  echo "Yahoo quote cron already installed. No change."
  exit 0
fi

{
  printf '%s\n' "$filtered"
  echo "$LINE"
} | crontab -

echo "Installed Yahoo dashboard quote cron:"
echo "  $LINE"
