# GB MCP Server

MCP server that accepts Game Boy / Game Boy Color ROMs from an LLM, validates
them inside an isolated Docker container, stores accepted files under
`roms/<32-hex>/`, and maps those directories to the LLM user's **email** in
SQLite (`user_subdirectories.sqlite3`). Mapped ROMs are played in a dedicated
`gb-pyboy-instance` container per subdirectory.

Email ↔ subdirectory mapping is application identity. It is **not** transport
authentication. Remote HTTP clients authenticate with a bearer token **or**
MCP OAuth 2.1. After that check succeeds, tools take an optional `email`
argument. If `email` is omitted, an OAuth access token `email` or `sub` claim
is used as the session identity. If there is no token identity, the tool
returns a structured `model_request` asking for email — do not invent one
(for example `trainer@x.ai`). An explicit `email` still wins when present.
Do not ask the model to type the bearer token, a password, or an API key into
a tool.

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
checksum are required; file extension is not enough. The file length must
match cartridge header 0x0148 (truncated dumps are rejected; extra bytes are
allowed only as a whole 16 KiB bank pad). Unrecognized size codes are
rejected unless `GB_ROM_ALLOW_UNKNOWN_SIZE=1`. Listing exposes `playable`
(false when a stored file is shorter than that header expectation, including
dumps persisted before this check).

Play is **not** in-process. `load_subdirectory_rom` starts or reuses
`gb-play-<subdir hex>`, mounting only that subdirectory (ROM read-only, `.state`
read-write). `send_pyboy_input` talks to that container through
`gb_mcp/emulator` and still returns MCP PNG images. `save_battery` writes the
cartridge save without stopping. `stop_pyboy` and idle timeout write the save
to the volume, then remove the container. The next load starts a new container
and restores `roms/<subdir>/<rom>.state`.

One live session per email. Switching games saves and stops the old instance,
then starts the new one. A dead instance returns a short tool error, not a
Docker dump.

## Tools

| Tool | Purpose |
| --- | --- |
| `submit_gb_rom` | Small homebrew only: one base64 ROM; isolated Docker validation; persist on success. Optional `subdirectory`+`email` replaces an owned mapping in place. Optional `boot=true` starts PyBoy after a mapped submit. 1 MiB dumps must use begin/batch-append/finalize |
| `begin_gb_rom_upload` | Start a chunked upload (`filename`, `total_bytes`, `sha256`) → `{upload_id, chunk_size}` |
| `append_gb_rom_upload` | Append the next consecutive decoded chunk (`chunk_index` + `chunk_base64`). Prefer `append_gb_rom_upload_batch` |
| `append_gb_rom_upload_batch` | Append consecutive chunks in one call (`start_index` + `chunks_base64`); cap 16 chunks / 64 KiB decoded |
| `finalize_gb_rom_upload` | Verify sha256/length, run the same isolated validator, persist, optional map/boot. Optional `subdirectory`+`email` overwrites that owned mapping in place |
| `abort_gb_rom_upload` | Cancel an in-flight chunked upload and delete staging |
| `map_subdirectory_to_email` | Bind a 32-hex directory to the user's email |
| `list_subdirectories_for_email` | List that user's games and header metadata, including `playable` |
| `load_subdirectory_rom` | Start / resume a play instance (default speed uncapped; 45-minute idle) |
| `send_pyboy_input` | Buttons, steps, macros, optional framebuffer `until`; returns PNG screenshot(s) at scale 4 |
| `ping_pyboy` | Reset the idle timer without advancing emulation or pressing buttons |
| `save_battery` | Write the cartridge save without stopping PyBoy |
| `stop_pyboy` | Save, then remove the instance container |

