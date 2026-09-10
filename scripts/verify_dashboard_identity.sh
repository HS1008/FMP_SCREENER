#!/usr/bin/env bash
# Load host Streamlit identity and verify it. Never prints URLs or passwords.
# Exit 0 ok or explicit writer fallback, 2 mutations succeeded, 3 identity missing.
set -euo pipefail

load_dashboard_env() {
  if [ "${FMP_IDENTITY_ENV_ONLY:-}" = "1" ]; then
    files=("${FMP_DASHBOARD_ENV:-}")
  else
    files=(
      /etc/fmp/fmp-dashboard.env
      /root/FMP_SCREENER/.env
      "${FMP_DASHBOARD_ENV:-}"
    )
  fi
  for f in "${files[@]}"; do
    if [ -n "${f:-}" ] && [ -f "$f" ]; then
      set -a
      # shellcheck disable=SC1090
      . "$f"
      set +a
    fi
  done
}

load_dashboard_env
if command -v python >/dev/null 2>&1; then
  PYTHON_BIN=python
else
  PYTHON_BIN=python3
fi
set +e
"$PYTHON_BIN" -m jobs.verify_dashboard_readonly
rc=$?
set -e
if [ "$rc" = "0" ]; then
  echo "dashboard_readonly_verify=ok"
  exit 0
fi
if [ "$rc" = "2" ]; then
  echo "dashboard_readonly_verify=failed"
  exit 2
fi
fallback=$(printf '%s' "${DASHBOARD_ALLOW_WRITER_FALLBACK:-}" | tr '[:upper:]' '[:lower:]')
if [ "$fallback" = "1" ] || [ "$fallback" = "true" ] || [ "$fallback" = "yes" ] || [ "$fallback" = "on" ]; then
  echo "dashboard_readonly_verify=skipped_explicit_writer_fallback"
  exit 0
fi
echo "DASHBOARD_READONLY_URL required (or DASHBOARD_ALLOW_WRITER_FALLBACK=1)"
exit 3
