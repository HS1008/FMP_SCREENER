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

while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA="$2"; shift 2 ;;
    --rollback) ROLLBACK=1; shift ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --release-root) RELEASE_ROOT="$2"; shift 2 ;;
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
  if [ -L "$CURRENT_LINK" ]; then
    ln -sfn "$(readlink -f "$CURRENT_LINK")" "${PREVIOUS_LINK}.swap"
    ln -sfn "$prev" "$CURRENT_LINK"
    ln -sfn "$(readlink -f "${PREVIOUS_LINK}.swap")" "$PREVIOUS_LINK"
    rm -f "${PREVIOUS_LINK}.swap"
  else
    ln -sfn "$prev" "$CURRENT_LINK"
  fi
  echo "rolled_back_to=$(basename "$prev")"
  systemctl restart fmp-dashboard
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
  git -C "$target" fetch --depth 1 origin "$SHA"
  git -C "$target" checkout --detach "$SHA"
fi

if [ -f "$target/requirements.txt" ]; then
  python3 -m pip install -r "$target/requirements.txt"
fi

(
  cd "$target"
  python -m jobs.apply_migrations
)

if [ -L "$CURRENT_LINK" ]; then
  ln -sfn "$(readlink -f "$CURRENT_LINK")" "$PREVIOUS_LINK"
fi
ln -sfn "$target" "$CURRENT_LINK"
echo "current_release=$SHA"
if systemctl is-enabled fmp-dashboard >/dev/null 2>&1; then
  systemctl restart fmp-dashboard
fi
