# Implementation report: screenshot-only play loop

Landed on `main` in `571331c` (2026-09-04). Target: an LLM can play Pallet Town
→ Brock in ≤75 minutes wall-clock on average using screenshots only, with
roughly 80–150 decision turns.

## Later play fixes (2026-09-05)

- `reset_pyboy` with `discard_state=true` unlinks the PyBoy snapshot and cold-boots.
  `.state` is not cartridge battery.
- A successful `load_state` ticks **8 settle frames** with buttons released.
- Intermediate `tick(n, render=False)` is **capped at 4**; a new button chord
  renders before any `render=False` batch.

## How to call the new `send_pyboy_input` macros

Old callers still work: `buttons` + `hold_frames` + `screenshot_mode=final`.
Breaking default changes:

- `emulation_speed` defaults to **0 (uncapped)** (session start previously forced 1×)
- screenshots are **4× nearest-neighbor** (was native 160×144)
- idle timeout is **45 minutes** (was 5 minutes)

### Walk one tile north

Prefer one-tile steps. Do not hold a d-pad for 3600 frames across a door.

```json
{
  "email": "trainer@example.com",
  "subdirectory": "<32-hex>",
  "macro": "hold",
  "buttons": ["up"],
  "hold_frames": 16,
  "gap_frames": 12,
  "emulation_speed": 0,
  "screenshot_mode": "final",
  "screenshot_scale": 4
}
```

`gap_frames` may be 8–16. If stairs do nothing or the camera scrolls out of
bounds, call `reset_pyboy` with `discard_state=true` and start a new game; do
not keep restoring a poisoned `.state`.

If `until` is omitted, a **default hold abort** still fires on full-screen
`pixel_delta_above` with threshold **0.12** versus the **start-of-call** native
frame, so “hold Up through grass” cannot grind a wild battle with Up stuck.
Disable with `disable_default_hold_abort: true` or `until.on: "none"`.

### Mash A until the dialogue box is stable

```json
{
  "email": "trainer@example.com",
  "subdirectory": "<32-hex>",
  "macro": "mash",
  "mash_button": "a",
  "mash_press_frames": 4,
  "mash_release_frames": 4,
  "max_frames": 3600,
  "emulation_speed": 0,
  "screenshot_mode": "final",
  "until": {
    "on": "stable",
    "region": [0, 96, 160, 48],
    "threshold": 0.02,
    "stable_frames": 12
  }
}
```

Keep-alive without moving the character: `ping_pyboy`. Save without stopping:
`save_battery`. Think-time longer than ~30s should ping.

## Defaults

| Knob | Value |
| --- | --- |
| `emulation_speed` | `0` (uncapped / `pyboy.set_emulation_speed(0)`) |
| `screenshot_scale` | `4` nearest-neighbor (640×576) |
| `screenshot_mode` | `final` |
| idle timeout | 2700 s (45 min); env `GB_PYBOY_IDLE_TIMEOUT_SECONDS` |
| `until_eval_interval` | 4 (range 1–15) |
| caller `until.threshold` | 0.08 |
| default hold abort | full-screen delta **> 0.12** vs start-of-call, on `macro=hold` |
| pixel_delta baseline | start-of-call native frame (not previous eval) |
| `stable` baseline | previous **evaluated** frame |
| region hash | `blake2s(native RGB crop bytes, digest_size=8)` hex |
| named hash boxes | `full [0,0,160,144]`, `bottom [0,96,160,48]`, `center [40,32,80,80]` |
| max frames / hold_frames | 3600 |
| max steps | 500 |
| `gap_frames` | 0–60, default 0 |
| call wall-clock | 20 s uncapped; up to 70 s at 1× |
| OCR | `ocr=false`; missing Tesseract → `ocr_error: "disabled"` |

## Explicitly not added

- Memory peek / WRAM / HRAM / `pyboy.memory`
- Symbol lookup, party list, map ID, player X/Y, battle structs
- `get_game_state` or tile-grid wrappers
- Pokémon ROM in CI (FakePyBoy + tiny valid GB ROM + synthetic 160×144 frames)