Play is screenshot-only: there is no memory or game-state tool. `until` and
classifiers are derived from the native 160×144 LCD. Default
`emulation_speed` is `0` (uncapped). Screenshots are nearest-neighbor
upscaled (`screenshot_scale` default 4 → 640×576). Idle sessions auto-save
to the volume and remove the container after **45 minutes** without
`send_pyboy_input` or `ping_pyboy`. Agents should call `ping_pyboy` if they
will think longer than about 30 seconds. Override idle with
`GB_PYBOY_IDLE_TIMEOUT_SECONDS` (default 2700).

### Chunked ROM upload (1 MiB and up)

Hosted MCP connectors and LLM tool-argument caps often cannot carry a 1 MiB
ROM as a single `submit_gb_rom.rom_base64` string (~1.4 MiB of base64). The
server limit (`MAX_ROM_B64_CHARS`, ~11M characters for 8 MiB decoded) is not
the problem — the transport is. Do **not** work around this by reading a
host filesystem path. Use the chunked ingest:

1. SHA-256 the complete `.gb` / `.gbc` and note `total_bytes` (1,048,576 for
   a 1 MiB dump).
2. `begin_gb_rom_upload(filename, total_bytes, sha256, email?)` →
   `{upload_id, chunk_size}`. Default `chunk_size` is 8 KiB decoded
   (~11 KiB base64). Override with `GB_ROM_UPLOAD_CHUNK_BYTES`. Do **not**
   send 24 KiB single chunks through LLM tool args; hosted connectors
   truncate ~32 KiB base64.
3. If an append times out or its response is truncated, call
   `get_gb_rom_upload(upload_id)` and resume at its returned `next_index`.
   The progress response contains no staging paths or ROM bytes. Retrying the
   immediately previous single chunk with identical bytes is idempotent.
4. Split the file into consecutive slices of at most `chunk_size` bytes.
   Prefer `append_gb_rom_upload_batch(upload_id, start_index, chunks_base64)`
   with up to 16 slices / 64 KiB decoded per call. Single-slice
   `append_gb_rom_upload(upload_id, i, chunk_base64)` still works. Holes,
   oversized chunks or batches, and `received_bytes > total_bytes` are
   rejected.
5. `finalize_gb_rom_upload(upload_id, filename?, email?, boot?, subdirectory?)`
   concatenates the staging files under `roms/.uploads/<upload_id>/` (mode
   `0700`), verifies sha256 and length, then runs the **same** isolated
   validator (`container up first`, ROM bytes on stdin `docker exec`,
   `--network=none`). On success the ROM is persisted under `roms/<32-hex>/`
   and mapped/booted like `submit_gb_rom`. Staging is always deleted. Call
   `abort_gb_rom_upload(upload_id)` to drop an in-flight upload without
   persisting. Abandoned uploads expire after 30 minutes; listing a user's
   games also reclaims expired staging.
6. To replace an unplayable mapping (`list_subdirectories_for_email` shows
   `playable: false`, for example a 1 KiB Pokémon header), pass the existing
   32-hex id: `finalize_gb_rom_upload(upload_id, email=..., subdirectory=<id>)`.
   That overwrites the `.gb` in that directory in place (same subdirectory
   id). Omitting `subdirectory` allocates a new id. `submit_gb_rom` accepts
   the same optional `subdirectory` for small homebrew. Never read a host
   or sandbox attachment path into `rom_base64`.

`submit_gb_rom` remains the right tool for small homebrew that fits in one
argument.

## Resources (read-only)

| URI | Content |
| --- | --- |
| `gb://users/{email}/roms` | Owned ROM list and game metadata |
| `gb://users/{email}/roms/{subdirectory}` | Cartridge header metadata for an owned ROM |
| `gb://users/{email}/session` | Live play-instance status for that email |
| `gb://usage` | How a connected model should use this server (submit, map, list, load, play, ping, save, stop). Contains no user data. |

## Compose

```bash
./start.sh
./stop.sh
```

