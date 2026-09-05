# Changelog

## [Unreleased] — 2026-09-04

### Screenshot-only play loop

- `send_pyboy_input` accepts macros (`hold`, `mash`, `steps`, `buttons`), `until` framebuffer interrupts, wait steps, `gap_frames`, screenshot modes `interrupt_and_final` / `keyframes`, `screenshot_scale` 1–4 (default 4), and uncapped `emulation_speed` (default 0). Caps: 500 steps, `hold_frames` 1–3600. There is no memory or game-state tool; `until` is screenshot-derived on the native 160×144 LCD.
- Idle timeout is 45 minutes (`GB_PYBOY_IDLE_TIMEOUT_SECONDS`, default 2700). `ping_pyboy` resets the idle timer without advancing emulation. `save_battery` writes the cartridge save without stopping PyBoy.
- `load_subdirectory_rom` accepts `emulation_speed` and `idle_timeout_seconds`. `submit_gb_rom` accepts `boot=true` to start PyBoy after a mapped submit.

### Size-strict ROM validation and chunked uploads

- Isolated validator is size-strict: Nintendo logo + header checksum + playable size. A known header size code (0x0148) whose file is shorter than the expected length is rejected (truncated dumps, including a 1 KiB Pokémon header, are not persisted or booted). Extra bytes are allowed only as a whole 16 KiB bank pad. Unrecognized size codes are rejected unless `GB_ROM_ALLOW_UNKNOWN_SIZE=1`. Listing includes `playable` / `unplayable_reason`.
- `begin_gb_rom_upload` / `append_gb_rom_upload` / `finalize_gb_rom_upload` stream a ROM in connector-safe chunks (default 24 KiB decoded), then run the same isolated validator. `abort_gb_rom_upload` deletes staging. `submit_gb_rom` still works for small homebrew; 1 MiB dumps must use the chunked tools.
- Abandoned staging under `roms/.uploads/` expires after 30 minutes. `list_subdirectories_for_email` also runs that expiry so idle servers reclaim disk without a later upload.
- Play-instance boot errors include a short sanitized reason (exit code + instance JSON `error`, including truncation actual vs expected byte counts). Raw docker logs are still not returned. A truncated file never starts a container.
