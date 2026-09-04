#!/usr/bin/env bash
# Start the Game Boy MCP compose stack in the background.
#
# Compose bind-mounts use ${PWD}, so this script always cds to the repo root.
# HTTP mode refuses to boot without GB_MCP_BEARER_TOKEN or GB_MCP_JWT_SECRET.
# A non-empty TUNNEL_TOKEN (or --tunnel) also starts the cloudflared profile.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

BUILD=1
TUNNEL="auto"
COMPOSE_BIN=()

usage() {
  cat <<'EOF'
Usage: start.sh [--tunnel | --no-tunnel] [--no-build]

Start gb-mcp-server (and the validator / play images) with Docker Compose.

  --tunnel      Also start cloudflared (requires TUNNEL_TOKEN)
  --no-tunnel   Do not start cloudflared even if TUNNEL_TOKEN is set
  --no-build    Skip image rebuilds

The SQLite map file is created if missing. ROMs and save states are left as-is.
Stop with ./stop.sh.
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

env_get() {
  local key="$1"
  local file="$ROOT/.env"
  local line value
  [[ -f "$file" ]] || return 0
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 0
  value="${line#*=}"
  value="${value%$'\r'}"
  if [[ ${#value} -ge 2 ]]; then
    if [[ "$value" == \"*\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
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

nonempty() {
  local value="$1"
  [[ -n "${value//[$' \t\r\n']/}" ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --tunnel)
      TUNNEL="yes"
      shift
      ;;
    --no-tunnel)
      TUNNEL="no"
      shift
      ;;
    --no-build)
      BUILD=0
      shift
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

[[ -f "$ROOT/compose.yaml" ]] || die "compose.yaml not found in $ROOT"

command -v docker >/dev/null 2>&1 || die "Docker is required and must be running to start the MCP server."
docker info >/dev/null 2>&1 || die "Docker is required and must be running to start the MCP server."
require_compose

sqlite_path="$ROOT/user_subdirectories.sqlite3"
if [[ -d "$sqlite_path" ]]; then
  die "user_subdirectories.sqlite3 is a directory. Docker created it because the file was missing; remove that directory and re-run."
fi

bearer="${GB_MCP_BEARER_TOKEN:-$(env_get GB_MCP_BEARER_TOKEN)}"
jwt="${GB_MCP_JWT_SECRET:-$(env_get GB_MCP_JWT_SECRET)}"
if ! nonempty "$bearer" && ! nonempty "$jwt"; then
  if [[ ! -f "$ROOT/.env" ]]; then
    die "HTTP mode requires GB_MCP_BEARER_TOKEN or GB_MCP_JWT_SECRET. Copy .env.example to .env and set at least one."
  fi
  die "HTTP mode requires GB_MCP_BEARER_TOKEN or GB_MCP_JWT_SECRET so the open internet cannot call tools."
fi

tunnel_token="${TUNNEL_TOKEN:-$(env_get TUNNEL_TOKEN)}"
profile_args=()
if [[ "$TUNNEL" == "yes" ]]; then
  nonempty "$tunnel_token" || die "--tunnel requires a non-empty TUNNEL_TOKEN in .env or the environment"
  profile_args=(--profile tunnel)
elif [[ "$TUNNEL" == "auto" ]] && nonempty "$tunnel_token"; then
  profile_args=(--profile tunnel)
fi

mkdir -p "$ROOT/roms"
touch "$sqlite_path"

up_args=(up -d)
if [[ "$BUILD" -eq 1 ]]; then
  up_args+=(--build)
fi

echo "Starting gb-mcp compose stack in $ROOT"
# Empty-array expansion is unbound under bash 3.2 `set -u` (macOS /bin/bash).
if [[ ${#profile_args[@]} -gt 0 ]]; then
  compose "${profile_args[@]}" "${up_args[@]}"
else
  compose "${up_args[@]}"
fi

server_id="$(compose ps -q gb-mcp-server || true)"
[[ -n "$server_id" ]] || die "gb-mcp-server container was not created. Check: docker compose logs"
if [[ "$(docker inspect -f '{{.State.Running}}' "$server_id")" != "true" ]]; then
  compose logs --tail 80 gb-mcp-server >&2 || true
  die "gb-mcp-server is not running. Check: docker compose logs gb-mcp-server"
fi

if [[ ${#profile_args[@]} -gt 0 ]]; then
  echo "cloudflared profile enabled (named tunnel)."
fi
echo "gb-mcp-server is up on the compose network at port 8080 (MCP path /mcp)."
echo "Uncomment ports in compose.yaml to reach http://127.0.0.1:8080/mcp from the host."
echo "Logs: docker compose logs -f gb-mcp-server"
echo "Stop: ./stop.sh"
