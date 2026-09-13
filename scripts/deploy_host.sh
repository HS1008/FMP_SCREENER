#!/usr/bin/env bash
# Versioned production host orchestration.
# GitHub Actions must stay thin: pin SSH, connect, invoke this script.
#
#   bash scripts/deploy_host.sh --sha <40-hex>
#
# Everyday auto-deploy stages /opt/fmp/releases/<sha>, validates, then may
# mirror the checkout and restart the existing unit. systemd cutover to
# /opt/fmp/current is a human action. Stage-only never flips current/previous.
# Migrations run ONCE from the staged release SHA.
set -euo pipefail
LOCK_FILE="${FMP_DEPLOY_LOCK:-/var/lock/fmp-deploy.lock}"
if ! (umask 077; : > "$LOCK_FILE") 2>/dev/null; then
  LOCK_FILE="/tmp/fmp-deploy.lock"
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "FAIL: another deploy holds $LOCK_FILE"
  exit 75
fi

ROOT="${FMP_CHECKOUT:-/root/FMP_SCREENER}"
RELEASE_ROOT="${FMP_RELEASE_ROOT:-/opt/fmp/releases}"
CURRENT_LINK="${FMP_CURRENT_LINK:-/opt/fmp/current}"
DASHBOARD_ENV="${FMP_DASHBOARD_ENV:-/etc/fmp/fmp-dashboard.env}"
WRITER_ENV="${FMP_WRITER_ENV:-/etc/fmp/fmp-writer.env}"
SECRET_DIR="/etc/fmp/secrets"
# Host secret path: /etc/fmp/secrets/dashboard_readonly.pw (never committed).
HOST_PW_FILE="$SECRET_DIR/dashboard_readonly.pw"
# Legacy checkout path: /root/FMP_SCREENER/.secrets/dashboard_readonly.pw
LEGACY_PW_FILE="$ROOT/.secrets/dashboard_readonly.pw"
SHA=""
SKIP_RESTART=0

while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA="$2"; shift 2 ;;
    --root) ROOT="$2"; shift 2 ;;
    --skip-restart) SKIP_RESTART=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

if ! printf '%s' "$SHA" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "FAIL: --sha must be a 40-character lowercase git SHA"
  exit 3
fi

cd "$ROOT"

echo "deploy_host=start sha=$SHA"
echo "Preserving host-modified activation scripts outside the checkout (not secrets)"
preserve_dir=/root/fmp_backups/checkout_preserve
install -d -m 0700 "$preserve_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
for rel in scripts/activate_market_intelligence_host.sh scripts/materialize_ai_context_env.py scripts/install_market_intelligence_timers.sh; do
  if [ ! -e "$rel" ]; then
    continue
  fi
  tracked=0
  git ls-files --error-unmatch -- "$rel" >/dev/null 2>&1 && tracked=1
  dirty=0
  if [ "$tracked" = 1 ] && ! git diff --quiet -- "$rel"; then
    dirty=1
  fi
  if [ "$tracked" = 0 ]; then
    dirty=1
  fi
  if [ "$dirty" = 1 ]; then
    echo "preserving $rel -> $preserve_dir"
    cp -a "$rel" "$preserve_dir/$(basename "$rel").$stamp"
    if [ "$tracked" = 1 ]; then
      git checkout -- "$rel"
    else
      rm -f "$rel"
    fi
  fi
done
echo "working_tree_porcelain=$(git status --porcelain | tr '\n' '|')"
echo "prepare=skip_checkout_pull requested_sha=$SHA"
PREV_HEAD="$(git rev-parse HEAD 2>/dev/null || true)"
STATE_DIR="${FMP_DEPLOY_STATE:-/var/lib/fmp/deploy}"
install -d -m 0755 "$STATE_DIR" 2>/dev/null || true
if [ -n "$PREV_HEAD" ]; then
  printf '%s\n' "$PREV_HEAD" > "$STATE_DIR/checkout_before_activate.sha" || true
fi

