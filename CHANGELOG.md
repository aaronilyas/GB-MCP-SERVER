# Changelog

## [Unreleased] — 2026-09-06

### Model-facing catalog shrink

- MCP `tools/list` is six tools: `add_rom`, `list_games`, `boot`, `play`, `save`, `stop`. The model no longer sees chunked upload tools, `map_subdirectory_to_email`, `ping_pyboy`, or the old play names (`submit_gb_rom`, `list_subdirectories_for_email`, `load_subdirectory_rom`, `reset_pyboy`, `send_pyboy_input`, `save_battery`, `stop_pyboy`).
- After `boot`, `play` / `save` / `stop` take no email or subdirectory. Identity comes from the OAuth session bind. Large dumps use `POST /roms`, not chat chunks.
- Play replies are `{ok, frames, stopped, game}` plus one 4× PNG or one short GIF. The model no longer sees hashes, OCR, screenshot modes, idle countdowns, or `battle_likely`.
- Resources are `gb://how-to-play`, `gb://screen`, `gb://session`. `gb://usage` and `gb://users/{email}/...` are gone. Server `instructions` match `gb://how-to-play`.

## [Unreleased] — 2026-09-05

### Play-session LCD, classifiers, hold abort, and email

- Capture copies the composited 160×144 RGB LCD (`pyboy.screen.ndarray` is a live RGBA view, not a raw BG/window layer). After `load_state`, settle is 8× `tick(1, render=True)` with buttons released — PyBoy `tick(n, render=True)` only composes the last frame of a batch, which left interiors that use the GB window with a stale black slab. Capture/until-eval frames still compose with `tick(1, render=True)`. Additive `window_occluded_likely` on `classifiers` is diagnostic only (not `until.classifier`, does not abort input).
- `battle_likely` is a Gen 1 fight-LCD heuristic: HP-bar-like strips in the enemy/player slots, rejected when `textbox_likely` or `start_menu_likely` is already true. Pallet-like overworld (tree belt vs pavement, fences/ledges) is false. Use `until.classifier=battle_likely` for grass → fight LCD takeover, not for walking, textboxes, or the Start menu.
- Default `macro=hold` abort is two-gate: full-frame `pixel_delta` > 0.12 **and** (`battle_likely` or `start_menu_likely` became true, or mean luminance jumped by > 80). Camera scroll / 1–3 tile walks do not abort; battle takeover, start menu, and warp fade do. `disable_default_hold_abort=true` and `until.on=none` still force-off.
- Instance-proxied tool replies that include `email` echo the mapped caller, never the Docker placeholder `"instance"`. The play container may still boot with `email="instance"` internally; the MCP host rewrites on the way out (`save_battery`, `ping_pyboy`, `send_pyboy_input`, `discard_state`, status).

### Snapshot vs cartridge battery

- `rom.gb.state` is a PyBoy `save_state` snapshot used to resume a session, not cartridge battery. `save_battery` writes that snapshot without stopping; it is not a substitute for `reset_pyboy`. Stop and idle still write the snapshot, then `pyboy.stop(save=True)` flushes cartridge SRAM (`rom.gb.ram`). Live `save_ram` on `save_battery` is optional. A failed `load_state` sets `restore_error`, leaves `restored_state=false`, and cold-boots. A successful restore ticks 8 frames (`tick(1, render=True)` each) with buttons released before the session is ready.
- `reset_pyboy(email, subdirectory, discard_state=true, restore_state=false)` stops the instance if any, unlinks `rom.gb.state` when `discard_state` is true, then loads again with `restore_state=false` (cold boot without the previous PyBoy snapshot). Cartridge SRAM is left alone.
- `load_subdirectory_rom` accepts `restore_state` (default `true`, same resume as today).

### Session email from OAuth

