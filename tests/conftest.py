from __future__ import annotations

import importlib.util
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image as PILImage
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import db
from gb_mcp import config
from gb_mcp.emulator.session import SessionManager

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "docker" / "validate_gb_rom.py"


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point db.py at a throwaway SQLite file so tests never touch the real DB."""
    db_path = tmp_path / "user_subdirectories.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", SessionLocal)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "_initialized", False)
    db.init_db()
    yield


@pytest.fixture
def roms_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Store accepted ROMs under a temp tree; ROOT is that tree so relative paths work."""
    root = tmp_path / "host"
    roms = root / "roms"
    roms.mkdir(parents=True)
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(config, "ROMS_DIR", roms)
    return roms


class FakeScreen:
    """Minimal pyboy.screen stand-in with a unique PIL image per capture."""

    def __init__(self, emu: FakePyBoy) -> None:
        self._emu = emu

    @property
    def image(self) -> PILImage.Image:
        return self._emu._make_image()


class FakePyBoy:
    """Stand-in for PyBoy so session tests do not start a real emulator."""

    def __init__(self, rom_path: Path) -> None:
        self.gamerom = str(rom_path)
        self.cartridge_title = "TESTGAME"
        self.buttons: list[tuple[str, int]] = []
        self.tick_calls: list[int] = []
        self.captures: list[int] = []
        self.ticks = 0
        self.stopped = False
        self.saved_ram = False
        self.speed: int | None = None
        self.loaded_state: bytes | None = None
        self.screen = FakeScreen(self)
        self._pressed: set[str] = set()
        self._releases: list[list[object]] = []
        self._dead = threading.Event()

    def set_emulation_speed(self, speed: int) -> None:
        self.speed = speed

    def button(self, name: str, delay: int = 1) -> None:
        self.buttons.append((name, delay))
        self._pressed.add(name)
        self._releases.append([name, delay])

    def save_state(self, fh) -> None:
        fh.write(b"FAKESTATE")

    def load_state(self, fh) -> None:
        self.loaded_state = fh.read()

    def tick(self, count: int = 1, render: bool = True, sound: bool = True) -> bool:
        if self._dead.is_set():
            return False
        self.tick_calls.append(count)
        self._dead.wait(0.01)
        n = count if isinstance(count, int) and count > 0 else 0
        for _ in range(n):
            if self._dead.is_set():
                return False
            self._advance_buttons()
            self.ticks += 1
        return not self._dead.is_set()

    def stop(self, save: bool = True, ram_file=None, rtc_file=None) -> None:
        self.saved_ram = bool(save)
        self.stopped = True
        self._dead.set()

    def _advance_buttons(self) -> None:
        still: list[list[object]] = []
        for name, remaining in self._releases:
            if not isinstance(remaining, int) or remaining <= 0:
                self._pressed.discard(str(name))
                continue
            still.append([name, remaining - 1])
        self._releases = still

    def _make_image(self) -> PILImage.Image:
        # Unique per capture: ticks in RGB, plus a pixel for currently held buttons.
        color = (self.ticks & 255, (self.ticks >> 8) & 255, 80)
        image = PILImage.new("RGB", (160, 144), color=color)
        image.putpixel((0, 0), (self.ticks & 255, len(self._pressed) & 255, 1))
        self.captures.append(self.ticks)
        return image


@pytest.fixture
def pyboy_manager(monkeypatch: pytest.MonkeyPatch) -> Iterator[SessionManager]:
    """Install a FakePyBoy session manager for the duration of a test."""
    from gb_mcp.emulator import session as pyboy_sessions

    manager = SessionManager(pyboy_factory=FakePyBoy, idle_timeout_seconds=30)
    monkeypatch.setattr(pyboy_sessions, "manager", manager)
    yield manager
    manager.shutdown()


@pytest.fixture(scope="session")
def validator_module():
    spec = importlib.util.spec_from_file_location("validate_gb_rom", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
