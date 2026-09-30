#!/usr/bin/env bash
# Idempotent bootstrap for the FMP screener dev environment.
# Installs the Python venv toolchain (missing from the base image) and project
# dependencies into a local .venv.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# The base image ships CPython but not the stdlib venv/ensurepip bootstrap.
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends python3-venv
fi

if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

# shellcheck source=/dev/null
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "FMP screener environment ready."
