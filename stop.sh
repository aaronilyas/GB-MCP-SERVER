#!/usr/bin/env bash
# Stop the Game Boy MCP compose stack and sibling play/validator containers.
#
# Play instances (`gb-play-<32-hex>`) are not Compose services. They are
# sibling containers started through the Docker socket. Compose down does
# not save or remove them, so this script asks each one to write its
# `.state` file before removing it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

FORCE=0
COMPOSE_BIN=()

usage() {
  cat <<'EOF'
Usage: stop.sh [--force]

Stop gb-mcp-server and cloudflared, after saving running play instances.

  --force   Skip the play-instance save RPC and docker rm -f immediately

ROMs, `.state` files, and user_subdirectories.sqlite3 are not deleted.
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

require_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_BIN=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_BIN=(docker-compose)
  else
    die "Docker Compose is required (Docker Compose plugin: docker compose)"
  fi
}

compose() {
  "${COMPOSE_BIN[@]}" -f "$ROOT/compose.yaml" "$@"
}

unique_ids() {
  awk 'NF && !seen[$0]++'
}

container_running() {
  local id="$1"
  local state
  state="$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null || echo false)"
  [[ "$state" == "true" ]]
}

wait_stopped() {
  local id="$1"
  local n=0
  while [[ "$n" -lt 40 ]]; do
    container_running "$id" || return 0
    sleep 0.5
    n=$((n + 1))
  done
  return 1
}

stop_play_instance() {
  local id="$1"
  local name
  name="$(docker inspect -f '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##' || true)"
  [[ -n "$name" ]] || name="$id"

  if [[ "$FORCE" -eq 1 ]]; then
    echo "Removing play instance $name"
  elif container_running "$id"; then
    echo "Saving play instance $name"
    # Same RPC the MCP host uses for stop_pyboy / SessionManager.shutdown.
    printf '%s' '{"reason":"shutdown"}' \
      | docker exec -i "$id" python /opt/instance/server.py rpc POST /stop \
      >/dev/null 2>&1 || true
    wait_stopped "$id" || true
  fi
  docker rm -f "$id" >/dev/null 2>&1 || true
}

list_play_ids() {
  {
    docker ps -aq --filter label=gb-mcp.role=play || true
    docker ps -aq --filter name=gb-play- || true
  } | unique_ids
}

list_validator_ids() {
  docker ps -aq --filter name=gb-rom-validate- || true
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --force)
      FORCE=1
      shift
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

[[ -f "$ROOT/compose.yaml" ]] || die "compose.yaml not found in $ROOT"

command -v docker >/dev/null 2>&1 || die "Docker is required and must be running to stop the MCP server."
docker info >/dev/null 2>&1 || die "Docker is required and must be running to stop the MCP server."
require_compose

# Give MCP atexit a chance to save sessions it still tracks.
compose stop -t 20 gb-mcp-server >/dev/null 2>&1 || true

play_ids="$(list_play_ids)"
if [[ -n "$play_ids" ]]; then
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    stop_play_instance "$id"
  done <<<"$play_ids"
fi

validator_ids="$(list_validator_ids)"
if [[ -n "$validator_ids" ]]; then
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    docker rm -f "$id" >/dev/null 2>&1 || true
  done <<<"$validator_ids"
fi

# Always pass the tunnel profile so cloudflared is removed when it was started.
echo "Stopping gb-mcp compose stack"
compose --profile tunnel down --remove-orphans

echo "gb-mcp-server is stopped. ROMs and user_subdirectories.sqlite3 were left in place."
