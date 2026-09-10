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
SKIP_MIGRATE=0
NO_ACTIVATE=0
STAGE_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA="$2"; shift 2 ;;
    --rollback) ROLLBACK=1; shift ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --release-root) RELEASE_ROOT="$2"; shift 2 ;;
    --skip-restart) SKIP_RESTART=1; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    --skip-identity) SKIP_IDENTITY=1; shift ;;
    --skip-migrate) SKIP_MIGRATE=1; shift ;;
    --no-activate) NO_ACTIVATE=1; shift ;;
    --stage-only)
      STAGE_ONLY=1
      NO_ACTIVATE=1
      SKIP_MIGRATE=1
      SKIP_PREFLIGHT=1
      SKIP_RESTART=1
      shift
      ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

if [ "$SKIP_IDENTITY" = 1 ] && [ "${DEPLOY_BREAK_GLASS:-}" != "1" ]; then
  echo "refusing --skip-identity without DEPLOY_BREAK_GLASS=1"
  exit 4
fi
if [ "$SKIP_IDENTITY" = 1 ]; then
  echo "deploy_break_glass=1 skip_identity=1"
fi

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
      PY="$prev/venv/bin/python"
      if [ ! -x "$PY" ]; then PY=python3; fi
      "$PY" -m qc_research.contracts.digests
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
    if [ "$SKIP_IDENTITY" != 1 ]; then
      POST_VERIFY_RC=0
      (
        cd "$prev"
        export FMP_IDENTITY_ENV_ONLY=1
        export FMP_DASHBOARD_ENV="${FMP_DASHBOARD_ENV:-/etc/fmp/fmp-dashboard.env}"
        bash scripts/verify_dashboard_identity.sh
      ) || POST_VERIFY_RC=$?
      if [ "$POST_VERIFY_RC" != "0" ]; then
        echo "post-restart dashboard identity verify failed rc=${POST_VERIFY_RC}"
        exit "$POST_VERIFY_RC"
      fi
    fi
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

# Proposed systemd ExecStart is /opt/fmp/current/venv/bin/streamlit.
# --skip-preflight skips migrations/pytest, not a bootable release venv.
# --skip-identity (break-glass layout-only) may omit the venv.
if [ ! -x "$target/venv/bin/streamlit" ]; then
  if [ ! -f "$target/requirements.txt" ]; then
    if [ "$SKIP_IDENTITY" = 1 ] || [ "$STAGE_ONLY" = 1 ]; then
      echo "layout_only_skip_venv=1"
    else
      echo "FAIL: release tree is missing requirements.txt"
      exit 3
    fi
  else
    echo "Creating release venv at $target/venv"
    python3 -m venv "$target/venv"
    "$target/venv/bin/pip" install -r "$target/requirements.txt"
  fi
fi
if [ "$SKIP_IDENTITY" != 1 ] && [ "$STAGE_ONLY" != 1 ] && [ ! -x "$target/venv/bin/streamlit" ]; then
  echo "FAIL: release venv is missing streamlit at $target/venv/bin/streamlit"
  exit 3
fi

if [ "$SKIP_PREFLIGHT" != 1 ]; then
  (
    cd "$target"
    # shellcheck disable=SC1091
    source "$target/venv/bin/activate"
    # Release trees have no local dotenv. Prefer the host writer env;
    # Python load_writer_dotenv() still fills the git-pull checkout dotenv.
    if [ -f /etc/fmp/fmp-writer.env ]; then
      set -a
      # shellcheck disable=SC1091
      . /etc/fmp/fmp-writer.env
      set +a
    fi
    unset FMP_STREAMLIT_READONLY STREAMLIT_ALLOW_PROVIDER_FETCH DASHBOARD_ALLOW_WRITER_FALLBACK
    PY="$target/venv/bin/python"
    if [ ! -x "$PY" ]; then
      echo "FAIL: staged interpreter missing at $target/venv/bin/python"
      exit 3
    fi
    if [ "$SKIP_MIGRATE" != 1 ]; then
      "$PY" -m jobs.apply_migrations
    fi
    "$PY" -m pytest -q tests/test_deploy_release.py tests/test_ui_boundary.py tests/test_surface_status.py
  )
fi
if [ "$STAGE_ONLY" = 1 ]; then
  echo "stage_only=1 provision=skipped activate=skipped"
fi
if [ "$SKIP_IDENTITY" != 1 ] && [ "$STAGE_ONLY" != 1 ]; then
  (
    cd "$target"
    PY="$target/venv/bin/python"
    if [ ! -x "$PY" ]; then
      echo "FAIL: staged interpreter missing at $target/venv/bin/python"
      exit 3
    fi
    "$PY" -m qc_research.contracts.digests
    bash scripts/provision_dashboard_readonly.sh --require --root "$target"
    export FMP_PYTHON="$PY"
    export FMP_IDENTITY_ENV_ONLY=1
    export FMP_DASHBOARD_ENV="${FMP_DASHBOARD_ENV:-/etc/fmp/fmp-dashboard.env}"
    bash scripts/verify_dashboard_identity.sh
  )
fi

if [ "$NO_ACTIVATE" = 1 ]; then
  echo "staged_release=$SHA"
  echo "activate=skipped"
  exit 0
fi

if [ -L "$CURRENT_LINK" ]; then
  ln -sfn "$(readlink -f "$CURRENT_LINK")" "$PREVIOUS_LINK"
fi
ln -sfn "$target" "$CURRENT_LINK"
echo "current_release=$SHA"
if [ "$SKIP_RESTART" != 1 ] && systemctl is-enabled fmp-dashboard >/dev/null 2>&1; then
  systemctl restart fmp-dashboard
  if [ "$SKIP_IDENTITY" != 1 ]; then
    POST_VERIFY_RC=0
    (
      cd "$target"
      export FMP_IDENTITY_ENV_ONLY=1
      export FMP_DASHBOARD_ENV="${FMP_DASHBOARD_ENV:-/etc/fmp/fmp-dashboard.env}"
      bash scripts/verify_dashboard_identity.sh
    ) || POST_VERIFY_RC=$?
    if [ "$POST_VERIFY_RC" != "0" ]; then
      echo "post-restart dashboard identity verify failed rc=${POST_VERIFY_RC}"
      exit "$POST_VERIFY_RC"
    fi
  fi
fi
