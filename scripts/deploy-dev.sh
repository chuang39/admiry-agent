#!/usr/bin/env bash
# Roll this repo's agent onto the dev box: build the image on the box from a clean checkout of <git-sha>, point the
# compose `agent` service at it, recreate that one service, wait for it to answer.
#
#   scripts/deploy-dev.sh <ssh-host> <compose-dir> <git-sha>
#   scripts/deploy-dev.sh "$HOST" "$DIR" "$(git rev-parse HEAD)"
#
# Rollback = the same command with the previous sha (the script prints it). A sha whose image is already on the box
# (e.g. one built by the old CD pipeline) is not built again: the box needs no checkout of it.
#
# What it touches on the box, and nothing else:
#   - /srv/admiry-agent: a clone of this repo, checked out (detached, cleaned) at <git-sha>, only when it builds;
#   - the local image <the agent service's repository>:<git-sha> (never pushed; compose never pulls);
#   - <compose-dir>/.env: backed up to /root/.env.bak-hack-<UTC>, then AGENT_IMAGE_TAG=<git-sha>, written in place;
#   - the `agent` service only: `up -d --no-deps --pull never agent`, with the compose files and project the running
#     agent container was started with (its labels), so no other service's configuration can change.
#
# Settings (environment, optional):
#   AGENT_REPO_URL     the repo to clone (default: this repo's public GitHub URL)
#   AGENT_PUBLIC_BASE  the agent's public base (default https://agent.dev.adx.admiry.ai)
#   HEALTH_WAIT        seconds to wait for /connect/docs 200 and /health naming the sha (default 90)
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <ssh-host> <compose-dir> <git-sha>" >&2
  exit 2
fi
HOST=$1
DIR=$2
SHA=$3
if ! [[ "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "git-sha must be the full 40-character sha, got: $SHA" >&2
  exit 2
fi
REPO_URL=${AGENT_REPO_URL:-https://github.com/chuang39/admiry-agent.git}
BASE=${AGENT_PUBLIC_BASE:-https://agent.dev.adx.admiry.ai}
WAIT=${HEALTH_WAIT:-90}

# Runs on the box. Sent as the ssh command line (declare -f), with stdin from /dev/null: nothing on the box can
# swallow the rest of the script the way a heredoc on stdin can (`docker compose exec` forwards stdin).
remote_deploy() {
  set -euo pipefail
  local dir=$1 sha=$2 repo_url=$3 base=$4 wait=$5
  local src=/srv/admiry-agent
  # A variable from the shell beats .env and would win silently.
  unset AGENT_IMAGE_TAG COMPOSE_PROFILES COMPOSE_FILE COMPOSE_PROJECT_NAME
  cd "$dir"
  [ -f .env ] || { echo "no .env in $dir" >&2; return 1; }

  # The compose files and project of the agent container that runs (or last ran) here.
  local cid
  cid=$(docker ps -aq --filter label=com.docker.compose.service=agent \
    --filter "label=com.docker.compose.project.working_dir=$dir" | head -n1)
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

  if docker image inspect "$repo:$sha" >/dev/null 2>&1; then
    echo "== $repo:$sha is already on the box: not building"
  else
    if [ -d "$src/.git" ]; then
      git -C "$src" fetch --quiet origin
    else
      git clone --quiet "$repo_url" "$src"
    fi
    git -C "$src" -c advice.detachedHead=false checkout --quiet --force --detach "$sha"
    git -C "$src" clean -fdxq
    [ "$(git -C "$src" rev-parse HEAD)" = "$sha" ] || { echo "checkout is not at $sha" >&2; return 1; }
    echo "== building $repo:$sha from $src"
    docker build --progress=plain -f "$src/agent/Dockerfile" --build-arg "BIDX_BUILD_SHA=$sha" \
      -t "$repo:$sha" "$src" </dev/null
  fi

  # .env: back up, then rewrite in place (same inode: cat into it, never mv or sed -i).
  local ts bak tmp ino_before ino_after
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  bak=/root/.env.bak-hack-$ts
  cp -p .env "$bak"
  echo "== .env backed up to $bak"
  ino_before=$(stat -c %i .env)
  tmp=$(umask 077 && mktemp /root/.env.new-hack-XXXXXX)
  if grep -q '^AGENT_IMAGE_TAG=' .env; then
    awk -v t="$sha" '/^AGENT_IMAGE_TAG=/ { print "AGENT_IMAGE_TAG=" t; next } { print }' .env >"$tmp"
  else
    { cat .env; echo "AGENT_IMAGE_TAG=$sha"; } >"$tmp"
  fi
  cat "$tmp" >.env
  rm -f "$tmp"
  ino_after=$(stat -c %i .env)
  [ "$ino_before" = "$ino_after" ] || { echo ".env inode changed ($ino_before -> $ino_after)" >&2; return 1; }
  echo "== .env: AGENT_IMAGE_TAG $old_tag -> $sha"

  local resolved
  resolved=$("${C[@]}" config --format json agent | jq -r '.services.agent.image')
  [ "$resolved" = "$repo:$sha" ] || { echo "compose resolves the agent to $resolved, not $repo:$sha" >&2; return 1; }

  "${C[@]}" up -d --no-deps --pull never agent </dev/null

  echo "== waiting up to ${wait}s for $base/connect/docs 200 and /health at $sha"
  local start code health
  start=$(date +%s)
  while :; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$base/connect/docs" || true)
    health=$(curl -s --max-time 5 "$base/health" || true)
    if [ "$code" = 200 ] && [[ "$health" == *"$sha"* ]]; then
      echo "== healthy after $(($(date +%s) - start))s: /connect/docs $code, /health $health"
      "${C[@]}" ps agent
      return 0
    fi
    if [ $(($(date +%s) - start)) -ge "$wait" ]; then
      echo "!! not healthy after ${wait}s: /connect/docs $code, /health '$health'" >&2
      "${C[@]}" ps agent >&2 || true
      "${C[@]}" logs --no-color --tail 120 agent >&2 || true
      echo "!! roll back: scripts/deploy-dev.sh <ssh-host> <compose-dir> $old_tag   (backup: $bak)" >&2
      return 1
    fi
    sleep 3
  done
}

ssh -o BatchMode=yes "$HOST" "$(declare -f remote_deploy); remote_deploy $(printf '%q ' "$DIR" "$SHA" "$REPO_URL" "$BASE" "$WAIT")" </dev/null