if [ -s "$LEGACY_PW_FILE" ] && [ ! -s "$HOST_PW_FILE" ]; then
  install -d -m 0700 "$SECRET_DIR" 2>/dev/null || true
  if cp -a "$LEGACY_PW_FILE" "$HOST_PW_FILE" 2>/dev/null; then
    chmod 0600 "$HOST_PW_FILE" || true
    echo "dashboard_readonly_pw=migrated_to_etc_fmp_secrets"
    echo "dashboard_readonly_pw_legacy_kept=1"
  fi
fi

if [ ! -f "$DASHBOARD_ENV" ]; then
  echo "FAIL: $DASHBOARD_ENV is required before irreversible deploy steps"
  exit 1
fi

echo "Staging immutable release (no migrate, no restart, no activate, no provision)..."
STAGED="$RELEASE_ROOT/$SHA"
REPO_URL="${FMP_REPO_URL:-https://github.com/hs1008/fmp_screener.git}"
CURRENT_BEFORE=""
if [ -L "$CURRENT_LINK" ] || [ -e "$CURRENT_LINK" ]; then
  CURRENT_BEFORE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
fi
IMMUTABLE_RC=0
install -d -m 0755 "$RELEASE_ROOT"
if [ ! -d "$STAGED/.git" ]; then
  if git clone --depth 1 "$REPO_URL" "$STAGED"; then
    :
  else
    git clone "$ROOT" "$STAGED"
  fi
fi
git -C "$STAGED" fetch --depth 1 origin "$SHA" || git -C "$STAGED" fetch "$ROOT" "$SHA"
git -C "$STAGED" checkout --detach "$SHA"
actual="$(git -C "$STAGED" rev-parse HEAD)"
if [ "$actual" != "$SHA" ]; then
  echo "FAIL: staged HEAD ${actual} does not match requested ${SHA}"
  exit 3
fi
if [ ! -x "$STAGED/venv/bin/python" ]; then
  if [ ! -f "$STAGED/requirements.txt" ]; then
    echo "FAIL: staged tree is missing requirements.txt"
    exit 3
  fi
  echo "Creating release venv at $STAGED/venv"
  python3 -m venv "$STAGED/venv"
  "$STAGED/venv/bin/pip" install -r "$STAGED/requirements.txt"
fi
if [ ! -x "$STAGED/venv/bin/streamlit" ]; then
  echo "FAIL: staged release venv is missing streamlit"
  exit 3
fi
if [ "$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)" != "$CURRENT_BEFORE" ]; then
  echo "FAIL: stage-only must not move current/previous pointers"
  exit 3
fi
echo "staged_release=$SHA activate=skipped"
CODE_ROOT="$STAGED"
if [ ! -x "$CODE_ROOT/venv/bin/python" ]; then
  echo "FAIL: staged release venv is missing; refusing to mutate the shared checkout venv first"
  exit 3
fi
PYTHON_BIN="$CODE_ROOT/venv/bin/python"

echo "Applying database migrations ONCE from the staged SHA..."
(
  cd "$CODE_ROOT"
  # shellcheck disable=SC1091
  source "$CODE_ROOT/venv/bin/activate"
  export PYTHONPATH="$CODE_ROOT"
  if [ -f "$WRITER_ENV" ]; then
    # Default WRITER_ENV=/etc/fmp/fmp-writer.env
    set -a
    # shellcheck disable=SC1091
    . "$WRITER_ENV"
    set +a
  elif [ -f "$ROOT/.env" ]; then
    # Default ROOT=/root/FMP_SCREENER so this sources /root/FMP_SCREENER/.env
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.env"
    set +a
  fi
  unset FMP_STREAMLIT_READONLY STREAMLIT_ALLOW_PROVIDER_FETCH DASHBOARD_ALLOW_WRITER_FALLBACK
  "$PYTHON_BIN" -m qc_research.contracts.digests
  "$PYTHON_BIN" -m jobs.apply_migrations
)