- Play and mapping tools (`list_subdirectories_for_email`, `load_subdirectory_rom`, `reset_pyboy`, `send_pyboy_input`, `ping_pyboy`, `save_battery`, `stop_pyboy`, `submit_gb_rom`, `begin_gb_rom_upload`, `finalize_gb_rom_upload`, `map_subdirectory_to_email`) accept omitted `email` when the current OAuth access token has an `email` or `sub` claim. Explicit `email` still wins. If `email` is omitted and there is no token identity, the result includes a structured `model_request` asking for email (do not invent `trainer@x.ai`). Email is not transport auth; bearer/OAuth stays in `gb_mcp/http.py`.

### Screenshot-only play loop

- `send_pyboy_input` accepts macros (`hold`, `mash`, `steps`, `buttons`), `until` framebuffer interrupts, wait steps, `gap_frames`, screenshot modes `interrupt_and_final` / `keyframes`, `screenshot_scale` 1–4 (default 4), and uncapped `emulation_speed` (default 0). Caps: 500 steps, `hold_frames` 1–3600. There is no memory or game-state tool; `until` is screenshot-derived on the native 160×144 LCD.
- Idle timeout is 45 minutes (`GB_PYBOY_IDLE_TIMEOUT_SECONDS`, default 2700). `ping_pyboy` resets the idle timer without advancing emulation.
- `load_subdirectory_rom` accepts `emulation_speed` and `idle_timeout_seconds`. `submit_gb_rom` accepts `boot=true` to start PyBoy after a mapped submit.

### Size-strict ROM validation and chunked uploads

- Isolated validator is size-strict: Nintendo logo + header checksum + playable size. A known header size code (0x0148) whose file is shorter than the expected length is rejected (truncated dumps, including a 1 KiB Pokémon header, are not persisted or booted). Extra bytes are allowed only as a whole 16 KiB bank pad. Unrecognized size codes are rejected unless `GB_ROM_ALLOW_UNKNOWN_SIZE=1`. Listing includes `playable` / `unplayable_reason`.
- `begin_gb_rom_upload` / `append_gb_rom_upload` / `append_gb_rom_upload_batch` / `finalize_gb_rom_upload` stream a ROM in connector-safe chunks (default 8 KiB decoded; override with `GB_ROM_UPLOAD_CHUNK_BYTES`), then run the same isolated validator. `append_gb_rom_upload_batch` applies up to 16 consecutive chunks (64 KiB decoded) in one call so hosted connectors do not truncate ~32 KiB base64 single-chunk args. `abort_gb_rom_upload` deletes staging. `submit_gb_rom` still works for small homebrew; 1 MiB dumps must use the chunked tools.
- `submit_gb_rom` and `finalize_gb_rom_upload` accept optional `subdirectory` plus `email` to atomically overwrite the `.gb`/`.gbc` in an owned mapping (same 32-hex id). Unmapped, other-owned, or invalid hex names are rejected and nothing is persisted. A truncated sibling is deleted so load cannot boot the 1 KiB dump. `load_subdirectory_rom` calls `assert_rom_playable` before any play instance starts and names the finalize-with-subdirectory repair path.
- Abandoned staging under `roms/.uploads/` expires after 30 minutes. `list_subdirectories_for_email` also runs that expiry so idle servers reclaim disk without a later upload.
- Play-instance boot errors include a short sanitized reason (exit code + instance JSON `error`, including truncation actual vs expected byte counts). Raw docker logs are still not returned. A truncated file never starts a container.

### Agent ingest contract

- Default decoded chunk size is 8 KiB (`GB_ROM_UPLOAD_CHUNK_BYTES`). 1 MiB Pokémon dumps: `begin_gb_rom_upload` → `append_gb_rom_upload_batch` → `finalize_gb_rom_upload(..., email, boot=true)`. Never put ~32 KiB of base64 in a single tool argument when the client is an LLM. No host-path ingest. After map/boot, call `ping_pyboy` if think time exceeds ~30 seconds. One live session per email. Play remains screenshot-only; there is no memory or game-state tool.
