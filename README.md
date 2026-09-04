# GB MCP Server

MCP server that accepts Game Boy / Game Boy Color ROMs from an LLM, validates
them inside an isolated Docker container, stores accepted files under
`roms/<32-hex>/`, and maps those directories to the LLM user's **email** in
SQLite (`user_subdirectories.sqlite3`). Mapped ROMs are played in a dedicated
`gb-pyboy-instance` container per subdirectory.

Email ↔ subdirectory mapping is application identity. It is **not** transport
authentication. Remote HTTP clients authenticate with a bearer token; tools
still take `email` arguments after that check succeeds. Do not ask the model
to type the bearer token (or any password) into a tool.

## Runtime

Three images, one long-lived process:

| Image | Role |
| --- | --- |
| `gb-mcp-server` | MCP tools + resources + SQLite. No user ROMs baked in. |
| `gb-rom-validator` | Throwaway `--network=none` container per `submit_gb_rom` |
| `gb-pyboy-instance` | One headless PyBoy (`window=null`) per `roms/<32-hex>/` |

The MCP process talks to the **host Docker daemon** (sibling containers). It
does not run Docker-in-Docker. After MCP itself is containerized, mount
`/var/run/docker.sock`. Validator and instance containers never get the socket
and are not published.

`submit_gb_rom` still streams the ROM on stdin into a locked-down validator
(`network=none`, read-only, `cap-drop=ALL`, then `rm -f`). Logo + header
checksum are required; file extension is not enough.

Play is **not** in-process. `load_subdirectory_rom` starts or reuses
`gb-play-<subdir hex>`, mounting only that subdirectory (ROM read-only, `.state`
read-write). `send_pyboy_input` talks to that container through
`gb_mcp/emulator` and still returns MCP PNG images. `stop_pyboy` and idle
timeout write the save to the volume, then remove the container. The next load
starts a new container and restores `roms/<subdir>/<rom>.state`.

One live session per email. Switching games saves and stops the old instance,
then starts the new one. A dead instance returns a short tool error, not a
Docker dump.

## Tools

| Tool | Purpose |
| --- | --- |
| `submit_gb_rom` | Base64 ROM in; isolated Docker validation; persist on success |
| `map_subdirectory_to_email` | Bind a 32-hex directory to the user's email |
| `list_subdirectories_for_email` | List that user's games and header metadata |
| `load_subdirectory_rom` | Start / resume a play instance for an owned directory |
| `send_pyboy_input` | Button chords; returns PNG screenshot(s) |
| `stop_pyboy` | Save, then remove the instance container |

Idle sessions auto-save to the volume and remove the container after five
minutes without button input.

## Resources (read-only)

| URI | Content |
| --- | --- |
| `gb://users/{email}/roms` | Owned ROM list and game metadata |
| `gb://users/{email}/roms/{subdirectory}` | Cartridge header metadata for an owned ROM |
| `gb://users/{email}/session` | Live play-instance status for that email |

## Compose

```bash
touch user_subdirectories.sqlite3
docker compose up --build
```

That starts long-lived `gb-mcp-server` and builds `gb-rom-validator` plus
`gb-pyboy-instance` (those two exit immediately; they exist so `docker run`
can use the images). MCP is on the compose network at port 8080. Uncomment
`ports` in `compose.yaml` to debug against `http://127.0.0.1:8080/mcp`.

Volumes (survive MCP restart and instance `rm`):

| Host path | Container path | Contents |
| --- | --- | --- |
| `./roms` | `/app/roms` | ROMs + `*.gb.state` / `*.gbc.state` |
| `./user_subdirectories.sqlite3` | `/app/user_subdirectories.sqlite3` | email ↔ subdirectory map |
| `/var/run/docker.sock` | `/var/run/docker.sock` | MCP only (sibling validator + play) |

`GB_ROMS_HOST_PATH` is set to `${PWD}/roms` so play instances bind the **host**
directory, not `/app/roms` inside the MCP container.

Save files live next to the ROM (`roms/<32-hex>/<name>.gb.state`). Stopping or
idling removes `gb-play-<32-hex>`; the `.state` file stays on the volume. The
next `load_subdirectory_rom` starts a new container and restores it.

## Local stdio

`python server.py` on the host still works when Docker is up and the same
three images exist. Host/dev Python deps are `requirements.txt` (includes
pytest plus the two image pin files). The MCP image uses
`requirements-server.txt`; the play-instance image uses
`requirements-instance.txt`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
docker build -t gb-rom-validator:latest .
docker build -t gb-pyboy-instance:latest -f Dockerfile.instance .
python server.py
```

If those images are missing and the Dockerfiles are on disk, the process will
build them on first use. Prefer the compose-built images when running MCP in
a container.

`python server.py` always uses stdio unless you pass `--http` or set
`GB_MCP_TRANSPORT=streamable-http`.

## Streamable HTTP

```bash
export GB_MCP_HOST=0.0.0.0
export GB_MCP_PORT=8080
export GB_MCP_PATH=/mcp
export GB_MCP_BEARER_TOKEN='replace-me'
# Optional. Leave unset to derive the origin from the request Host header.
# export GB_MCP_PUBLIC_URL=https://gb-mcp-server.com
python server.py --http
```

The process binds `0.0.0.0:8080` by default and serves MCP at `/mcp`.
`GB_MCP_PUBLIC_URL` is **not** required at import or startup. When it is
unset, absolute links and `/.well-known/oauth-protected-resource` derive the
origin from the request `Host` header (`X-Forwarded-Proto` if present), then
fall back to `http://127.0.0.1:8080`. Restarting with a new
`GB_MCP_PUBLIC_URL` is enough when the tunnel hostname changes. No hostname
is hard-coded in source.