`./start.sh` creates `user_subdirectories.sqlite3` if needed, builds the three
images, and starts long-lived `gb-mcp-server` in the background. HTTP mode
requires `GB_MCP_BEARER_TOKEN` or `GB_MCP_JWT_SECRET` in `.env` or the
environment. A non-empty `TUNNEL_TOKEN` (or `./start.sh --tunnel`) also starts
cloudflared. `./stop.sh` asks running `gb-play-*` instances to write their
`.state` files, removes leftover validator containers, then takes the compose
stack down. ROMs, save states, and the SQLite map stay on disk.

Equivalent compose commands:

```bash
touch user_subdirectories.sqlite3
docker compose up --build -d
docker compose --profile tunnel down
```

That starts long-lived `gb-mcp-server` and builds `gb-rom-validator` plus
`gb-pyboy-instance` (those two exit immediately; they exist so `docker run`
can use the images). MCP is on the compose network at port 8080. Uncomment
`ports` in `compose.yaml` to debug against `http://127.0.0.1:8080/mcp`.
Play instances are sibling containers, not compose services — `./stop.sh`
saves and removes them; `docker compose down` alone does not.

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

The process binds `0.0.0.0:8080` by default and serves MCP at `/mcp`. The
origin path `/` is an alias of that endpoint so hosted connectors (ChatGPT
custom connectors in particular) can paste `https://www.gb-mcp-server.com`
or `https://gb-mcp-server.com/mcp`. `GB_MCP_PUBLIC_URL` is **not** required
at import or startup. When it is unset, absolute links, RFC 9728 / RFC 8414
metadata, and OAuth token `iss` / `aud` derive the origin from the request
`Host` header (`X-Forwarded-Proto` if present), then fall back to
`http://127.0.0.1:8080`. If it is set, a `www.` alias of that host still
uses the request origin so issuer and `resource` match the URL the client
pasted. Restarting with a new `GB_MCP_PUBLIC_URL` is enough when the tunnel
hostname changes. No hostname is hard-coded in source. The issuer string is
that origin with no trailing slash; it must match `authorization_servers`
exactly.

SSE responses use `Content-Type: text/event-stream` and are not buffered by
this process. Cloudflare Tunnel buffers ordinary HTTP; it streams SSE. **Quick
Tunnels (`*.trycloudflare.com` / `cloudflared tunnel --url`) do not support
SSE** — use a named tunnel for a real MCP client.

### Dual transport auth

Every request to `/mcp` except `OPTIONS` requires:

```
Authorization: Bearer <token>
```

`<token>` is one of:

1. `GB_MCP_BEARER_TOKEN` (shared secret) — header-capable native clients
2. A JWT signed with `GB_MCP_JWT_SECRET` (HS256) — same clients, operator-issued
3. An access token from this process's OAuth authorization server — hosted LLM
   connectors that cannot set a static header

Missing or invalid credentials return **401** with `WWW-Authenticate`
(`resource_metadata`, `error`, `error_description`) and never run a tool body.
There is no query-string token.

OAuth access tokens are HS256 JWTs with `iss` (public origin), `aud` (MCP
resource URL), `exp`, `sub`, and `scope`. They are signed with
`GB_MCP_JWT_SECRET` when that is set, otherwise with a key derived from
`GB_MCP_BEARER_TOKEN`. HTTP mode still refuses to boot if neither variable is
set.

### Remote LLM clients / OAuth

Hosted web UIs (ChatGPT custom connectors, Claude.ai custom connectors, Gemini
custom MCP, Copilot Studio, and any other spec-compliant MCP host) cannot paste
`Authorization: Bearer <GB_MCP_BEARER_TOKEN>`. They speak **MCP Authorization
(OAuth 2.1)**: unauthenticated `initialize` → 401 → RFC 9728 protected-resource
metadata → RFC 8414 (or OpenID Connect discovery) → Dynamic Client Registration
→ authorization-code + PKCE S256 → Bearer access token on `/mcp`.

Use the public origin or the MCP path — both work:

```
https://<public-host>
https://<public-host>/mcp
```

