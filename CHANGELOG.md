# Changelog

## [Unreleased] — 2026-09-04 — screenshot-only play loop

- `send_pyboy_input` accepts macros (`hold`, `mash`, `steps`, `buttons`), `until` framebuffer interrupts, wait steps, `gap_frames`, screenshot modes `interrupt_and_final` / `keyframes`, `screenshot_scale` 1–4 (default 4), and uncapped `emulation_speed` (default 0). Caps: 500 steps, `hold_frames` 1–3600. There is no memory or game-state tool; `until` is screenshot-derived on the native 160×144 LCD.
- Idle timeout is 45 minutes (`GB_PYBOY_IDLE_TIMEOUT_SECONDS`, default 2700). `ping_pyboy` resets the idle timer without advancing emulation. `save_battery` writes the cartridge save without stopping PyBoy.
- `load_subdirectory_rom` accepts `emulation_speed` and `idle_timeout_seconds`. `submit_gb_rom` accepts `boot=true` to start PyBoy after a mapped submit.
