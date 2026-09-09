# GB MCP Server

MCP server that accepts Game Boy / Game Boy Color ROMs from an LLM, validates
them inside an isolated Docker container, stores accepted files under
`roms/<32-hex>/`, and maps those directories to the LLM user's **email** in
SQLite (`user_subdirectories.sqlite3`). Mapped ROMs are played in a dedicated
`gb-pyboy-instance` container per subdirectory.

## Runtime

Three images, one long-lived process:

| Image | Role |
| --- | --- |
| `gb-mcp-server` | MCP tools + resources + SQLite. No user ROMs baked in. |
| `gb-rom-validator` | Throwaway `--network=none` container per ROM ingest |
| `gb-pyboy-instance` | One headless PyBoy (`window=null`) per `roms/<32-hex>/` |

The MCP process talks to the **host Docker daemon** (sibling containers). It
does not run Docker-in-Docker. After MCP itself is containerized, mount
`/var/run/docker.sock`. Validator and instance containers never get the socket
and are not published.

## Auth

Remote HTTP clients authenticate with a bearer token, an operator JWT
(`GB_MCP_JWT_SECRET`, HS256), or MCP OAuth 2.1. That is **transport auth**,
not application identity. Never ask the model for a bearer token, a
password, or an API key.

Application identity is the **consent email claim** on the OAuth access
token (`email` plus an email-shaped `sub`), or an optional `email` on
`list_games`, `boot`, and `add_rom` only. Explicit `email` wins over token
identity.

Grok path: connect → consent **with email** → `list_games` returns
`{ok: true}` and the mapped games (no `model_request`). Or pass explicit
`email` on `list_games` / `boot` / `add_rom`. After `boot`, `play` /
`save` / `stop` are session-bound and take no email argument.

If `email` is omitted and there is no token identity, those tools return a
structured `model_request` asking for email — do not invent one (for
example `trainer@x.ai`).

`POST /roms` maps from the token email or an optional JSON/form `email`.
Large dumps stay on that HTTP route, not chat chunks.

HTTP mode refuses to boot unless `GB_MCP_BEARER_TOKEN` or `GB_MCP_JWT_SECRET`
is set. There is no query-string token.

## Surface

The supported operator/model catalog is six tools:

| Tool | Purpose |
| --- | --- |
| `add_rom` | Small homebrew: one `rom_base64` payload; isolated Docker validation; persist on success. Optional `email` |
| `list_games` | Games mapped to this email (`title`, `id`, `playable`). Optional `email` |
| `boot` | Start or resume a play instance. `reset=true` drops the PyBoy snapshot and cold-boots. Optional `email` |
| `play` | Buttons / macros; returns 4× PNG stills (640×576). Screenshot-only — no memory dumps. No email argument |
| `save` | Write the PyBoy snapshot (`rom.gb.state`) without stopping. No email argument |
| `stop` | Snapshot, flush cartridge SRAM (`rom.gb.ram`), remove the instance container. No email argument |

**Large dumps use `POST /roms` (HTTP), not chat chunks.** Hosted connectors
cannot carry a 1 MiB ROM as a tool argument. Small homebrew can use
`add_rom` / `rom_base64`. `POST /roms` maps from the token email or an
optional JSON/form `email`.

Legacy MCP names (`submit_gb_rom`, `send_pyboy_input`, `ping_pyboy`, the
chunked-upload tools, …) are gone from the public catalog; see git history.

One live session per email. Switching games saves and stops the old instance.
Idle timeout (default 45 minutes) auto-saves and removes the container. A
dead instance returns a short tool error, not a Docker dump.

## Run

Copy [`.env.example`](.env.example) to `.env`. HTTP needs
`GB_MCP_BEARER_TOKEN` or `GB_MCP_JWT_SECRET`. See that file for image names,
idle timeout, and (optional) Cloudflare tunnel variables.

```bash
./start.sh
./stop.sh
```

`./start.sh` creates `user_subdirectories.sqlite3` if needed, builds the three
images, and starts long-lived `gb-mcp-server` in the background. A non-empty
`TUNNEL_TOKEN` (or `./start.sh --tunnel`) also starts cloudflared. `./stop.sh`
asks running `gb-play-*` instances to write saves, removes leftover validator
containers, then takes the compose stack down. ROMs, save states, and the
SQLite map stay on disk.

```bash
touch user_subdirectories.sqlite3
docker compose up --build -d
```

MCP is on the compose network at port 8080. Uncomment `ports` in
`compose.yaml` to debug against `http://127.0.0.1:8080/mcp`. Play instances
are sibling containers, not compose services.

Host stdio (Docker daemon up, same three images):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

`python server.py` uses stdio unless you pass `--http` or set
`GB_MCP_TRANSPORT=streamable-http`.

```bash
export GB_MCP_HOST=0.0.0.0
export GB_MCP_PORT=8080
export GB_MCP_PATH=/mcp
export GB_MCP_BEARER_TOKEN='replace-me'
python server.py --http
```

The process binds `0.0.0.0:8080` by default and serves MCP at `/mcp`. Origin
`/` is an alias of that endpoint. Hosted connectors should choose OAuth, not
a pasted API key. **Quick Tunnels (`*.trycloudflare.com`) do not support
SSE** — use a named tunnel (or another proxy that streams SSE) for a real
MCP client.

```bash
pytest
```

Unit tests use a fake play-instance backend (no Docker). Optional
`pytest -m docker` runs an integration test when `gb-pyboy-instance:latest`
is already built and the daemon is up.

## More

Docker isolation (validator stdin-after-up, play-instance mounts, no socket
in those containers), snapshot vs SRAM, and chunked HTTP/storage internals
are in [docs/operator.md](docs/operator.md).

Historical play-loop notes:
[docs/history/IMPLEMENTATION_REPORT.md](docs/history/IMPLEMENTATION_REPORT.md).
The screenshot-only contract in
[docs/history/AGENT_CONTRACT.md](docs/history/AGENT_CONTRACT.md) is frozen.

## License

MIT. See [LICENSE](LICENSE).
