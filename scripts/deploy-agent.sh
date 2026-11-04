#!/usr/bin/env bash
# Roll this repo's agent onto a box (dev or prod): build the image on the box from a clean checkout of a pushed sha,
# point the compose `agent` service at it, recreate that one service, wait for it to answer.
#
#   scripts/deploy-agent.sh <ssh-host> <compose-dir> <sha-or-tag> [<health-url>]
#   scripts/deploy-agent.sh "$HOST" "$DIR" "$(git rev-parse HEAD)"                    # dev: the default health URL
#   scripts/deploy-agent.sh "$HOST" "$DIR" "$(git rev-parse HEAD)" https://agent.admiry.ai/health   # prod
#
# <sha-or-tag>:
#   - an image <the agent service's repository>:<sha-or-tag> already on the box (or, for 7-40 hex characters, the one
#     local tag that starts with it): the rollback path -- no checkout, no build; the gate is <health-url> 200;
#   - otherwise a git sha (7-40 hex characters) that is on origin (the box clones from GitHub: push first). The image
#     is tagged with the full sha; when this run builds it, the gate is <health-url> 200 and the same host's /health
#     naming the sha.
# <health-url> must answer 200 (default https://agent.dev.adx.admiry.ai/connect/docs).
#
# Rollback = the same command with the previous tag; any failure after .env was changed prints that exact command.
#
# What it touches on the box, and nothing else:
#   - /srv/admiry-agent: a clone of this repo, checked out (detached, cleaned) at the sha, only when it builds;
#   - the local image <repository>:<full sha> (never pushed; compose never pulls);
#   - <compose-dir>/.env: backed up to /root/.env.bak-hack-<UTC>, then AGENT_IMAGE_TAG=<tag>, written in place;
#   - the `agent` service only: `up -d --no-deps --pull never agent`, with the compose files and project the agent
#     container was started with (its labels), so no other service's configuration can change.
#
# Settings (environment, optional):
#   REPO_URL      the repo the box clones (default: this repo's public GitHub URL)
#   HEALTH_WAIT   seconds to wait for the gate (default 90)
set -euo pipefail

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
  echo "usage: $0 <ssh-host> <compose-dir> <sha-or-tag> [<health-url>]" >&2
  exit 2
