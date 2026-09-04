"""SQLite mapping between user emails and ROM subdirectory names.

SQLAlchemy owns the User <-> Subdirectory relationship so inserts go through
the ORM instead of ad-hoc SQL.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, create_engine, event, func, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "user_subdirectories.sqlite3"
SUBDIRECTORY_NAME_LENGTH = 32
# Practical RFC 5321 bound: 64-octet local-part + "@" + 255-octet domain.
MAX_EMAIL_CHARS = 320
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

_initialized = False


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Base(DeclarativeBase):
    pass


class User(Base):
    """A human user of the LLM, identified by email."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(MAX_EMAIL_CHARS), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    subdirectories: Mapped[list["Subdirectory"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, email={self.email!r})"


class Subdirectory(Base):
    """A 32-character roms/ subdirectory owned by one User."""

    __tablename__ = "subdirectories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(SUBDIRECTORY_NAME_LENGTH), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    user: Mapped[User] = relationship(back_populates="subdirectories")

    def __repr__(self) -> str:
        return f"Subdirectory(id={self.id!r}, name={self.name!r}, user_id={self.user_id!r})"


def init_db() -> None:
    """Create tables if needed. Safe to call more than once."""
    global _initialized
    Base.metadata.create_all(engine)
    _initialized = True


def normalize_email(email: str) -> str:
    """Strip, lowercase, and validate an email so the ORM stores one canonical form."""
    value = email.strip().lower()
    if not value:
        raise ValueError("email address is required")
    if len(value) > MAX_EMAIL_CHARS:
        raise ValueError(f"email address exceeds {MAX_EMAIL_CHARS} characters")
    if not _EMAIL_RE.fullmatch(value):
        raise ValueError("invalid email address")
    return value


@contextmanager
def session_scope() -> Iterator[Session]:
    if not _initialized:
        init_db()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def new_subdirectory_name() -> str:
    """Return a filesystem-safe 32-character subdirectory name."""
    return secrets.token_hex(SUBDIRECTORY_NAME_LENGTH // 2)


def get_or_create_user(session: Session, email: str) -> User:
    email = normalize_email(email)
    user = session.scalar(select(User).where(User.email == email))
    if user is not None:
        return user
    user = User(email=email)
    session.add(user)
    session.flush()
    return user


def subdirectory_exists(session: Session, name: str) -> bool:
    return session.scalar(select(Subdirectory.id).where(Subdirectory.name == name)) is not None


def map_subdirectory_to_email(session: Session, name: str, email: str) -> Subdirectory:
    """Persist the User <-> Subdirectory relationship; create the user if needed.

    Raises:
        ValueError: If `name` is already mapped to a different email.
    """
    name = name.strip().lower()
    if len(name) != SUBDIRECTORY_NAME_LENGTH:
        raise ValueError(
            f"subdirectory name must be {SUBDIRECTORY_NAME_LENGTH} characters, got {len(name)}"
        )
    email = normalize_email(email)

    existing = session.scalar(select(Subdirectory).where(Subdirectory.name == name))
    user = get_or_create_user(session, email)
    if existing is not None:
        if existing.user_id != user.id:
            raise ValueError(
                f"subdirectory {name!r} is already mapped to a different email address"
            )
        return existing

    subdirectory = Subdirectory(name=name, user=user)
    session.add(subdirectory)
    session.flush()
    return subdirectory


def list_subdirectories_for_email(session: Session, email: str) -> list[Subdirectory]:
    """Return ROM subdirectories mapped to `email`, oldest first.

    Unknown emails yield an empty list; this does not create a user.
    """
    email = normalize_email(email)
    stmt = (
        select(Subdirectory)
        .join(User)
        .where(User.email == email)
        .order_by(Subdirectory.created_at.asc(), Subdirectory.id.asc())
    )
    return list(session.scalars(stmt).all())


def get_subdirectory_for_email(session: Session, name: str, email: str) -> Subdirectory | None:
    """Return the subdirectory if it is mapped to `email`, else None.

    Does not create a user or mapping. Unknown emails and names yield None.
    """
    name = name.strip().lower()
    email = normalize_email(email)
    stmt = (
        select(Subdirectory)
        .join(User)
        .where(Subdirectory.name == name, User.email == email)
    )
    return session.scalar(stmt)