`www` and apex are treated as the same site. That host must be a **named**
Cloudflare tunnel (or other reverse proxy that streams SSE). Quick tunnels
(`*.trycloudflare.com`) still do not stream SSE.

In the host UI, choose OAuth (not “no authentication”, not a pasted API key).
Complete the consent page in the browser. Do **not** paste
`GB_MCP_BEARER_TOKEN` into the connector or into a tool argument.

This server implements MCP OAuth 2.1 so any spec-compliant host can connect.
Claude Desktop / Cursor-style static bearer remains a supported alternative
(next subsection).

Unauthenticated discovery (any client must be able to `GET` these):

| URL | Spec |
| --- | --- |
| `/.well-known/oauth-protected-resource` | RFC 9728 (root) |
| `/.well-known/oauth-protected-resource/mcp` | RFC 9728 §3.1 path-aware |
| `/.well-known/oauth-authorization-server` | RFC 8414 (root) |
| `/.well-known/oauth-authorization-server/mcp` | RFC 8414 path-aware |
| `/.well-known/openid-configuration` | OpenID Connect discovery alias |

OAuth endpoints (also unauthenticated):

| URL | Role |
| --- | --- |
| `GET` / `POST` `/authorize` | Authorization code + PKCE S256; HTML consent (Allow / Deny). No bearer token to paste. |
| `POST` `/register` | RFC 7591 dynamic client registration (`201` + `client_id`; public client, `token_endpoint_auth_method=none`) |
| `POST` `/token` | `authorization_code` and `refresh_token` (refresh tokens rotate) |

Redirect URIs are exact matches against the URIs the client registered (host +
path, never a string prefix). `http://127.0.0.1` and `http://localhost`
loopback callbacks are accepted, as is any `https` callback the client
registers (including hosted-connector callback hosts).

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

Native clients (Claude Desktop, Cursor) do not use CORS. Browser clients
(MCP Inspector, hosted OAuth in a browser) do. When set, CORS applies to
`/mcp`, well-known metadata, and OAuth routes.

| `GB_MCP_CORS_ORIGINS` | Effect |
| --- | --- |
| empty (default) | `*` — any Origin, **without** credentials (ChatGPT / Claude.ai preflight) |
| `none` | No CORS headers |
| `http://localhost:6274,http://127.0.0.1:6274` | Those Origins only |
| `*` | Any Origin, **without** credentials |

The server never sets `Access-Control-Allow-Credentials: true` and never
reflects an arbitrary `Origin` with credentials. Allowed request headers
include `Authorization`, `Content-Type`, `Accept`, `mcp-session-id`,
`mcp-protocol-version`, and `Last-Event-ID`.

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
5. Point header-capable clients at `https://gb-mcp-server.com/mcp` with the
   bearer token (JSON above). Point hosted LLM UIs at the same MCP URL and
   choose OAuth; they discover the authorization server from well-known
   metadata and the consent page.
6. Do not use `cloudflared tunnel --url` quick tunnels for MCP streaming.

```bash
cp .env.example .env
# set TUNNEL_TOKEN and GB_MCP_BEARER_TOKEN (or GB_MCP_JWT_SECRET)
./start.sh --tunnel
```

## Environment

See `.env.example`. Python reads `GB_MCP_*`, `GB_ROM_VALIDATOR_IMAGE`,
`GB_PYBOY_INSTANCE_IMAGE`, `GB_ROMS_HOST_PATH`, `GB_ROM_UPLOAD_CHUNK_BYTES`,
and `GB_ROM_ALLOW_UNKNOWN_SIZE`. `TUNNEL_TOKEN` and `TUNNEL_HOSTNAME` are
for Cloudflare / compose only.

## Tests

```bash
pytest
```

Unit tests use a fake play-instance backend (no Docker). Optional
`pytest -m docker` runs an integration test when `gb-pyboy-instance:latest`
is already built and the daemon is up.
