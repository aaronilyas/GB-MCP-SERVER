from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import db
from gb_mcp import config

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


@pytest.fixture(scope="session")
def validator_module():
    spec = importlib.util.spec_from_file_location("validate_gb_rom", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