echo "Provisioning dashboard_readonly (password file required)..."
bash "$CODE_ROOT/scripts/provision_dashboard_readonly.sh" --require --root "$CODE_ROOT"

VERIFY_RC=0
echo "Verifying Streamlit database identity..."
export FMP_IDENTITY_ENV_ONLY=1
export FMP_DASHBOARD_ENV="$DASHBOARD_ENV"
(
  cd "$CODE_ROOT"
  # shellcheck disable=SC1091
  source "$CODE_ROOT/venv/bin/activate"
  export PYTHONPATH="$CODE_ROOT"
  export FMP_PYTHON="$PYTHON_BIN"
  bash scripts/verify_dashboard_identity.sh
) || VERIFY_RC=$?

echo "Installing 1-minute backtest sync cron (idempotent, flock-protected)..."
bash scripts/install_backtest_sync_cron.sh "$ROOT"

echo "Recording deploy identity (no secrets)..."
(
  set -a
  # shellcheck disable=SC1091
  . "$DASHBOARD_ENV"
  set +a
  unset DATABASE_URL DB_PASSWORD DB_USER DB_HOST DB_NAME DB_PORT MARKET_INTELLIGENCE_DATABASE_URL DASHBOARD_ALLOW_WRITER_FALLBACK
  "$PYTHON_BIN" -m jobs.report_deploy_identity \
    --sha "$SHA" \
    --checkout "$ROOT" \
    --mode git_pull \
    --immutable-rc "$IMMUTABLE_RC" \
    --verify-rc "$VERIFY_RC" \
    --env-file "$DASHBOARD_ENV"
)

if [ "$VERIFY_RC" != "0" ]; then
  echo "dashboard identity verify failed rc=${VERIFY_RC}"
  exit "$VERIFY_RC"
fi
echo "pre_restart_audit=deferred_until_after_restart"

echo "Verifying official CSFML V1 identity (read-only, missing run is not a deploy failure)..."
CSFML_RC=0
(
  cd "$CODE_ROOT"
  # shellcheck disable=SC1091
  source "$CODE_ROOT/venv/bin/activate"
  export PYTHONPATH="$CODE_ROOT"
  set -a
  # Default DASHBOARD_ENV=/etc/fmp/fmp-dashboard.env
  # shellcheck disable=SC1091
  . "$DASHBOARD_ENV"
  set +a
  unset DATABASE_URL DB_PASSWORD DB_USER DB_HOST DB_NAME DB_PORT MARKET_INTELLIGENCE_DATABASE_URL DASHBOARD_ALLOW_WRITER_FALLBACK
  "$PYTHON_BIN" -m qc_research.verify_csfml_v1 --live --code-root "$CODE_ROOT" --out /var/lib/fmp/deploy/csfml_v1_live.json
) || CSFML_RC=$?
if [ "$CSFML_RC" = "2" ] || [ "$CSFML_RC" = "4" ]; then
  echo "official CSFML V1 identity refused rc=${CSFML_RC}"
  exit "$CSFML_RC"
fi
if [ "$CSFML_RC" != "0" ]; then
  echo "official CSFML V1 query-back failed rc=${CSFML_RC}"
  exit "$CSFML_RC"
fi

echo "Verifying official TLT V0 identity (read-only, missing run is not a deploy failure)..."
TLT_RC=0
(
  cd "$CODE_ROOT"
  # shellcheck disable=SC1091
  source "$CODE_ROOT/venv/bin/activate"
  export PYTHONPATH="$CODE_ROOT"
  set -a
  # shellcheck disable=SC1091
  . "$DASHBOARD_ENV"
  set +a
  unset DATABASE_URL DB_PASSWORD DB_USER DB_HOST DB_NAME DB_PORT MARKET_INTELLIGENCE_DATABASE_URL DASHBOARD_ALLOW_WRITER_FALLBACK
  "$PYTHON_BIN" -m qc_research.verify_tlt_monitor --live --allow-missing --code-root "$CODE_ROOT" --out /var/lib/fmp/deploy/tlt_v0_live.json
) || TLT_RC=$?
if [ "$TLT_RC" = "2" ] || [ "$TLT_RC" = "4" ]; then
  echo "official TLT V0 identity refused rc=${TLT_RC}"
  exit "$TLT_RC"