fi
HOST=$1
DIR=$2
REF=$3
HEALTH_URL=${4:-https://agent.dev.adx.admiry.ai/connect/docs}
if ! [[ "$REF" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "<sha-or-tag> must be a git sha or an image tag, got: $REF" >&2
  exit 2
fi
if ! [[ "$HEALTH_URL" =~ ^https?://[^/]+(/.*)?$ ]]; then
  echo "<health-url> must be a full http(s) URL, got: $HEALTH_URL" >&2
  exit 2
fi
REPO_URL=${REPO_URL:-https://github.com/chuang39/admiry-agent.git}
WAIT=${HEALTH_WAIT:-90}

# On the box, at exit: drop the temp .env copy; after a failure past the .env edit, say how to roll back.
remote_exit() {
  local rc=$?
  if [ -n "${R_TMP:-}" ]; then rm -f "$R_TMP"; fi
  if [ "$rc" -ne 0 ] && [ "${R_EDITED:-0}" = 1 ]; then
    echo "!! failed after .env was changed (AGENT_IMAGE_TAG ${R_OLD} -> ${R_TAG}); backup: ${R_BAK}" >&2
    echo "!! roll back: scripts/deploy-agent.sh ${R_ROLLBACK}" >&2
  fi
  return "$rc"
}

# Runs on the box. Sent as the ssh command line (declare -f), with stdin from /dev/null: nothing on the box can
# swallow the rest of the script the way a heredoc on stdin can (`docker compose exec` forwards stdin).
remote_deploy() {
  set -euo pipefail
  local host=$1 dir=$2 ref=$3 health_url=$4 repo_url=$5 wait=$6
  local src=/srv/admiry-agent
  # Read by remote_exit (globals: the EXIT trap runs after this function has returned).
  R_TMP='' R_BAK='' R_EDITED=0 R_OLD='' R_TAG='' R_ROLLBACK=''
  trap remote_exit EXIT
  # A variable from the shell beats .env and would win silently.
  unset AGENT_IMAGE_TAG COMPOSE_PROFILES COMPOSE_FILE COMPOSE_PROJECT_NAME
  cd "$dir"
  [ -f .env ] || { echo "no .env in $dir" >&2; return 1; }

  # The compose files and project of the agent container from this directory: the running one, else the last one.
  local cid filt="label=com.docker.compose.project.working_dir=$dir"
  cid=$(docker ps -q --filter label=com.docker.compose.service=agent --filter "$filt" | head -n1)
  [ -n "$cid" ] || cid=$(docker ps -aq --filter label=com.docker.compose.service=agent --filter "$filt" | head -n1)
  [ -n "$cid" ] || { echo "no agent container from $dir: cannot tell its compose files" >&2; return 1; }
  local files project
  files=$(docker inspect "$cid" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')
  project=$(docker inspect "$cid" --format '{{index .Config.Labels "com.docker.compose.project"}}')
  [ -n "$files" ] && [ -n "$project" ] || { echo "agent container $cid has no compose labels" >&2; return 1; }
  local -a C=(docker compose -p "$project") flist
  local f
  IFS=',' read -r -a flist <<<"$files"
  for f in "${flist[@]}"; do C+=(-f "$f"); done
  echo "== compose: ${C[*]}"

  local image repo old_tag
  image=$("${C[@]}" config --format json agent | jq -r '.services.agent.image')
  repo=${image%:*}
  old_tag=${image##*:}
  if [ -z "$image" ] || [ "$image" = null ] || [[ "$old_tag" == */* ]]; then
    echo "cannot read the agent image from compose config: '$image'" >&2
    return 1
  fi
  echo "== agent image now: $image"

  # Which image: one already on the box (rollback: no build), else a sha on origin, built here.
  local tag='' built=0 hex=0 matches
  if [[ "$ref" =~ ^[0-9a-f]{7,40}$ ]]; then hex=1; fi
  if docker image inspect "$repo:$ref" >/dev/null 2>&1; then
    tag=$ref
  elif [ "$hex" = 1 ]; then
    matches=$(docker images --format '{{.Tag}}' "$repo" | grep "^$ref" | sort -u || true)
    if [ -n "$matches" ] && [ "$(printf '%s\n' "$matches" | wc -l)" -eq 1 ]; then
      tag=$matches
    fi
  fi
  if [ -n "$tag" ]; then
    echo "== $repo:$tag is already on the box: no checkout, no build"
  else
    [ "$hex" = 1 ] || { echo "no image $repo:$ref on the box, and '$ref' is not a git sha" >&2; return 1; }
    if [ -d "$src/.git" ]; then
      git -C "$src" remote set-url origin "$repo_url"
      git -C "$src" fetch --quiet --prune origin </dev/null
    else
      git clone --quiet "$repo_url" "$src" </dev/null
    fi
    local full
    full=$(git -C "$src" rev-parse --verify --quiet "$ref^{commit}" || true)
    if [ -z "$full" ] || [ -z "$(git -C "$src" branch -r --contains "$full" 2>/dev/null)" ]; then
      echo "sha $ref is not on origin ($repo_url): push it first (git push origin main)" >&2
      return 1
    fi
    tag=$full
    if docker image inspect "$repo:$tag" >/dev/null 2>&1; then
      echo "== $repo:$tag is already on the box: no checkout, no build"
    else
      git -C "$src" -c advice.detachedHead=false checkout --quiet --force --detach "$full"
      git -C "$src" clean -fdxq
      [ "$(git -C "$src" rev-parse HEAD)" = "$full" ] || { echo "checkout is not at $full" >&2; return 1; }
      echo "== building $repo:$tag from $src"
      docker build --progress=plain -f "$src/agent/Dockerfile" --build-arg "BIDX_BUILD_SHA=$full" \
        -t "$repo:$tag" "$src" </dev/null
      built=1
    fi
  fi

  # .env: back up, then rewrite in place (same inode: cat into it, never mv or sed -i).
  local ts ino_before ino_after
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  R_BAK=/root/.env.bak-hack-$ts
  R_OLD=$old_tag
  R_TAG=$tag
  R_ROLLBACK=$(printf '%q ' "$host" "$dir" "$old_tag" "$health_url")
  cp -p .env "$R_BAK"
  echo "== .env backed up to $R_BAK"
  ino_before=$(stat -c %i .env)
  R_TMP=$(umask 077 && mktemp /root/.env.new-hack-XXXXXX)
  if grep -q '^AGENT_IMAGE_TAG=' .env; then
    awk -v t="$tag" '/^AGENT_IMAGE_TAG=/ { print "AGENT_IMAGE_TAG=" t; next } { print }' .env >"$R_TMP"
  else
    {
      cat .env
      if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then echo; fi # a last line without its newline
      echo "AGENT_IMAGE_TAG=$tag"
    } >"$R_TMP"
  fi
  R_EDITED=1
  cat "$R_TMP" >.env
  rm -f "$R_TMP"
  R_TMP=''
  ino_after=$(stat -c %i .env)
  [ "$ino_before" = "$ino_after" ] || { echo ".env inode changed ($ino_before -> $ino_after)" >&2; return 1; }
  echo "== .env: AGENT_IMAGE_TAG $old_tag -> $tag"

  local resolved
  resolved=$("${C[@]}" config --format json agent | jq -r '.services.agent.image')
  [ "$resolved" = "$repo:$tag" ] || { echo "compose resolves the agent to $resolved, not $repo:$tag" >&2; return 1; }

  "${C[@]}" up -d --no-deps --pull never agent </dev/null

  # The gate: <health-url> 200; when this run built the image, also the same host's /health naming the sha.
  local base want
  base=$(printf '%s' "$health_url" | sed -E 's#^(https?://[^/]+).*#\1#')
  want="$health_url 200"
  if [ "$built" = 1 ]; then want="$want and $base/health naming $tag"; fi
  echo "== waiting up to ${wait}s for $want"
  local start deadline left code health
  start=$(date +%s)
  deadline=$((start + wait))
  while :; do
    left=$((deadline - $(date +%s)))
    [ "$left" -ge 1 ] || left=1
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$((left < 5 ? left : 5))" "$health_url" || true)
    health=''
    if [ "$code" = 200 ] && [ "$built" = 1 ]; then
      left=$((deadline - $(date +%s)))
      [ "$left" -ge 1 ] || left=1
      health=$(curl -s --max-time "$((left < 5 ? left : 5))" "$base/health" || true)
    fi
    if [ "$code" = 200 ] && { [ "$built" = 0 ] || [[ "$health" == *"$tag"* ]]; }; then
      echo "== healthy after $(($(date +%s) - start))s: $health_url $code${health:+, /health $health}"
      "${C[@]}" ps agent
      return 0
    fi
    left=$((deadline - $(date +%s)))
    if [ "$left" -le 0 ]; then
      echo "!! not healthy after ${wait}s: $health_url $code${health:+, /health '$health'}" >&2
      "${C[@]}" ps agent >&2 || true
      "${C[@]}" logs --no-color --tail 120 agent >&2 || true
      return 1
    fi
    sleep "$((left < 3 ? left : 3))"
  done
}

ssh -o BatchMode=yes "$HOST" \
  "$(declare -f remote_exit remote_deploy); remote_deploy $(printf '%q ' "$HOST" "$DIR" "$REF" "$HEALTH_URL" "$REPO_URL" "$WAIT")" \
  </dev/null
