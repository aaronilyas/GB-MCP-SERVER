# Operator notes

Docker isolation, snapshot vs SRAM, HTTP ROM ingest internals, idle auto-save,
and the one-session-per-email rule. The short operator README is
[../README.md](../README.md). Historical play-loop writeups live under
[history/](history/).

## Docker isolation

Three images. Only `gb-mcp-server` is long-lived. The MCP process talks to the
**host Docker daemon** and starts **sibling** containers. It is not
Docker-in-Docker. After MCP itself is containerized, mount `/var/run/docker.sock`
on **that** container only.

Validator and play-instance containers **never** receive the Docker socket, are
not published, and are not Compose services that stay up. Compose builds those
two images as one-shot services so `docker run` can use them.

`GB_ROMS_HOST_PATH` (compose sets it to `${PWD}/roms`) is the path the Docker
**daemon** bind-mounts. Play instances must see the host `roms/` tree, not
`/app/roms` inside the MCP container.

### Validator (`gb-rom-validator`)

Every persist path (small `add_rom` / `rom_base64`, and large `POST /roms`)
runs the same isolated validator:

1. Create and **start** a throwaway container **before** any ROM bytes enter it
   (`gb-rom-validate-<id>`).
2. Lock-down: `--network=none`, `--read-only`, `--cap-drop ALL`,
   `no-new-privileges`, non-root user, memory/CPU/pids caps, tmpfs for `/tmp`
   and `/work`.
3. Stream ROM bytes on **stdin** via `docker exec` only after the container is
   up.
4. Require Nintendo logo + header checksum + playable size (header `0x0148`).
   Truncated dumps are rejected. Extra bytes are allowed only as a whole 16 KiB
   bank pad. Unrecognized size codes are rejected unless
   `GB_ROM_ALLOW_UNKNOWN_SIZE=1`.
5. `docker rm -f` the container. File extension is not enough.

A stored file shorter than the header size is listed `playable: false` and is
not booted.

### Play instances (`gb-pyboy-instance`)

`boot` starts or reuses `gb-play-<32-hex>`. That container:

- is a sibling of MCP (`docker run`, not a Compose service)
- uses `--network=none`, `--read-only` rootfs, `--cap-drop ALL`,
  `no-new-privileges`, no published ports
- bind-mounts **only** `roms/<32-hex>/` (ROM file read-only; the directory is
  writable for `.state` / `.ram`)
- does **not** mount `/var/run/docker.sock` or any other user's subdirectory
- talks to MCP through `docker exec` to a loopback HTTP server inside the
  instance

`./stop.sh` asks running `gb-play-*` instances to write saves, then removes
them, then takes the Compose stack down. `docker compose down` alone does not
save or remove play instances.

A dead instance returns a short tool error, not a Docker dump.

## Snapshot vs cartridge SRAM

Two files sit next to the ROM. They are not the same thing.

| File | What it is | Who writes it |
| --- | --- | --- |
| `rom.gb.state` (or `.gbc.state`) | PyBoy `save_state` snapshot used to **resume a session** | `save`; also the first step of `stop` / idle close |
| `rom.gb.ram` (or `.gbc.ram`) | Cartridge battery SRAM | `stop` / idle: `pyboy.stop(save=True)`. Optional live `save_ram` on `save` if PyBoy exposes it |

- **`save`** writes the snapshot **without** stopping. It is not a cold boot
  and is not a substitute for `boot(..., reset=true)`.
- **`stop`** and **idle timeout** write the snapshot, then flush SRAM, then
  remove the `gb-play-<32-hex>` container. The `.state` file stays on the
  volume.
- The next **`boot`** starts a new container and restores `.state` by default.
- **`boot(..., reset=true)`** stops a running instance if any, **unlinks the
  snapshot**, and cold-boots without the previous PyBoy snapshot. Cartridge
  SRAM is left alone.
- A failed `load_state` cold-boots (`restored_state: false`). A successful
  restore settles a few frames with buttons released before the session is
  ready.

If a restore is poisoned (camera off-map, stairs do nothing), drop the snapshot
with `reset=true` and start a new in-game save. Do not keep restoring that
`.state`.

## Chunk internals (HTTP / storage, not model tools)

Large dumps must not go through chat or MCP tool arguments. Operators (and
HTTP clients) use **`POST /roms`**. Small homebrew that fits in one tool
argument uses `add_rom` / `rom_base64`. There is **no host-path ingest**.

`begin` / `append` / `finalize` are the **storage implementation** behind that
HTTP path. They are not the public six-tool catalog.

1. **Begin** — allocate `roms/.uploads/<upload_id>/` (directory mode `0700`)
   with `filename`, `total_bytes`, and SHA-256. Response includes `upload_id`
   and `chunk_size` (default **8 KiB decoded**; override
   `GB_ROM_UPLOAD_CHUNK_BYTES`). Staging is never under `roms/<32-hex>/`.
2. **Append** — consecutive decoded slices only. Holes, oversized chunks, and
   `received_bytes > total_bytes` are rejected. Retrying the immediately
   previous slice with identical bytes is idempotent. Progress (`next_index`,
   `received_bytes`) contains no staging paths and no ROM bytes.
3. **Finalize** — concatenate staging, verify SHA-256 and length, run the
   **same** isolated validator (container up first, ROM on stdin,
   `--network=none`). On success the ROM is persisted under `roms/<32-hex>/`
   and mapped to the caller's email. Staging is always deleted.
4. **Abort / expiry** — drop an in-flight upload without persisting. Abandoned
   staging expires after **30 minutes**. Listing a user's games also reclaims
   expired staging.

Hosted MCP connectors truncate large base64 tool arguments. That is why 1 MiB
dumps are HTTP (`POST /roms`), not chat chunks. The server's own base64 cap
is not the limiter.

Optional `subdirectory` on persist overwrites an **owned** mapping in place
(same 32-hex id) so an unplayable truncated dump can be replaced. Unmapped or
other-owned ids are rejected and nothing is persisted.

## Idle auto-save and one session per email

One live play instance **per email**. `boot` of a different game for that
email saves and stops the old instance, then starts the new one. `boot` of the
already-running game is reused (`already_running`) and does not ignore a
poisoned snapshot — use `reset=true` for a cold boot.

Idle timeout defaults to **45 minutes** (`GB_PYBOY_IDLE_TIMEOUT_SECONDS`,
default 2700). `play` resets that timer. After 45 minutes with no play
activity the instance auto-saves (snapshot, then SRAM) and removes the
container — the same close path as `stop`. The idle loop does not tick the
emulator while waiting. `save` does not extend the idle window.

Play is screenshot-only: 4× nearest-neighbor PNG stills (native 160×144 →
640×576). There is no memory or game-state tool.

Override session-start speed with `GB_PYBOY_EMULATION_SPEED` (default `0`,
uncapped). See `.env.example` for the rest.
