#!/usr/bin/env bash
# Provision Market Intelligence secrets on the existing DigitalOcean host env file.
#
# Default is a dry run: print the planned edits, change nothing, start nothing.
# This script never enables systemd timers, never restarts Streamlit or the API,
# never runs a provider refresh, and never prints secret values.
#
# Existing non-placeholder keys are preserved unless --rotate is passed.
# Intentional rotation is explicit, atomic, and limited to allowed key names.
#
#   scripts/provision_digitalocean_mi_secrets.sh
#   scripts/provision_digitalocean_mi_secrets.sh --apply --fred-key-file /root/FMP_SCREENER/.secrets/fred_api_key
#   scripts/provision_digitalocean_mi_secrets.sh --apply --rotate --eia-key-file /root/FMP_SCREENER/.secrets/eia_api_key
set -euo pipefail

ENV_FILE="/etc/fmp/market_intelligence.env"
EXAMPLE="deploy/market_intelligence/market_intelligence.env.example"
FRED_KEY_FILE=""
EIA_KEY_FILE=""
OPENFIGI_KEY_FILE=""
CBOE_ID_FILE=""
CBOE_SECRET_FILE=""
APPLY=0
ROTATE=0
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --fred-key-file) FRED_KEY_FILE="$2"; shift 2 ;;
    --eia-key-file) EIA_KEY_FILE="$2"; shift 2 ;;
    --openfigi-key-file) OPENFIGI_KEY_FILE="$2"; shift 2 ;;
    --cboe-id-file) CBOE_ID_FILE="$2"; shift 2 ;;
    --cboe-secret-file) CBOE_SECRET_FILE="$2"; shift 2 ;;
    --root) ROOT="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --rotate) ROTATE=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

echo "DigitalOcean Market Intelligence secret provisioning"
echo "  env file : ${ENV_FILE}"
echo "  apply    : ${APPLY}"
echo "  rotate   : ${ROTATE}"
echo "  actions  : write allowed keys if missing/placeholder; preserve non-placeholder unless --rotate"
echo "  will NOT : enable timers, restart services, run ingest, deploy, or print keys"

if [ ! -f "${ENV_FILE}" ]; then
  echo "  env file does not exist yet"
  echo "  planned: install -m 0600 ${ROOT}/${EXAMPLE} ${ENV_FILE}"
fi

need_any=0
[ -n "${FRED_KEY_FILE}" ] && need_any=1
[ -n "${EIA_KEY_FILE}" ] && need_any=1
[ -n "${OPENFIGI_KEY_FILE}" ] && need_any=1
[ -n "${CBOE_ID_FILE}" ] && need_any=1
[ -n "${CBOE_SECRET_FILE}" ] && need_any=1
if [ "${need_any}" -eq 0 ]; then
  echo "  no key files provided (--fred-key-file / --eia-key-file / --openfigi-key-file / --cboe-id-file / --cboe-secret-file)"
  if [ "${APPLY}" -eq 1 ]; then
    echo "refusing --apply without a key file" >&2
    exit 3
  fi
  exit 0
fi

check_key_file() {
  local path="$1"
  if [ -z "${path}" ]; then
    return 0
  fi
  if [ ! -f "${path}" ]; then
    echo "key file not found" >&2
    exit 3
  fi
  local mode
  mode="$(stat -c '%a' "${path}" 2>/dev/null || stat -f '%OLp' "${path}")"
  if [ "${mode}" != "600" ] && [ "${mode}" != "400" ]; then
    echo "refusing to read a key file with mode ${mode} (require 0600 or 0400)" >&2
    exit 3
  fi
}

check_key_file "${FRED_KEY_FILE}"
check_key_file "${EIA_KEY_FILE}"
check_key_file "${OPENFIGI_KEY_FILE}"
check_key_file "${CBOE_ID_FILE}"
check_key_file "${CBOE_SECRET_FILE}"

if [ "${APPLY}" -eq 0 ]; then
  echo "DRY RUN: would upsert missing/placeholder keys from protected files (values not shown)"
  exit 0
fi

umask 077
if [ ! -f "${ENV_FILE}" ]; then
  install -m 0600 "${ROOT}/${EXAMPLE}" "${ENV_FILE}"
fi
TMP="$(mktemp "${ENV_FILE}.XXXX")"
python3 - "${ENV_FILE}" "${TMP}" "${ROOT}" "${ROTATE}" "${FRED_KEY_FILE}" "${EIA_KEY_FILE}" "${OPENFIGI_KEY_FILE}" "${CBOE_ID_FILE}" "${CBOE_SECRET_FILE}" <<'PY'
import pathlib, sys
env_path, tmp_path, root, rotate_flag = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, root)
from scripts.protected_env import upsert_if_placeholder
rotate = rotate_flag == "1"
text = pathlib.Path(env_path).read_text(encoding="utf-8")
actions = []
pairs = [
    ("FRED_API_KEY", sys.argv[5]),
    ("EIA_API_KEY", sys.argv[6]),
    ("OPENFIGI_API_KEY", sys.argv[7]),
    ("CBOE_CLIENT_ID", sys.argv[8] if len(sys.argv) > 8 else ""),
    ("CBOE_CLIENT_SECRET", sys.argv[9] if len(sys.argv) > 9 else ""),
]
for key, path in pairs:
    if not path:
        continue
    value = pathlib.Path(path).read_text(encoding="utf-8").strip()
    text, action = upsert_if_placeholder(text, key, value, rotate=rotate)
    actions.append(key + "=" + action)
pathlib.Path(tmp_path).write_text(text, encoding="utf-8")
print("key_actions=" + ",".join(actions))
PY
chmod 0600 "${TMP}"
mv "${TMP}" "${ENV_FILE}"
echo "Allowed keys written or preserved (values not printed). Services were not restarted."
echo "Credential presence does not enable ingestion. Production remains inactive until a human enables the matching flag/timer."
