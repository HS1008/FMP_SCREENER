#!/usr/bin/env bash
# Load host Streamlit identity and verify it. Never prints URLs or passwords.
# Exit 0 ok, 2 mutations succeeded, 3 identity missing, 4 writer fallback refused, 5 provider fetch refused.
set -euo pipefail

load_dashboard_env() {
  if [ "${FMP_IDENTITY_ENV_ONLY:-}" = "1" ]; then
    files=("${FMP_DASHBOARD_ENV:-}")
  else
    files=(
      /etc/fmp/fmp-dashboard.env
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

clear_inherited_writer_env() {
  # Parent shells (activate_market_intelligence_host.sh) source the writer
  # checkout .env before calling this script. Identity verify must not see
  # those keys, and must not treat them as a dashboard fallback.
  unset DATABASE_URL MARKET_INTELLIGENCE_DATABASE_URL
  unset DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD
}

refuse_writer_keys_in_dashboard_env() {
  if [ -n "${DATABASE_URL:-}" ] || [ -n "${DB_HOST:-}" ] || [ -n "${DB_USER:-}" ] || \
     [ -n "${DB_PASSWORD:-}" ] || [ -n "${MARKET_INTELLIGENCE_DATABASE_URL:-}" ]; then
    echo "dashboard_readonly_verify=writer_fallback_refused"
    echo "dashboard env must not carry writer database keys"
    exit 4
  fi
}

clear_inherited_writer_env
load_dashboard_env
refuse_writer_keys_in_dashboard_env
fallback=$(printf '%s' "${DASHBOARD_ALLOW_WRITER_FALLBACK:-}" | tr '[:upper:]' '[:lower:]')
if [ "$fallback" = "1" ] || [ "$fallback" = "true" ] || [ "$fallback" = "yes" ] || [ "$fallback" = "on" ]; then
  echo "dashboard_readonly_verify=writer_fallback_refused"
  echo "DASHBOARD_ALLOW_WRITER_FALLBACK is not a production deploy path"
  exit 4
fi
provider=$(printf '%s' "${STREAMLIT_ALLOW_PROVIDER_FETCH:-}" | tr '[:upper:]' '[:lower:]')
if [ "$provider" = "1" ] || [ "$provider" = "true" ] || [ "$provider" = "yes" ] || [ "$provider" = "on" ]; then
  echo "dashboard_readonly_verify=provider_fetch_refused"
  echo "STREAMLIT_ALLOW_PROVIDER_FETCH is not a production deploy path"
  exit 5
fi
if [ -n "${DASHBOARD_READONLY_URL:-}" ]; then
  streamlit_ro=$(printf '%s' "${FMP_STREAMLIT_READONLY:-}" | tr '[:upper:]' '[:lower:]')
  if [ "$streamlit_ro" != "1" ] && [ "$streamlit_ro" != "true" ] && [ "$streamlit_ro" != "yes" ] && [ "$streamlit_ro" != "on" ]; then
    echo "dashboard_readonly_verify=streamlit_readonly_missing"
    echo "FMP_STREAMLIT_READONLY must be enabled in the dashboard env"
    exit 4
  fi
fi
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
echo "DASHBOARD_READONLY_URL required"
exit 3