fi
if [ "$TLT_RC" != "0" ]; then
  echo "official TLT V0 query-back failed rc=${TLT_RC}"
  exit "$TLT_RC"
fi

echo "Verifying official Stage 1 identity (read-only, missing run is not a deploy failure)..."
STAGE1_RC=0
(
  cd "$CODE_ROOT"
  # shellcheck disable=SC1091
  source "$CODE_ROOT/venv/bin/activate"
  export PYTHONPATH="$CODE_ROOT"
  set -a
  # Default DASHBOARD_ENV=/etc/fmp/fmp-dashboard.env
  # shellcheck disable=SC1091
  . "$DASHBOARD_ENV"
  set +a
  unset DATABASE_URL DB_PASSWORD DB_USER DB_HOST DB_NAME DB_PORT MARKET_INTELLIGENCE_DATABASE_URL DASHBOARD_ALLOW_WRITER_FALLBACK
  "$PYTHON_BIN" -m qc_research.verify_stage1 --live --code-root "$CODE_ROOT" --out /var/lib/fmp/deploy/stage1_live.json
) || STAGE1_RC=$?
if [ "$STAGE1_RC" = "2" ] || [ "$STAGE1_RC" = "4" ]; then
  echo "official Stage 1 identity refused rc=${STAGE1_RC}"
  exit "$STAGE1_RC"
fi
if [ "$STAGE1_RC" != "0" ]; then
  echo "official Stage 1 query-back failed rc=${STAGE1_RC}"
  exit "$STAGE1_RC"
fi

echo "Recording systemd cutover readiness (dry-run, no unit change)..."
CUTOVER_RC=0
(
  set -a
  # Default DASHBOARD_ENV=/etc/fmp/fmp-dashboard.env
  # shellcheck disable=SC1091
  . "$DASHBOARD_ENV"
  set +a
  unset DATABASE_URL DB_PASSWORD DB_USER DB_HOST DB_NAME DB_PORT MARKET_INTELLIGENCE_DATABASE_URL DASHBOARD_ALLOW_WRITER_FALLBACK
  "$PYTHON_BIN" -m jobs.cutover_dashboard_systemd \
    --verify-rc "$VERIFY_RC" \
    --env-file "$DASHBOARD_ENV" \
    --out /var/lib/fmp/deploy/cutover_readiness.json
) || CUTOVER_RC=$?
if [ "$CUTOVER_RC" != "0" ]; then
  echo "systemd cutover readiness failed rc=${CUTOVER_RC}"
  exit "$CUTOVER_RC"
fi

echo "Persisting sanitized deploy identity to PostgreSQL..."
(
  set -a
  if [ -f "$WRITER_ENV" ]; then
    # Default WRITER_ENV=/etc/fmp/fmp-writer.env
    # shellcheck disable=SC1091
    . "$WRITER_ENV"
  elif [ -f "$ROOT/.env" ]; then
    # Default ROOT=/root/FMP_SCREENER so this sources /root/FMP_SCREENER/.env
    # shellcheck disable=SC1091
    . "$ROOT/.env"
  fi
  set +a
  unset FMP_STREAMLIT_READONLY STREAMLIT_ALLOW_PROVIDER_FETCH DASHBOARD_ALLOW_WRITER_FALLBACK
  "$PYTHON_BIN" -m jobs.record_deploy_identity_db \
    --from /var/lib/fmp/deploy/current.json \
    --csfml /var/lib/fmp/deploy/csfml_v1_live.json \
    --tlt /var/lib/fmp/deploy/tlt_v0_live.json \
    --stage1 /var/lib/fmp/deploy/stage1_live.json
)

