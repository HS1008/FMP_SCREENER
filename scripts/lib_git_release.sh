#!/usr/bin/env bash
# Production checkout activation helpers. Sourced by the piped deploy_host.sh
# from the staged release tree so the live checkout need not already contain
# this file. Never prints secrets.
#
# Fetch the requested SHA from an authoritative remote with ancestry.
# Do not copy an unmarked shallow commit from a depth-1 staged clone.
# A repo that already has the commit object but is missing parents will not
# be repaired by a normal fetch (have/want skips it); --refetch or an object
# copy from a full clone is required.

_git() {
  git -c protocol.file.allow=always "$@"
}

_commit_walkable() {
  local dir="$1"
  local sha="$2"
  _git -C "$dir" cat-file -e "${sha}^{commit}" 2>/dev/null || return 1
  _git -C "$dir" log -1 --format=%H "$sha" >/dev/null 2>&1 || return 1
  _git -C "$dir" rev-list --max-count=2 "$sha" >/dev/null 2>&1 || return 1
  local parent
  while read -r parent; do
    [ -z "$parent" ] && continue
    _git -C "$dir" cat-file -e "${parent}^{commit}" 2>/dev/null || return 1
  done < <(_git -C "$dir" cat-file -p "$sha" | awk '/^$/{exit} /^parent /{print $2}')
}

_is_shallow() {
  local dir="$1"
  [ "$(_git -C "$dir" rev-parse --is-shallow-repository 2>/dev/null || echo false)" = "true" ]
}

_fetch_sha_from() {
  local dir="$1"
  local src="$2"
  local sha="$3"
  GIT_TERMINAL_PROMPT=0 _git -C "$dir" fetch --update-shallow "$src" "$sha" \
    || GIT_TERMINAL_PROMPT=0 _git -C "$dir" fetch "$src" "$sha"
}

_refetch_sha_from() {
  local dir="$1"
  local src="$2"
  local sha="$3"
  GIT_TERMINAL_PROMPT=0 _git -C "$dir" fetch --refetch --update-shallow "$src" "$sha" \
    || GIT_TERMINAL_PROMPT=0 _git -C "$dir" fetch --refetch "$src" "$sha"
}

_copy_objects_from_full_clone() {
  local dir="$1"
  local url="$2"
  local sha="$3"
  local sidecar objects
  sidecar="$(mktemp -d "${TMPDIR:-/tmp}/fmp-git-ancestry.XXXXXX")" || return 1
  if ! GIT_TERMINAL_PROMPT=0 _git clone --bare "$url" "$sidecar"; then
    rm -rf "$sidecar"
    return 1
  fi
  objects="$sidecar/objects"
  if [ ! -d "$objects" ]; then
    objects="$sidecar/.git/objects"
  fi
  if [ ! -d "$objects" ]; then
    rm -rf "$sidecar"
    return 1
  fi
  mkdir -p "$dir/.git/objects"
  if ! cp -a "$objects/." "$dir/.git/objects/"; then
    rm -rf "$sidecar"
    return 1
  fi
  rm -rf "$sidecar"
  _commit_walkable "$dir" "$sha"
}

assert_commit_graph() {
  local dir="$1"
  local expected="$2"
  local actual logged line parent
  if [ ! -d "$dir/.git" ] && [ ! -f "$dir/.git" ]; then
    echo "FAIL: $dir is not a git checkout"
    return 3
  fi
  actual="$(_git -C "$dir" rev-parse HEAD)"
  if [ "$actual" != "$expected" ]; then
    echo "FAIL: checkout HEAD ${actual} does not match requested ${expected}"
    return 3
  fi
  if ! logged="$(_git -C "$dir" log -1 --format=%H)"; then
    echo "FAIL: git log -1 failed; commit graph is incomplete"
    return 3
  fi
  if [ "$logged" != "$expected" ]; then
    echo "FAIL: git log -1 reported ${logged}, expected ${expected}"
    return 3
  fi
  line="$(_git -C "$dir" rev-list --parents -n 1 HEAD)"
  # shellcheck disable=SC2086
  set -- $line
  shift
  for parent in "$@"; do
    if ! _git -C "$dir" cat-file -e "${parent}^{commit}"; then
      echo "FAIL: missing required parent ${parent} of ${expected}"
      return 3
    fi
  done
  if ! _git -C "$dir" rev-list --max-count=2 HEAD >/dev/null; then
    echo "FAIL: git rev-list cannot walk ancestry of ${expected}"
    return 3
  fi
  echo "git_graph=connected sha=$expected"
  return 0
}

fetch_commit_with_ancestry() {
  local dir="$1"
  local sha="$2"
  local url="$3"
  local staged="${4:-}"

  if _commit_walkable "$dir" "$sha"; then
    return 0
  fi

  _fetch_sha_from "$dir" "$url" "$sha" || true
  if _commit_walkable "$dir" "$sha"; then
    return 0
  fi

  echo "commit present without ancestry; refetching $sha from origin"
  _refetch_sha_from "$dir" "$url" "$sha" || true
  if _commit_walkable "$dir" "$sha"; then
    return 0
  fi

  if _is_shallow "$dir"; then
    GIT_TERMINAL_PROMPT=0 _git -C "$dir" fetch --update-shallow --deepen=100 "$url" || true
    GIT_TERMINAL_PROMPT=0 _git -C "$dir" fetch --unshallow "$url" || true
  fi
  if _commit_walkable "$dir" "$sha"; then
    return 0
  fi

  echo "refetch incomplete; copying objects from a full origin clone"
  _copy_objects_from_full_clone "$dir" "$url" "$sha" || true
  if _commit_walkable "$dir" "$sha"; then
    return 0
  fi

  if [ -n "$staged" ] && [ -d "$staged/.git" ]; then
    echo "origin fetch incomplete; deepening staged clone before any staged fetch"
    GIT_TERMINAL_PROMPT=0 _git -C "$staged" fetch --unshallow "$url" || true
    GIT_TERMINAL_PROMPT=0 _git -C "$staged" fetch --deepen=100 "$url" || true
    if _is_shallow "$staged"; then
      echo "FAIL: staged clone is still shallow; refusing to copy an incomplete commit"
      return 3
    fi
    _refetch_sha_from "$dir" "$staged" "$sha" || _fetch_sha_from "$dir" "$staged" "$sha" || true
  fi

  if _commit_walkable "$dir" "$sha"; then
    return 0
  fi
  echo "FAIL: could not fetch $sha from origin with ancestry"
  return 3
}

activate_live_checkout() {
  local dir="$1"
  local sha="$2"
  local url="$3"
  local staged="${4:-}"
  fetch_commit_with_ancestry "$dir" "$sha" "$url" "$staged" || return
  _git -C "$dir" checkout --detach "$sha" || return
  assert_commit_graph "$dir" "$sha"
}
