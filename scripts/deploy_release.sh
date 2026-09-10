#!/usr/bin/env bash
# Immutable release deployment (validated beside the existing git-pull flow).
# Does not replace .github/workflows/deploy.yml until this path is proven.
#
#   scripts/deploy_release.sh --sha <git_sha>
#   scripts/deploy_release.sh --rollback
#
# Layout:
#   /opt/fmp/releases/<sha>/   immutable checkout
#   /opt/fmp/current           symlink to the active release
#   /etc/fmp/...               host config / secrets
#   /var/lib/fmp/...           host state
#   /var/log/fmp/...           logs
set -euo pipefail

REPO_URL="${FMP_REPO_URL:-https://github.com/hs1008/fmp_screener.git}"
RELEASE_ROOT="${FMP_RELEASE_ROOT:-/opt/fmp/releases}"
CURRENT_LINK="${FMP_CURRENT_LINK:-/opt/fmp/current}"
PREVIOUS_LINK="${FMP_PREVIOUS_LINK:-/opt/fmp/previous}"
SHA=""
ROLLBACK=0
SKIP_RESTART=0
SKIP_PREFLIGHT=0
SKIP_IDENTITY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA="$2"; shift 2 ;;
    --rollback) ROLLBACK=1; shift ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --release-root) RELEASE_ROOT="$2"; shift 2 ;;
    --skip-restart) SKIP_RESTART=1; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    --skip-identity) SKIP_IDENTITY=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

if [ "$ROLLBACK" = 1 ]; then
  if [ ! -L "$PREVIOUS_LINK" ]; then
    echo "No previous release symlink at $PREVIOUS_LINK"
    exit 2
  fi
  prev="$(readlink -f "$PREVIOUS_LINK")"
  if [ "$SKIP_IDENTITY" = 1 ]; then
    echo "rollback_identity=skipped"
  else
    if [ ! -f "$prev/scripts/verify_dashboard_identity.sh" ]; then
      echo "rollback target is missing scripts/verify_dashboard_identity.sh"
      exit 3
    fi
    (
      cd "$prev"
      python3 -m qc_research.contracts.digests
      bash scripts/provision_dashboard_readonly.sh --require
      export FMP_IDENTITY_ENV_ONLY=1
      export FMP_DASHBOARD_ENV="${FMP_DASHBOARD_ENV:-/etc/fmp/fmp-dashboard.env}"
      bash scripts/verify_dashboard_identity.sh
    )
  fi
  if [ -L "$CURRENT_LINK" ]; then
    ln -sfn "$(readlink -f "$CURRENT_LINK")" "${PREVIOUS_LINK}.swap"
    ln -sfn "$prev" "$CURRENT_LINK"
    ln -sfn "$(readlink -f "${PREVIOUS_LINK}.swap")" "$PREVIOUS_LINK"
    rm -f "${PREVIOUS_LINK}.swap"
  else
    ln -sfn "$prev" "$CURRENT_LINK"
  fi
  echo "rolled_back_to=$(basename "$prev")"
  if [ "$SKIP_RESTART" != 1 ]; then
    systemctl restart fmp-dashboard
  fi
  exit 0
fi

if [ -z "$SHA" ]; then
  echo "Usage: $0 --sha <git_sha>" >&2
  exit 64
fi

target="$RELEASE_ROOT/$SHA"
install -d -m 0755 "$RELEASE_ROOT"
if [ ! -d "$target/.git" ]; then
  git clone --depth 1 "$REPO_URL" "$target"
fi
git -C "$target" fetch --depth 1 origin "$SHA"
git -C "$target" checkout --detach "$SHA"
actual="$(git -C "$target" rev-parse HEAD)"
if [ "$actual" != "$SHA" ]; then
  echo "immutable release HEAD ${actual} does not match requested ${SHA}"
  exit 3
fi

if [ "$SKIP_PREFLIGHT" != 1 ]; then
  if [ -f "$target/requirements.txt" ]; then
    python3 -m pip install -r "$target/requirements.txt"
  fi
  (
    cd "$target"
    python -m jobs.apply_migrations
    python -m pytest -q tests/test_deploy_release.py tests/test_ui_boundary.py tests/test_surface_status.py
  )
fi
if [ "$SKIP_IDENTITY" != 1 ]; then
  (
    cd "$target"
    python -m qc_research.contracts.digests
    bash scripts/provision_dashboard_readonly.sh --require
    export FMP_IDENTITY_ENV_ONLY=1
    export FMP_DASHBOARD_ENV="${FMP_DASHBOARD_ENV:-/etc/fmp/fmp-dashboard.env}"
    bash scripts/verify_dashboard_identity.sh
  )
fi

if [ -L "$CURRENT_LINK" ]; then
  ln -sfn "$(readlink -f "$CURRENT_LINK")" "$PREVIOUS_LINK"
fi
ln -sfn "$target" "$CURRENT_LINK"
echo "current_release=$SHA"
if [ "$SKIP_RESTART" != 1 ] && systemctl is-enabled fmp-dashboard >/dev/null 2>&1; then
  systemctl restart fmp-dashboard
fi
