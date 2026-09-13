# Source this file from a post-deploy host session (do not execute).
# Keeps fd 9 open in the caller so mutating Stage 1 / Market Intelligence
# jobs cannot overlap on the shared host/database.
# GitHub concurrency is per-workflow; this flock is the host serializer.
if [ -z "${FMP_POST_DEPLOY_LOCK_HELD:-}" ]; then
  POST_DEPLOY_LOCK="${FMP_POST_DEPLOY_LOCK:-/var/lock/fmp-post-deploy.lock}"
  if ! (umask 077; : > "$POST_DEPLOY_LOCK") 2>/dev/null; then
    POST_DEPLOY_LOCK="/tmp/fmp-post-deploy.lock"
  fi
  exec 9>"$POST_DEPLOY_LOCK"
  if ! flock -w "${FMP_POST_DEPLOY_LOCK_WAIT:-3600}" 9; then
    echo "FAIL: could not acquire $POST_DEPLOY_LOCK"
    exit 75
  fi
  FMP_POST_DEPLOY_LOCK_HELD=1
  echo "post_deploy_lock=acquired"
fi