restore_checkout() {
  if [ -n "${PREV_HEAD:-}" ] && [ "$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)" != "$PREV_HEAD" ]; then
    echo "Restoring checkout to last pre-activate SHA $PREV_HEAD"
    git -C "$ROOT" fetch "$STAGED" "$PREV_HEAD" || true
    git -C "$ROOT" checkout --detach "$PREV_HEAD"
  fi
}

if [ "$SKIP_RESTART" != 1 ]; then
  echo "Activating existing checkout to requested SHA (does not flip /opt/fmp/current)..."
  git -C "$ROOT" fetch --update-head-ok "$STAGED" "$SHA"
  git -C "$ROOT" checkout --detach "$SHA"
  live_head="$(git -C "$ROOT" rev-parse HEAD)"
  if [ "$live_head" != "$SHA" ]; then
    echo "FAIL: checkout HEAD ${live_head} does not match requested ${SHA}"
    restore_checkout
    exit 3
  fi
  echo "Restarting Streamlit..."
  if ! systemctl restart fmp-dashboard; then
    echo "restart failed; restoring previous checkout"
    restore_checkout
    systemctl restart fmp-dashboard || true
    exit 1
  fi
  echo "Verifying Streamlit service..."
  if ! systemctl is-active --quiet fmp-dashboard; then
    echo "post-restart service inactive; restoring previous checkout"
    restore_checkout
    systemctl restart fmp-dashboard || true
    exit 1
  fi
  echo "Re-verifying Streamlit database identity after restart..."
  POST_VERIFY_RC=0
  (
    cd "$CODE_ROOT"
    # shellcheck disable=SC1091
    source "$CODE_ROOT/venv/bin/activate"
    export PYTHONPATH="$CODE_ROOT"
    export FMP_IDENTITY_ENV_ONLY=1
    export FMP_DASHBOARD_ENV="$DASHBOARD_ENV"
    bash scripts/verify_dashboard_identity.sh
  ) || POST_VERIFY_RC=$?
  if [ "$POST_VERIFY_RC" != "0" ]; then
    echo "post-restart dashboard identity verify failed rc=${POST_VERIFY_RC}"
    restore_checkout
    systemctl restart fmp-dashboard || true
    exit "$POST_VERIFY_RC"
  fi
  echo "Observing running Streamlit identity after restart..."
  "$PYTHON_BIN" -m jobs.observe_running_dashboard --out "$STATE_DIR/running_identity.json"
  echo "Auditing host Streamlit identity after restart (no secrets, no systemd change)..."
  AUDIT_RC=0
  (
    set -a
    # Default DASHBOARD_ENV=/etc/fmp/fmp-dashboard.env
    # shellcheck disable=SC1091
    . "$DASHBOARD_ENV"
    set +a
    unset DATABASE_URL DB_PASSWORD DB_USER DB_HOST DB_NAME DB_PORT MARKET_INTELLIGENCE_DATABASE_URL DASHBOARD_ALLOW_WRITER_FALLBACK
    "$PYTHON_BIN" -m jobs.audit_host_dashboard \
      --verify-rc "$POST_VERIFY_RC" \
      --out "$STATE_DIR/host_audit.json" \
      --require-readonly \
      --require-running-identity \
      --expected-sha "$SHA" \
      --env-file /etc/fmp/fmp-dashboard.env
  ) || AUDIT_RC=$?
  if [ "$AUDIT_RC" != "0" ]; then
    echo "host dashboard audit failed rc=${AUDIT_RC}"
    restore_checkout
    systemctl restart fmp-dashboard || true
    exit "$AUDIT_RC"
  fi
  printf '%s\n' "$SHA" > "$STATE_DIR/last_verified.sha"
  echo "last_verified=$SHA"
  if systemctl cat fmp-ibkr-ingest.service >/dev/null 2>&1; then
    echo "Restarting private IBKR ingest API..."
    systemctl restart fmp-ibkr-ingest.service
  fi
fi

echo "migrations_applied=once"
echo "deploy_host=complete sha=$SHA"