SSE responses use `Content-Type: text/event-stream` and are not buffered by
this process. Cloudflare Tunnel buffers ordinary HTTP; it streams SSE. **Quick
Tunnels (`*.trycloudflare.com` / `cloudflared tunnel --url`) do not support
SSE** — use a named tunnel for a real MCP client.

### Bearer token

Every request to `/mcp` requires:

```
Authorization: Bearer <token>
```

`<token>` is either `GB_MCP_BEARER_TOKEN` (shared secret) or a JWT signed
with `GB_MCP_JWT_SECRET` (HS256). Missing or invalid credentials return
**401** with `WWW-Authenticate` and never run a tool body.

`GET /.well-known/oauth-protected-resource` is public. Its `resource` field
is `${GB_MCP_PUBLIC_URL}/mcp` when `GB_MCP_PUBLIC_URL` is set.

### Client config (Claude / Cursor / inspector)

Point the client at **`https://gb-mcp-server.com/mcp`** and send the bearer
header. Do not put the token in a tool argument.

**Claude Desktop / Claude Code** (`claude_desktop_config.json` or `.mcp.json`):

```json
{
  "mcpServers": {
    "gb-mcp-server": {
      "type": "http",
      "url": "https://gb-mcp-server.com/mcp",
      "headers": {
        "Authorization": "Bearer ${GB_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

**Cursor** (`mcp.json`):

```json
{
  "mcpServers": {
    "gb-mcp-server": {
      "url": "https://gb-mcp-server.com/mcp",
      "headers": {
        "Authorization": "Bearer ${GB_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

**MCP Inspector**: transport **Streamable HTTP**, URL
`https://gb-mcp-server.com/mcp`, custom header `Authorization` =
`Bearer ${GB_MCP_BEARER_TOKEN}`. Inspector is a browser; set
`GB_MCP_CORS_ORIGINS` (below) to its origin, often `http://localhost:6274`.

### CORS

Native clients (Claude Desktop, Cursor) do not use CORS. Browser clients do.

| `GB_MCP_CORS_ORIGINS` | Effect |
| --- | --- |
| empty (default) | No CORS headers |
| `http://localhost:6274,http://127.0.0.1:6274` | Those Origins only |
| `*` | Any Origin, **without** credentials |

The server never sets `Access-Control-Allow-Credentials: true` and never
reflects an arbitrary `Origin` with credentials. Allowed request headers
include `Authorization`, `Content-Type`, `Accept`, `mcp-session-id`, and
`mcp-protocol-version`.

## Cloudflare Tunnel

The repo cannot log into Cloudflare or create a domain. No hostname is
hard-coded in source. Finish these steps in the dashboard after copying
`.env.example` to `.env`.

1. Domain already on Cloudflare (you must have this; the repo cannot create it).
2. Zero Trust → Networks → Tunnels → Create a **named** tunnel → copy the
   token into `TUNNEL_TOKEN`.
3. Public hostname route: `TUNNEL_HOSTNAME=gb-mcp-server.com` →
   `http://gb-mcp-server:8080` (compose) or `http://localhost:8080` (host-run).
   Ingress scaffolding is `cloudflared/config.example.yml` (replace
   `${TUNNEL_HOSTNAME}`; do not commit the copied file).
4. Optionally set `GB_MCP_PUBLIC_URL=https://gb-mcp-server.com` and restart MCP
   (leave empty to derive the origin from `Host`).
5. Point the LLM client at `https://gb-mcp-server.com/mcp` with the bearer
   token (JSON above).
6. Do not use `cloudflared tunnel --url` quick tunnels for MCP streaming.

```bash
cp .env.example .env
touch user_subdirectories.sqlite3
docker compose --profile tunnel up --build
```

## Environment

See `.env.example`. Python reads `GB_MCP_*`, `GB_ROM_VALIDATOR_IMAGE`,
`GB_PYBOY_INSTANCE_IMAGE`, and `GB_ROMS_HOST_PATH`. `TUNNEL_TOKEN` and
`TUNNEL_HOSTNAME` are for Cloudflare / compose only.

## Tests

```bash
pytest
```

Unit tests use a fake play-instance backend (no Docker). Optional
`pytest -m docker` runs an integration test when `gb-pyboy-instance:latest`
is already built and the daemon is up.
