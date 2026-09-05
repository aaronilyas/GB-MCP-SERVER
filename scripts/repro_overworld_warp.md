# Overworld warp repro (local only)

This is an operator script, **not** an MCP tool. It does not peek WRAM, map
id, or player X/Y, and it must not grow a memory / game-state tool on the
server. Local stdout (hashes, classifiers, paths) is for your terminal only;
do not add those keys to `send_pyboy_input` responses.

Do **not** commit `.gb`, `.gbc`, `.state`, or `.sav` (or `.ram`). Point
`--rom` at a dump that already lives on disk.

The script boots PyBoy the same way as
`gb_mcp.emulator.loop._default_pyboy_factory`:

- `window=null` (`GB_PYBOY_WINDOW`, default `null`)
- `sound_emulated=False`
- `no_input=True`

It never writes a new `.state` / battery file (`pyboy.stop(save=False)`).

## Setup

From the repo root, use the instance venv (PyBoy + numpy + Pillow):

```bash
cd /path/to/GB-MCP-SERVER
.venv/bin/python scripts/repro_overworld_warp.py --help
```

Capture a PyBoy save-state **one step in front of a warp** (house door,
building door, or map-edge connection):

1. `load_subdirectory_rom` on the mapped dump.
2. Walk until the sprite is on the tile *south of* a north-facing door
   (or on the last in-bounds tile before a route connection).
3. `save_battery` or `stop_pyboy`. Both write `<rom>.state` next to the ROM
   (same path `EmulatorSession._run` restores).

`--mode restore` and `--mode engine` load that sibling `.state`. `--mode cold`
ignores it. PyBoy may still auto-load a sibling `.ram` cartridge battery on
every mode; cold is not a “blank new game” if a battery file is present.

## Exact commands

Replace `ROM` with the absolute path to your dump. Default walk is **hold Up
for 360 frames** (~15 Gen 1 steps), which is enough to step onto a door and
finish a fade if the warp fires.

### restore — production boot, direct `_tick_chunk` walk

```bash
.venv/bin/python scripts/repro_overworld_warp.py \
  --rom ROM \
  --mode restore \
  --direction up \
  --frames 360
```

Loads `ROM.state` the same way as `EmulatorSession._run` (nonempty file →
`load_state`; on failure `restored_state=False` and `restore_error=...`).
Holds the d-pad with `button_press` and advances with
`input_engine._tick_chunk` (render only on `until_eval_interval` boundaries
and the last frame; default interval 4).

### engine — production boot, buttons only through `run_play_input`

```bash
.venv/bin/python scripts/repro_overworld_warp.py \
  --rom ROM \
  --mode engine \
  --direction up \
  --frames 360
```

Same restore as above. The walk is a `macro=hold` payload into
`run_play_input` (`disable_default_hold_abort=true` so a fade does not abort
the repro). Compare this PNG set to `restore`: if they diverge, the extra
scheduler around `_tick_chunk` is in play.

### cold — ignore `.state`

```bash
.venv/bin/python scripts/repro_overworld_warp.py \
  --rom ROM \
  --mode cold \
  --direction up \
  --frames 360
```

Does **not** call `load_state`. You should see the title / intro (or a
battery-save spawn, if `.ram` exists), not the door you captured. This mode
answers “is the glitch save-state specific?”, not “does this door warp?”.

### Optional: render every frame

If restore/engine walk through the door at interval 4, rerun with interval 1
(every tick `render=True`):

```bash
.venv/bin/python scripts/repro_overworld_warp.py \
  --rom ROM \
  --mode restore \
  --direction up \
  --frames 360 \
  --until-eval-interval 1
```

Same command with `--mode engine`. If interval 1 warps and interval 4 does
not, the failure tracks `tick(n, render=False)` on intermediate frames.

Other useful flags:

| Flag | Default | Notes |
| --- | --- | --- |
| `--state PATH` | `<rom>.state` | Override the `_run` sibling path |
| `--out DIR` | `/tmp/gb-warp-repro` | PNG directory |
| `--emulation-speed` | `0` | Uncapped, same as play sessions |
| `--screenshot-scale` | `4` | 640×576 nearest-neighbor, same as MCP |
| `--call-timeout-seconds` | `70` | Wall-clock cap |

Doors entered from the south use `--direction up`. A west-facing map edge
uses `--direction left`, and so on.

## Stdout

The first two lines are the contract:

```text
frames_advanced=360
restored_state=True
```

`frames_advanced` is the number of emulator frames actually ticked.
`restored_state` is True only when `load_state` succeeded (so **cold is
always False**, even if a `.state` sits next to the ROM).

Further lines are local debug (`mode`, `png=...`, framebuffer
`region_hashes_*`, `classifiers_after`). They are **not** MCP response
fields.

PNGs (scale 4 unless you change it):

```text
/tmp/gb-warp-repro/<mode>_<direction>_before.png
/tmp/gb-warp-repro/<mode>_<direction>_0090.png   # 25/50/75% on render points (direct walk)
/tmp/gb-warp-repro/<mode>_<direction>_after.png
/tmp/gb-warp-repro/<mode>_<direction>_engine_00.png  # engine packed final only
```

Open `*_before.png` and `*_after.png` for the verdict.

## What “warp worked” looks like

You started on a **legal** overworld tile, facing the warp, one step away.

**Door / staircase (Pallet house, Poké Center, gym, cave mouth):**

- Before: outdoor (or previous room) tileset; door graphic in front of the
  sprite; HUD is the usual overworld (no battle bars, no start menu).
- During: a full-screen fade (white or black) or a distinct interior
  tileset taking over the LCD.
- After: a **different room**. Gen 1 houses show a doormat, floor, furniture,
  and the player standing on the mat. A Poké Center shows the counter and
  healing machine. The camera is locked to that interior; you are not still
  looking at the previous map’s roof / wall / route tiles.

**Map-edge connection (Pallet Town north → Route 1, and similar):**

- Before: the last row/column of the current map, with the route’s edge
  already visible or about to scroll in.
- After: the **next map’s** tileset and layout (Route 1 grass and ledges
  instead of Pallet’s houses). The player is on the first in-bounds row of
  the new map. Scroll is sane: no wrapping strip of the previous map.

`region_hashes_before` and `region_hashes_after` should differ. A
`classifiers_after` with `battle_likely=true` is a **wild encounter**, not a
warp — you walked into grass; pick a door tile and recapture `.state`.

## What “walked through tile / OOB camera” looks like

The warp script never ran (or ran against a broken LCD/scroll state). The
player is treated as if the door / edge were a walkable floor tile.

**Walked through the warp tile:**

- After: still the **same map**. The sprite is on top of the door graphic,
  inside the building’s roof tiles, or past the map’s painted edge.
- No fade, or a flicker that dumps you back onto the same overworld.
- Interiors never load: you do not see the doormat / counter / cave floor.

**OOB camera (scroll / SCX-SCY garbage):**

- Background tiles **repeat, wrap, or turn into the wrong tileset** (stripes,
  garbage patterns, a strip of HUD tiles in the playfield).
- The player sprite detaches from the floor grid: walking “on” walls, floating
  in a repeating void, or sliding while the camera crawls through uninitialized
  tilemap.
- The 160×144 window is no longer a coherent room or route. This is the
  usual look when Gen 1 keeps walking after a failed door/edge warp and the
  camera follows into unmapped tilemap.

If `*_after.png` still shows a clean Pallet (or whatever) street **and** the
sprite has passed the door pixel, that is “walked through tile”. If the
background has become junk / wrapped, that is “OOB camera”. Either one is a
failed warp.

## Reading the three modes

| Mode | Loads `<rom>.state` | Walk |
| --- | --- | --- |
| `cold` | no | `_tick_chunk` directly |
| `restore` | yes, like `_run` | `_tick_chunk` directly |
| `engine` | yes, like `_run` | `run_play_input` only |

- **restore and engine both fail, interval 1 works:** intermediate
  `tick(n, render=False)` is the suspect (LCD / VBlank skipped).
- **restore works, engine fails:** something in `run_play_input` besides
  `_tick_chunk` (this script already disables default hold abort).
- **both work:** the warp is fine under this boot; the live MCP session
  differs (idle policy, a previous input, a stale `.state`).
- **cold looks like the title screen:** expected. Recapture `.state` on the
  door; do not use cold as the warp verdict.

## Out of scope

- No `map_id` / `player_x` / `player_y` / WRAM tool, including as a “debug”
  MCP field.
- No Pokémon dump in git. Keep ROMs under `roms/<32-hex>/` (already gitignored)
  or elsewhere outside the tree.
- Do not wire this script into `server.py` or the play instance.
