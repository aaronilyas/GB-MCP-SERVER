# GB MCP Server

MCP server that accepts Game Boy / Game Boy Color ROMs from an LLM, validates
them inside an isolated Docker container, stores accepted files under
`roms/<32-hex>/`, and maps those directories to the LLM user's **email** in
SQLite (`user_subdirectories.sqlite3`). Mapped ROMs can be loaded into an
in-process PyBoy session.

Email ↔ subdirectory mapping is application identity. It is **not** transport
authentication. Remote HTTP clients authenticate with a bearer token; tools
still take `email` arguments after that check succeeds. Do not ask the model
to type the bearer token (or any password) into a tool.

## Tools

| Tool | Purpose |
| --- | --- |
| `submit_gb_rom` | Base64 ROM in; isolated Docker validation; persist on success |
| `map_subdirectory_to_email` | Bind a 32-hex directory to the user's email |
| `list_subdirectories_for_email` | List that user's games and header metadata |
| `load_subdirectory_rom` | Start / resume PyBoy for an owned directory |
| `send_pyboy_input` | Button chords; returns PNG screenshot(s) |
| `stop_pyboy` | Save and close the session |

Idle sessions auto-save and close after five minutes without button input.

## Resources (read-only)

| URI | Content |
| --- | --- |
| `gb://users/{email}/roms` | Owned ROM list and game metadata |
| `gb://users/{email}/roms/{subdirectory}` | Cartridge header metadata for an owned ROM |
| `gb://users/{email}/session` | Live PyBoy session status for that email |

## Local stdio (default)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

`python server.py` always uses stdio unless you pass `--http` or set
`GB_MCP_TRANSPORT=streamable-http`. Build the validator image once so ROM
submission can run isolated containers:

```bash
docker build -t gb-rom-validator:latest .
```

## Streamable HTTP

```bash
export GB_MCP_HOST=0.0.0.0
export GB_MCP_PORT=8080
export GB_MCP_PATH=/mcp
export GB_MCP_BEARER_TOKEN='replace-me'
# Optional until a public hostname exists; see Cloudflare section.
# export GB_MCP_PUBLIC_URL=https://gb.example.com
python server.py --http
```

The process binds `0.0.0.0:8080` by default and serves MCP at `/mcp`.
`GB_MCP_PUBLIC_URL` is **not** required at import or startup. When it is
unset, absolute links and `/.well-known/oauth-protected-resource` derive the
origin from the request `Host` header (`X-Forwarded-Proto` if present), then
fall back to `http://127.0.0.1:8080`. Restarting with a new
`GB_MCP_PUBLIC_URL` is enough when the tunnel hostname changes.

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

Point the client at **`${GB_MCP_PUBLIC_URL}/mcp`** and send the bearer header.
Do not put the token in a tool argument.

**Claude Desktop / Claude Code** (`claude_desktop_config.json` or `.mcp.json`):

```json
{
  "mcpServers": {
    "gb-mcp-server": {
      "type": "http",
      "url": "${GB_MCP_PUBLIC_URL}/mcp",
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
      "url": "${GB_MCP_PUBLIC_URL}/mcp",
      "headers": {
        "Authorization": "Bearer ${GB_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

**MCP Inspector**: transport **Streamable HTTP**, URL
`${GB_MCP_PUBLIC_URL}/mcp`, custom header `Authorization` =
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

## Public URL via Cloudflare Tunnel

The repo cannot log into Cloudflare or create a domain. Finish these steps in
the dashboard after copying `.env.example` to `.env`.

1. Domain already on Cloudflare (you must have this; the repo cannot create it).
2. Zero Trust → Networks → Tunnels → Create a **named** tunnel → copy the
   token into `TUNNEL_TOKEN`.
3. Public hostname route: `TUNNEL_HOSTNAME` → `http://gb-mcp-server:8080`
   (compose) or `http://localhost:8080` (host-run). Ingress scaffolding is
   `cloudflared/config.example.yml` (replace `${TUNNEL_HOSTNAME}`; do not
   commit the copied file).
4. Set `GB_MCP_PUBLIC_URL=https://<that hostname>` and restart MCP.
5. Point the LLM client at `https://<hostname>/mcp` with the bearer token
   (JSON above).
6. Do not use `cloudflared tunnel --url` quick tunnels for MCP streaming.

Compose starts MCP + cloudflared once `TUNNEL_TOKEN` is pasted and
`GB_MCP_PUBLIC_URL` / `GB_MCP_BEARER_TOKEN` are set:

```bash
cp .env.example .env
touch user_subdirectories.sqlite3
docker compose up --build
```

`gb-mcp-server` listens on the compose network only (`expose: "8080"`). The
host firewall does not need 8080 open. Uncomment `ports` in
`docker-compose.yml` only to debug against `http://127.0.0.1:8080/mcp`.

## Environment

See `.env.example`. Python reads `GB_MCP_*`. `TUNNEL_TOKEN` and
`TUNNEL_HOSTNAME` are for Cloudflare / compose only.

## Tests

```bash
pytest
```
