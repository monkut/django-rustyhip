"""Structural smoke tests — verify the backend package imports cleanly and
its classes wire up the way Django expects.

HTTP path + Django-test-runner interop are covered by the consumer project's
test suite running against a live rustyhip; those paths aren't reproduced
here because spinning up a Lambda runtime in this repo's CI is out of scope.
"""

from __future__ import annotations

import sqlite3

import pytest


def test_package_exports_database_wrapper() -> None:
    from rustyhip import DatabaseWrapper

    assert DatabaseWrapper.__name__ == "DatabaseWrapper"


def test_exception_classes_are_the_stdlib_sqlite3_classes() -> None:
    """Consumers catching ``sqlite3.OperationalError`` should see our exceptions."""
    from rustyhip.base import Database

    assert Database.Error is sqlite3.Error
    assert Database.OperationalError is sqlite3.OperationalError
    assert Database.IntegrityError is sqlite3.IntegrityError
    assert Database.PARSE_DECLTYPES == sqlite3.PARSE_DECLTYPES
    assert Database.PARSE_COLNAMES == sqlite3.PARSE_COLNAMES


def test_format_qmark_regex_comes_from_django() -> None:
    from django.db.backends.sqlite3.base import FORMAT_QMARK_REGEX as django_re

    from rustyhip.base import FORMAT_QMARK_REGEX as rustyhip_re

    assert rustyhip_re is django_re


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("BEGIN", True),
        ("  begin ", True),
        ("COMMIT", True),
        ("ROLLBACK TO SAVEPOINT foo", True),
        ("SAVEPOINT s1", True),
        ("RELEASE s1", True),
        ("-- comment\nBEGIN", True),
        ("SELECT 1", False),
        ("INSERT INTO t VALUES (1)", False),
        ("CREATE TABLE t (x INT)", False),
        ("", False),
    ],
)
def test_transaction_statement_detection(sql: str, expected: bool) -> None:
    from rustyhip.base import _is_transaction_stmt

    assert _is_transaction_stmt(sql) is expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT %s", "SELECT ?"),
        ("UPDATE t SET a = %s WHERE b = %s", "UPDATE t SET a = ? WHERE b = ?"),
        ("SELECT '%%s'", "SELECT '%s'"),
    ],
)
def test_placeholder_rewrite_matches_django(sql: str, expected: str) -> None:
    from rustyhip.base import _convert_format_to_qmark

    assert _convert_format_to_qmark(sql) == expected


def test_pragma_regex_matches_setter_and_getter_forms() -> None:
    from rustyhip.base import _PRAGMA_RE

    setter = _PRAGMA_RE.match("PRAGMA foreign_keys = OFF")
    assert setter is not None
    assert setter.group("name").lower() == "foreign_keys"
    assert setter.group("value") == "OFF"

    getter = _PRAGMA_RE.match("PRAGMA foreign_keys")
    assert getter is not None
    assert getter.group("name").lower() == "foreign_keys"
    assert getter.group("value") is None


def test_connection_creates_cursor_and_getlimit() -> None:
    from rustyhip.base import RustyhipConnection, RustyhipCursor

    conn = RustyhipConnection(endpoint="http://example.invalid", timeout=1.0)
    cur = conn.cursor()
    assert isinstance(cur, RustyhipCursor)
    assert conn.getlimit(9) == 999
    assert conn.getlimit(0) == 0


def test_database_wrapper_uses_our_subclasses() -> None:
    from rustyhip.base import (
        Database,
        DatabaseCreation,
        DatabaseFeatures,
        DatabaseOperations,
        DatabaseWrapper,
    )

    assert DatabaseWrapper.Database is Database
    assert DatabaseWrapper.features_class is DatabaseFeatures
    assert DatabaseWrapper.creation_class is DatabaseCreation
    assert DatabaseWrapper.ops_class is DatabaseOperations


def test_features_disable_transactions_and_in_memory_db() -> None:
    from rustyhip.features import DatabaseFeatures

    assert DatabaseFeatures.supports_transactions is False
    assert DatabaseFeatures.can_share_in_memory_db is False
    assert DatabaseFeatures.uses_savepoints is False
    assert DatabaseFeatures.atomic_transactions is False


def _make_cursor():
    from rustyhip.base import RustyhipConnection

    return RustyhipConnection(endpoint="http://example.invalid", timeout=1.0).cursor()


def test_build_payload_requests_positional_rows() -> None:
    """monkut/rustyhip#29: objects-format rows collapse duplicate column names."""
    from rustyhip.base import _build_payload

    payload = _build_payload("SELECT 1", None)
    assert payload["rows_format"] == "arrays"
    assert payload["params"] == []


def test_ingest_positional_rows_preserve_duplicate_column_names() -> None:
    """monkut/rustyhip#29: `SELECT p.id, k.id FROM ... JOIN ...` must yield both values."""
    cur = _make_cursor()
    cur._ingest({"columns": ["id", "id"], "rows": [[1, 7]], "readonly": True})
    assert cur._rows == [(1, 7)]
    assert cur.description is not None
    assert [d[0] for d in cur.description] == ["id", "id"]


def test_ingest_falls_back_to_name_keyed_object_rows() -> None:
    """Older rustyhip servers ignore `rows_format` and send objects."""
    cur = _make_cursor()
    cur._ingest({"columns": ["a", "b"], "rows": [{"a": 1, "b": 2}], "readonly": True})
    assert cur._rows == [(1, 2)]
