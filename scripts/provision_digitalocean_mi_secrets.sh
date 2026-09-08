#!/usr/bin/env bash
# Provision Market Intelligence secrets on the existing DigitalOcean host env file.
#
# Default is a dry run: print the planned edits, change nothing, start nothing.
# This script never enables systemd timers, never restarts Streamlit or the API,
# never runs a FRED refresh, and never prints secret values.
#
#   scripts/provision_digitalocean_mi_secrets.sh
#   scripts/provision_digitalocean_mi_secrets.sh --apply --fred-key-file /root/FMP_SCREENER/.secrets/fred_api_key
#
# The key file must be mode 0600 (or 0400). The value is read from the file, never
# from argv or the process command line. Existing keys in the env file are preserved;
# only missing or placeholder FRED_API_KEY is replaced.
set -euo pipefail

ENV_FILE="/etc/fmp/market_intelligence.env"
EXAMPLE="deploy/market_intelligence/market_intelligence.env.example"
FRED_KEY_FILE=""
APPLY=0
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --fred-key-file) FRED_KEY_FILE="$2"; shift 2 ;;
    --root) ROOT="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

echo "DigitalOcean Market Intelligence secret provisioning"
echo "  env file : ${ENV_FILE}"
echo "  apply    : ${APPLY}"
echo "  actions  : write FRED_API_KEY if missing/placeholder; preserve every other line"
echo "  will NOT : enable timers, restart services, run ingest, deploy, or print the key"

if [ ! -f "${ENV_FILE}" ]; then
  echo "  env file does not exist yet"
  echo "  planned: install -m 0600 ${ROOT}/${EXAMPLE} ${ENV_FILE}"
  echo "  then fill writer identity / tokens by hand; this script only sets FRED_API_KEY"
fi

if [ -z "${FRED_KEY_FILE}" ]; then
  echo "  FRED_API_KEY source file not provided (--fred-key-file)"
  echo "  dry-run / apply will not write a key until that file is supplied"
  if [ "${APPLY}" -eq 1 ]; then
    echo "refusing --apply without --fred-key-file" >&2
    exit 3
  fi
  exit 0
fi

if [ ! -f "${FRED_KEY_FILE}" ]; then
  echo "fred key file not found" >&2
  exit 3
fi
MODE="$(stat -c '%a' "${FRED_KEY_FILE}" 2>/dev/null || stat -f '%OLp' "${FRED_KEY_FILE}")"
if [ "${MODE}" != "600" ] && [ "${MODE}" != "400" ]; then
  echo "refusing to read a key file with mode ${MODE} (require 0600 or 0400)" >&2
  exit 3
fi

if [ "${APPLY}" -eq 0 ]; then
  echo "DRY RUN: would set FRED_API_KEY from the protected file (value not shown)"
  exit 0
fi

umask 077
if [ ! -f "${ENV_FILE}" ]; then
  install -m 0600 "${ROOT}/${EXAMPLE}" "${ENV_FILE}"
fi
TMP="$(mktemp "${ENV_FILE}.XXXX")"
python3 - "${ENV_FILE}" "${FRED_KEY_FILE}" "${TMP}" <<'PY'
import pathlib, re, sys
env_path, key_path, tmp_path = sys.argv[1], sys.argv[2], sys.argv[3]
key = pathlib.Path(key_path).read_text(encoding="utf-8").strip()
if not key or any(ch.isspace() for ch in key):
    raise SystemExit("fred key file is empty or contains whitespace")
text = pathlib.Path(env_path).read_text(encoding="utf-8")
pattern = re.compile(r"^#?\s*FRED_API_KEY=.*$", re.M)
replacement = "FRED_API_KEY=" + key
if pattern.search(text):
    text = pattern.sub(replacement, text, count=1)
else:
    if text and not text.endswith("\n"):
        text += "\n"
    text += replacement + "\n"
pathlib.Path(tmp_path).write_text(text, encoding="utf-8")
PY
chmod 0600 "${TMP}"
mv "${TMP}" "${ENV_FILE}"
echo "FRED_API_KEY written to env file (value not printed). Services were not restarted."
echo "Production remains inactive until a human enables the refresh timer and reviews Data Health."