CI fails if response keys match `FORBIDDEN_RESPONSE_KEY_NEEDLES` (`wram`, `hram`,
`memory`, `party`, `map_id`, `player_x`, …). `strip_forbidden_keys` is also
applied on the instance before the tool returns.

## Known classifier failure modes

Coarse pixel heuristics on the native 160×144 buffer. Prefer false positives
on `battle_likely` over missing a wild encounter; default hold abort is the
real safety net for grass.

- **textbox_likely** — bottom 48px needs a dark frame and a much lighter inner
  window. Misses unframed / light-bordered text. A full-bottom white slab does
  not fire.
- **battle_likely** — top-vs-bottom color split and/or 2–10px light status bars.
  Over-triggers on any two-band + bar layout. Misses battles with no split and
  no thin light bars. Thick dialogue windows are not treated as bars.
- **start_menu_likely** — left ~80px mostly very light over most of the height,
  rest different. Misses right-hand or dark menus. A full-white screen does not
  fire.

`center` hash box `[40,32,80,80]` overlaps the top 16px of a Gen 1 dialogue
window (`y≥96`). `bottom` still changes more than `center` on a dialogue-bar
fixture.

## Files changed

| Area | Files |
| --- | --- |
| Contract | `docs/history/AGENT_CONTRACT.md`, `gb_mcp/emulator/play_limits.py`, `gb_mcp/emulator/input_schema.py` |
| Engine | `gb_mcp/emulator/input_engine.py`, `gb_mcp/emulator/play_runtime.py` |
| Vision | `gb_mcp/emulator/vision.py` |
| OCR | `gb_mcp/emulator/ocr.py` |
| Session | `gb_mcp/config.py`, `gb_mcp/emulator/loop.py`, `session.py`, `backend.py`, `instance.py`, `docker/instance_server.py` |
| MCP / docs | `server.py`, `README.md`, `.env.example`, `CHANGELOG.md` |
| Tests | `tests/conftest.py`, `test_input_schema.py`, `test_input_engine.py`, `test_vision.py`, `test_session_lifecycle.py`, `test_play_loop.py`, `test_leakage.py`, plus updates to `test_emulator.py`, `test_server.py`, `test_resources.py` |

## Residual risk to the 75-minute Pallet → Brock goal

- **Uncapped speed + 3600-frame holds/mashes** are the main wall-clock win.
  Intermediate ticks use `pyboy.tick(n, render=False)`; LCD is rendered only
  on capture / until-eval frames.
- **Idle loop no longer ticks.** Uncapped think-time no longer fast-forwards
  the overworld. After map/boot, agents must `ping_pyboy` if they will think
  > ~30s (45-minute idle is the hard close). One live session per email:
  switching games saves and stops the old instance.
- **Default hold abort (0.12)** stops a grass hold on a large full-screen
  change (wild battle / map transition). A very subtle fade might not trip
  0.12; callers should still pass `until` for dialogue (`stable` on the bottom
  box) and battles (`classifier: battle_likely` or full-screen delta).
- **Classifiers are not ROM-tuned.** Do not rely on `battle_likely` alone;
  combine with `pixel_delta_above` / default hold abort.
- **20s call timeout** is plenty for 3600 uncapped frames on a normal CPU;
  at `emulation_speed=1` the timeout scales up to 70s.
- This server does not play the game. Reaching Brock in 80–150 turns still
  depends on the agent’s screenshot policy (`interrupt_and_final` / `keyframes`)
  and not using 1-frame taps for walking.
- **1 MiB Pokémon dumps cannot use `submit_gb_rom`.** Connector argument
  limits typically cannot carry ~1.4 MiB of base64, and a single ~32 KiB
  base64 tool argument is truncated when the client is an LLM. Default
  decoded chunk size is 8 KiB (override with `GB_ROM_UPLOAD_CHUNK_BYTES`).
  Use `begin_gb_rom_upload` → `append_gb_rom_upload_batch` →
  `finalize_gb_rom_upload(..., email, boot=true)`. Do not ingest from a
  host filesystem path.
- **Screenshot-only play contract is unchanged.** There is no memory or
  game-state tool.
