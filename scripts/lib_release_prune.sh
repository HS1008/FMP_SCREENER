#!/usr/bin/env bash
# Prune unused /opt/fmp/releases/<sha> trees so everyday auto-deploy cannot
# fill the droplet with one full venv per merge.
#
# Sourced by the piped deploy_host.sh (embedded copy) and by tests.
# Never prints secrets. Never deletes current, previous, last_verified,
# or the SHA being staged.
#
# Keep set:
#   - FMP_CURRENT_LINK / FMP_PREVIOUS_LINK targets under RELEASE_ROOT
#   - last_verified.sha (if it is a 40-hex SHA)
#   - requested SHA (FMP_RELEASE_PRUNE_KEEP_SHA or SHA)
#   - FMP_RELEASE_KEEP_EXTRA (default 1) newest complete unused trees
# Incomplete unused trees (no venv/bin/streamlit) are always removed.

_is_release_sha() {
  printf '%s' "$1" | grep -Eq '^[0-9a-f]{40}$'
}

_canonical_under_release_root() {
  local p="$1"
  local root canon
  root="$(readlink -f "$RELEASE_ROOT" 2>/dev/null || true)"
  canon="$(readlink -f "$p" 2>/dev/null || true)"
  [ -n "$root" ] && [ -n "$canon" ] || return 1
  case "$canon" in
    "$root"/*) ;;
    *) return 1 ;;
  esac
  printf '%s\n' "$canon"
}

_keep_sha_from_link() {
  local link="$1"
  local canon base
  [ -L "$link" ] || [ -e "$link" ] || return 0
  canon="$(_canonical_under_release_root "$link")" || return 0
  base="$(basename "$canon")"
  if _is_release_sha "$base"; then
    printf '%s\n' "$base"
  fi
}

_release_tree_complete() {
  local dir="$1"
  [ -x "$dir/venv/bin/streamlit" ]
}

_safe_release_tree() {
  local dir="$1"
  local base canon
  base="$(basename "$dir")"
  _is_release_sha "$base" || return 1
  [ ! -L "$dir" ] || return 1
  [ -d "$dir" ] || return 1
  canon="$(_canonical_under_release_root "$dir")" || return 1
  [ "$(basename "$canon")" = "$base" ] || return 1
  printf '%s\n' "$canon"
}

report_release_disk() {
  local label="${1:-release_disk}"
  if [ -d "$RELEASE_ROOT" ]; then
    echo "${label}=$(df -P "$RELEASE_ROOT" | awk 'NR==2 {print $4}')"
  else
    echo "${label}=absent"
  fi
}

remove_incomplete_release_venv() {
  local staged="$1"
  local venv
  [ -n "$staged" ] || return 0
  venv="$staged/venv"
  if [ -x "$venv/bin/python" ] && [ ! -x "$venv/bin/streamlit" ]; then
    echo "incomplete_release_venv=remove $venv"
    rm -rf -- "$venv"
  fi
}

prune_unused_releases() {
  local keep_sha extra root canon base dir
  local -A keep=()
  local -a unused=()
  local -a extra_keep=()
  local deleted=0

  if [ "${FMP_RELEASE_PRUNE:-1}" != "1" ]; then
    echo "release_prune=skipped"
    return 0
  fi
  if [ ! -d "$RELEASE_ROOT" ]; then
    echo "release_prune=noop root_absent"
    return 0
  fi
  root="$(readlink -f "$RELEASE_ROOT" 2>/dev/null || true)"
  if [ -z "$root" ]; then
    echo "release_prune=noop root_unresolved"
    return 0
  fi

  keep_sha="${FMP_RELEASE_PRUNE_KEEP_SHA:-${SHA:-}}"
  if _is_release_sha "$keep_sha"; then
    keep["$keep_sha"]=requested
  fi
  base="$(_keep_sha_from_link "${CURRENT_LINK:-}")"
  if [ -n "$base" ]; then
    keep["$base"]=current
  fi
  base="$(_keep_sha_from_link "${PREVIOUS_LINK:-}")"
  if [ -n "$base" ]; then
    keep["$base"]=previous
  fi
  if [ -s "${STATE_DIR:-}/last_verified.sha" ]; then
    base="$(tr -d '[:space:]' < "${STATE_DIR}/last_verified.sha")"
    if _is_release_sha "$base"; then
      keep["$base"]=last_verified
    fi
  fi

  extra="${FMP_RELEASE_KEEP_EXTRA:-1}"
  if ! printf '%s' "$extra" | grep -Eq '^[0-9]+$'; then
    extra=1
  fi

  for dir in "$root"/*; do
    [ -e "$dir" ] || [ -L "$dir" ] || continue
    canon="$(_safe_release_tree "$dir")" || continue
    base="$(basename "$canon")"
    if [ -n "${keep[$base]:-}" ]; then
      echo "release_prune=keep sha=$base reason=${keep[$base]}"
      continue
    fi
    unused+=("$canon")
  done

  if [ "$extra" -gt 0 ] && [ "${#unused[@]}" -gt 0 ]; then
    local -a complete=()
    local -a incomplete=()
    for dir in "${unused[@]}"; do
      if _release_tree_complete "$dir"; then
        complete+=("$dir")
      else
        incomplete+=("$dir")
      fi
    done
    if [ "${#complete[@]}" -gt 0 ]; then
      local -a ranked=()
      while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        ranked+=("$dir")
      done < <(ls -1dt -- "${complete[@]}" 2>/dev/null || true)
      local i=0
      unused=("${incomplete[@]}")
      for dir in "${ranked[@]}"; do
        if [ "$i" -lt "$extra" ]; then
          extra_keep+=("$dir")
          echo "release_prune=keep sha=$(basename "$dir") reason=extra_recent"
          i=$((i + 1))
        else
          unused+=("$dir")
        fi
      done
    else
      unused=("${incomplete[@]}")
    fi
  fi

  if [ "${#unused[@]}" -gt 0 ]; then
    for dir in "${unused[@]}"; do
      canon="$(_safe_release_tree "$dir")" || continue
      echo "release_prune=delete sha=$(basename "$canon")"
      if rm -rf -- "$canon"; then
        deleted=$((deleted + 1))
      else
        echo "release_prune=delete_failed sha=$(basename "$canon")"
      fi
    done
  fi
  echo "release_prune=done deleted=$deleted kept=${#keep[@]} extra_kept=${#extra_keep[@]}"
}
