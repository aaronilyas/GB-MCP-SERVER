from __future__ import annotations

import pytest

import db


def test_normalize_email_strips_and_lowercases() -> None:
    assert db.normalize_email("  User@Example.COM ") == "user@example.com"


@pytest.mark.parametrize(
    "value",
    ["", "   ", "not-an-email", "a" * 321 + "@x.com", "user@", "@example.com", "user@example"],
)
def test_normalize_email_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        db.normalize_email(value)


def test_get_or_create_user_is_idempotent(isolated_db) -> None:
    with db.session_scope() as session:
        first = db.get_or_create_user(session, "a@example.com")
        second = db.get_or_create_user(session, "A@Example.com")
        assert first.id == second.id
        assert first.email == "a@example.com"


def test_map_subdirectory_to_email_creates_user(isolated_db) -> None:
    name = "a" * db.SUBDIRECTORY_NAME_LENGTH
    with db.session_scope() as session:
        mapped = db.map_subdirectory_to_email(session, name, "owner@example.com")
        assert mapped.name == name
        assert mapped.user.email == "owner@example.com"


def test_map_subdirectory_to_email_is_idempotent_for_same_user(isolated_db) -> None:
    name = "b" * db.SUBDIRECTORY_NAME_LENGTH
    with db.session_scope() as session:
        first = db.map_subdirectory_to_email(session, name, "owner@example.com")
        first_id = first.id
    with db.session_scope() as session:
        second = db.map_subdirectory_to_email(session, name, "owner@example.com")
        assert second.id == first_id


def test_map_subdirectory_refuses_different_email(isolated_db) -> None:
    name = "c" * db.SUBDIRECTORY_NAME_LENGTH
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, name, "one@example.com")
    with db.session_scope() as session:
        with pytest.raises(ValueError, match="already mapped"):
            db.map_subdirectory_to_email(session, name, "two@example.com")


def test_map_rejects_wrong_name_length(isolated_db) -> None:
    with db.session_scope() as session:
        with pytest.raises(ValueError, match="must be 32 characters"):
            db.map_subdirectory_to_email(session, "abc", "a@example.com")


def test_list_unknown_email_is_empty(isolated_db) -> None:
    with db.session_scope() as session:
        assert db.list_subdirectories_for_email(session, "nobody@example.com") == []


def test_list_returns_oldest_first(isolated_db) -> None:
    names = ["d" * db.SUBDIRECTORY_NAME_LENGTH, "e" * db.SUBDIRECTORY_NAME_LENGTH]
    with db.session_scope() as session:
        db.map_subdirectory_to_email(session, names[0], "owner@example.com")
        db.map_subdirectory_to_email(session, names[1], "owner@example.com")
    with db.session_scope() as session:
        rows = db.list_subdirectories_for_email(session, "owner@example.com")
        assert [row.name for row in rows] == names


def test_subdirectory_exists(isolated_db) -> None:
    name = "f" * db.SUBDIRECTORY_NAME_LENGTH
    with db.session_scope() as session:
        assert db.subdirectory_exists(session, name) is False
        db.map_subdirectory_to_email(session, name, "owner@example.com")
        assert db.subdirectory_exists(session, name) is True


def test_new_subdirectory_name_is_32_hex() -> None:
    name = db.new_subdirectory_name()
    assert len(name) == db.SUBDIRECTORY_NAME_LENGTH
    assert all(c in "0123456789abcdef" for c in name)


def test_get_subdirectory_for_email_requires_mapping(isolated_db) -> None:
    name = "g" * db.SUBDIRECTORY_NAME_LENGTH
    with db.session_scope() as session:
        assert db.get_subdirectory_for_email(session, name, "owner@example.com") is None
        db.map_subdirectory_to_email(session, name, "owner@example.com")
    with db.session_scope() as session:
        row = db.get_subdirectory_for_email(session, name, "Owner@Example.com")
        assert row is not None
        assert row.name == name
        assert db.get_subdirectory_for_email(session, name, "other@example.com") is None
