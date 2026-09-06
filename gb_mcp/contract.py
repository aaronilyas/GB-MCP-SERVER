"""Model-facing how-to for Game Boy MCP play.

Later used as MCP ``instructions`` and ``gb://how-to-play``.
"""

from __future__ import annotations

HOW_TO_PLAY_MAX_CHARS = 2000

HOW_TO_PLAY = """\
Game Boy MCP.
Loop: list_games → boot → play → look at the returned image or video → repeat.
Tools: list_games, boot, play, save, stop, add_rom.
list_games lists your titles.
boot starts a session by title or id. Default restores the last snapshot.
reset=true cold-boots and drops the snapshot.
After boot, play is the controller: buttons plus optional frames, gap, mash, steps, until, media.
play takes no email or subdirectory.
Each play returns one PNG (4×) or one short GIF. Look at it, then play again.
until vocab: battle | textbox | menu | stable | fade.
One live session per user.
save writes a snapshot and leaves the session running.
stop and idle auto-save, then close.
Large ROM dumps use HTTP POST /roms, not chat chunks.
Small homebrew may use add_rom with rom_base64.
"""
