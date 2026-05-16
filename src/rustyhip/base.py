"""Django DB backend — subclasses django.db.backends.sqlite3 but swaps the
sqlite3 Python module for a thin HTTP client that POSTs to rustyhip's /sql.

Django's SQLite dialect, schema editor, introspection, and features stay
intact; only the connection + cursor layer is overridden.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.error
import urllib.request
from sqlite3 import (
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    Warning,
)
from typing import TYPE_CHECKING, Any

from django.core.serializers.json import DjangoJSONEncoder
from django.db.backends.sqlite3 import base as sqlite3_base
from django.db.backends.sqlite3.base import FORMAT_QMARK_REGEX

from .creation import DatabaseCreation
from .features import DatabaseFeatures
from .operations import DatabaseOperations

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

logger = logging.getLogger("rustyhip")

# Matches a full PRAGMA statement. Non-greedy on the value so trailing
# semicolons / whitespace don't get captured; callers only need the name
# and whether a `=` assignment was present.
_PRAGMA_RE = re.compile(
    r"^\s*PRAGMA\s+(?P<name>\w+)\s*(?:=\s*(?P<value>.+?))?\s*;?\s*$",
    re.IGNORECASE,
)

# SQL statements that our server-side connection can't honor (no cross-call
# transaction state — each /sql call opens a fresh SQLite connection). We
# match on the first keyword and swallow them at the client.
_TRANSACTION_KEYWORDS = (
    "BEGIN",
    "COMMIT",
    "END",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
)

# `Connection.getlimit(limit_id)` values from the SQLite C API — stdlib's
# ``sqlite3`` module doesn't re-export them as named constants.
_SQLITE_LIMIT_VARIABLE_NUMBER = 9


class Database:
    """Stand-in for Python's ``sqlite3`` module. Django calls
    ``DatabaseWrapper.Database.connect(...)`` to build a connection — we return
    an HTTP-backed one instead.

    Exception classes and PARSE_* constants are re-exported from the stdlib
    ``sqlite3`` module so Django's DB-API expectations line up exactly.
    """

    Error = Error
    Warning = Warning
    InterfaceError = InterfaceError
    DatabaseError = DatabaseError
    DataError = DataError
    OperationalError = OperationalError
    IntegrityError = IntegrityError
    InternalError = InternalError
    ProgrammingError = ProgrammingError
    NotSupportedError = NotSupportedError

    PARSE_DECLTYPES = sqlite3.PARSE_DECLTYPES
    PARSE_COLNAMES = sqlite3.PARSE_COLNAMES

    apilevel = "2.0"
    threadsafety = 1
    paramstyle = "qmark"

    @staticmethod
    def connect(endpoint: str, timeout: float = 30.0, auth_token: str = "", **_: Any) -> RustyhipConnection:
        return RustyhipConnection(endpoint=endpoint, timeout=timeout, auth_token=auth_token)

    @staticmethod
    def register_converter(*_: Any, **__: Any) -> None:
        """No-op — we don't route values through Python's sqlite3 adapter layer."""

    @staticmethod
    def register_adapter(*_: Any, **__: Any) -> None:
        """No-op."""

    @staticmethod
    def enable_callback_tracebacks(*_: Any, **__: Any) -> None:
        """No-op."""


class RustyhipConnection:
    """Mimics just enough of sqlite3.Connection for Django's SQLite backend."""

    def __init__(self, endpoint: str, timeout: float = 30.0, auth_token: str = "") -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.auth_token = auth_token
        self.closed = False
        # Django sets isolation_level to None to opt into manual tx mode. We
        # honor reads/writes of the attribute but don't act on it.
        self.isolation_level: str | None = None
        self.in_transaction = False
        self.row_factory: Any = None

    def cursor(self, factory: Any = None) -> RustyhipCursor:
        return RustyhipCursor(self)

    # Convenience passthrough used by Django for a couple of pragmas at connect time.
    def execute(self, sql: str, params: Iterable[Any] | None = None) -> RustyhipCursor:
        cur = self.cursor()
        cur.execute(sql, params or ())
        return cur

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> RustyhipCursor:
        cur = self.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def create_function(self, name: str, narg: int, func: Any, **_: Any) -> None:
        """No-op. Django registers a handful of SQLite UDFs via this method
        (``django_date_extract``, ``django_timestamp_diff``, etc.). Our
        server-side SQLite doesn't have them; queries that use them will fail
        with "no such function" — acceptable for the migrate happy path.
        """

    def create_collation(self, *_: Any, **__: Any) -> None:
        """No-op."""

    def getlimit(self, limit_id: int) -> int:
        """Mimic ``sqlite3.Connection.getlimit``. Django calls this to size
        bulk-insert batches via ``SQLITE_LIMIT_VARIABLE_NUMBER`` (id=9). Return
        SQLite's compile-time default so Django picks a reasonable batch size.
        """
        # SQLITE_LIMIT_VARIABLE_NUMBER — default 999 pre-3.32, 32766 since.
        if limit_id == _SQLITE_LIMIT_VARIABLE_NUMBER:
            return 999
        return 0

    def commit(self) -> None:
        """Swallowed — each /sql call auto-commits server-side."""

    def rollback(self) -> None:
        """Swallowed — no cross-call transaction state on the server."""

    def close(self) -> None:
        self.closed = True

    # Context manager protocol (sqlite3.Connection is one).
    def __enter__(self) -> RustyhipConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Mirror sqlite3 behavior: commit on success, rollback on error. Both are no-ops.
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


class RustyhipCursor:
    """DB-API 2.0 cursor backed by rustyhip's ``POST /sql`` endpoint."""

    arraysize = 1

    def __init__(self, conn: RustyhipConnection) -> None:
        self.conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self._row_iter: Iterator[tuple[Any, ...]] | None = None
        self.rowcount: int = -1
        self.lastrowid: int | None = None
        self.description: list[tuple[str, None, None, None, None, None, None]] | None = None

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> RustyhipCursor:
        if _is_transaction_stmt(sql):
            # Silently succeed — we fake client-side transaction management.
            self._reset_result()
            return self
        if self._intercept_pragma(sql):
            return self
        if params is not None:
            sql = _convert_format_to_qmark(sql)
        payload = _build_payload(sql, params)
        response = self._post(payload)
        self._ingest(response)
        return self

    def _intercept_pragma(self, sql: str) -> bool:
        """Handle PRAGMAs client-side where turbolite or our HTTP model can't
        honor them correctly. Most PRAGMAs (including setters like
        ``PRAGMA foreign_keys = OFF``) pass through — rustyhip holds a
        long-lived connection on the server, so setter state persists across
        /sql calls.

        Returns True if the pragma was handled locally; False if the caller
        should fall through to the HTTP POST.
        """
        m = _PRAGMA_RE.match(sql)
        if not m:
            return False
        name = m.group("name").lower()
        if name == "foreign_key_check":
            # Report no violations. Django's schema editor calls this at the
            # end of every migration; SQLite's answer is a row per broken FK.
            # We're not enforcing FK across connections, so an empty result
            # is the honest answer.
            self._rows = []
            self._row_iter = iter(self._rows)
            self.description = [
                ("table", None, None, None, None, None, None),
                ("rowid", None, None, None, None, None, None),
                ("parent", None, None, None, None, None, None),
                ("fkid", None, None, None, None, None, None),
            ]
            self.rowcount = 0
            self.lastrowid = None
            return True
        # Everything else — setters and readers — pass through to rustyhip.
        return False

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> RustyhipCursor:
        total_rowcount = 0
        for params in seq_of_params:
            self.execute(sql, params)
            if self.rowcount > 0:
                total_rowcount += self.rowcount
        # DB-API says rowcount on executemany is the sum of affected rows.
        self.rowcount = total_rowcount
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._row_iter is None:
            return None
        return next(self._row_iter, None)

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        if self._row_iter is None:
            return []
        count = size or self.arraysize
        out: list[tuple[Any, ...]] = []
        for _ in range(count):
            row = next(self._row_iter, None)
            if row is None:
                break
            out.append(row)
        return out

    def fetchall(self) -> list[tuple[Any, ...]]:
        if self._row_iter is None:
            return []
        return list(self._row_iter)

    def close(self) -> None:
        self._row_iter = None
        self._rows = []

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(self._rows)

    # ----- internals -----

    def _reset_result(self) -> None:
        self._rows = []
        self._row_iter = iter(())
        self.rowcount = -1
        self.lastrowid = None
        self.description = None

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, cls=_RustyhipJSONEncoder).encode("utf-8")
        headers: dict[str, str] = {"content-type": "application/json"}
        if self.conn.auth_token:
            headers["authorization"] = f"Bearer {self.conn.auth_token}"
        req = urllib.request.Request(  # noqa: S310  (endpoint is operator-configured)
            f"{self.conn.endpoint}/sql",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.conn.timeout) as resp:  # noqa: S310
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # 4xx/5xx — rustyhip returns JSON error bodies for 400s.
            raw = e.read() if hasattr(e, "read") else b""
            status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OperationalError(f"rustyhip POST failed: {e}") from e

        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as e:
            # Cap the offending body in the exception so a 500 HTML error page
            # doesn't blow up the log / traceback with megabytes of output.
            snippet = raw[:500]
            raise OperationalError(
                f"rustyhip returned non-JSON body (status={status}, first {len(snippet)} bytes): {snippet!r}"
            ) from e

        if status >= 400:
            _raise_for_sql_error(status, data)
        return data

    def _ingest(self, data: dict[str, Any]) -> None:
        columns = data.get("columns") or []
        rows_raw = data.get("rows") or []
        self._rows = [tuple(row.get(col) for col in columns) for row in rows_raw]
        self._row_iter = iter(self._rows)
        self.description = [(name, None, None, None, None, None, None) for name in columns] if columns else None
        # Default to `readonly = False` when the server omits the flag — a
        # write that loses its `rowcount` silently is worse than a SELECT that
        # reports 0 instead of len(rows) (len is typically 0 on writes anyway).
        if data.get("readonly", False):
            self.rowcount = len(self._rows)
        else:
            self.rowcount = int(data.get("rowcount") or 0)
        lastrowid = data.get("lastrowid")
        # SQLite rowids are always positive integers; `0` means "no insert ran
        # yet" and is surfaced as None so callers don't key off a fake rowid.
        self.lastrowid = int(lastrowid) if lastrowid else None


def _is_transaction_stmt(sql: str) -> bool:
    # Strip leading whitespace + leading "--" line comments, then look at the first word.
    cursor = 0
    n = len(sql)
    while cursor < n:
        ch = sql[cursor]
        if ch.isspace():
            cursor += 1
            continue
        if ch == "-" and cursor + 1 < n and sql[cursor + 1] == "-":
            end = sql.find("\n", cursor)
            cursor = n if end == -1 else end + 1
            continue
        break
    first_word = sql[cursor:].split(None, 1)[0].upper() if cursor < n else ""
    return first_word in _TRANSACTION_KEYWORDS


def _build_payload(sql: str, params: Iterable[Any] | None) -> dict[str, Any]:
    return {"sql": sql, "params": list(params) if params is not None else []}


def _convert_format_to_qmark(sql: str) -> str:
    """Mirror ``django.db.backends.sqlite3.base.SQLiteCursorWrapper.convert_query``.

    Django's ORM compiles SQL using ``%s`` positional markers; Python's
    sqlite3 module uses ``?``. The built-in SQLite cursor wrapper rewrites
    placeholders at execute time; we reuse its exact regex for parity.
    """
    return FORMAT_QMARK_REGEX.sub("?", sql).replace("%%", "%")


class _RustyhipJSONEncoder(DjangoJSONEncoder):
    """``DjangoJSONEncoder`` handles ``datetime``, ``date``, ``time``,
    ``timedelta``, ``Decimal``, ``UUID``, and ``Promise`` natively. We only
    need to extend it with an explicit rejection of BLOB-style values —
    rustyhip doesn't round-trip bytes through its JSON wire format yet.
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, (bytes, bytearray, memoryview)):
            raise NotSupportedError("rustyhip backend does not yet support BLOB parameters")
        return super().default(o)


# Rustyhip's server-side codes (src/errors.rs) — keep in sync with the Rust side.
_SERVER_ERROR_UNAUTHORIZED = "RUSTYHIP_E_UNAUTHORIZED"
_SERVER_ERROR_VALIDATION = "RUSTYHIP_E_VALIDATION"
_SERVER_ERROR_NOT_FOUND = "RUSTYHIP_E_NOT_FOUND"
_SERVER_ERROR_SQL = "RUSTYHIP_E_SQL"
_SERVER_ERROR_INTERNAL = "RUSTYHIP_E_INTERNAL"


def _raise_for_sql_error(status: int, data: Any) -> None:
    """Map a rustyhip error response to the appropriate DB-API exception.

    Rustyhip returns ``{"error": {"code": "RUSTYHIP_E_*", "message": "...",
    "request_id": "..."}}`` on every non-2xx. The code tells us the category;
    for the catch-all ``RUSTYHIP_E_SQL`` we fall back to message-substring
    matching to distinguish ``IntegrityError`` (unique / FK violations) from
    ``ProgrammingError`` (syntax / missing tables).
    """
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        code = err.get("code")
        msg = err.get("message") or ""
    else:
        code = None
        msg = str(err or data or f"rustyhip returned HTTP {status}")

    if code == _SERVER_ERROR_UNAUTHORIZED:
        raise ProgrammingError(f"rustyhip auth rejected: {msg}")
    if code == _SERVER_ERROR_VALIDATION:
        raise OperationalError(msg)
    if code == _SERVER_ERROR_NOT_FOUND:
        raise ProgrammingError(msg)
    if code == _SERVER_ERROR_INTERNAL:
        raise OperationalError(msg)
    if code == _SERVER_ERROR_SQL:
        lowered = msg.lower()
        if "unique" in lowered or "constraint" in lowered:
            raise IntegrityError(msg)
        if "syntax" in lowered or "no such" in lowered:
            raise ProgrammingError(msg)
        raise OperationalError(msg)
    # Old servers or unexpected shapes — preserve the status for callers.
    raise DatabaseError(f"HTTP {status}: {msg}")


class DatabaseWrapper(sqlite3_base.DatabaseWrapper):
    """Connection wrapper. Inherits Django's SQLite schema editor / introspection /
    operations, but replaces the sqlite3 module with our HTTP shim.
    """

    Database = Database  # Django uses this for exception hierarchy lookups.
    vendor = "sqlite"
    display_name = "Rustyhip"
    features_class = DatabaseFeatures
    creation_class = DatabaseCreation
    ops_class = DatabaseOperations

    def get_connection_params(self) -> dict[str, Any]:
        conf = self.settings_dict
        options = conf.get("OPTIONS") or {}
        endpoint = conf.get("HOST") or options.get("endpoint") or options.get("ENDPOINT")
        if not endpoint:
            raise ImproperlyConfigured(
                "django-rustyhip requires DATABASES[...]['HOST'] set to the rustyhip endpoint URL (e.g. 'http://localhost:9000')."
            )
        return {
            "endpoint": endpoint,
            "timeout": float(options.get("timeout", 30.0)),
            "auth_token": conf.get("PASSWORD") or "",
        }

    def get_new_connection(self, conn_params: dict[str, Any]) -> RustyhipConnection:
        return self.Database.connect(**conn_params)

    def init_connection_state(self) -> None:
        """Django's SQLite backend runs pragmas here. Our remote server opens
        a fresh SQLite connection per /sql call, so per-connection pragmas
        (like ``PRAGMA foreign_keys=ON``) wouldn't stick. Skip them.
        """
        return None

    def create_cursor(self, name: str | None = None) -> RustyhipCursor:
        # Django guarantees ``self.connection`` is set by the time `create_cursor`
        # is called (it goes through `ensure_connection` upstream). The type
        # hint on the base class is `Optional`, hence the explicit assert.
        assert self.connection is not None
        return self.connection.cursor()

    def _set_autocommit(self, autocommit: bool) -> None:
        # Every /sql call is auto-committed server-side; nothing to toggle.
        self.autocommit = autocommit

    def is_usable(self) -> bool:
        return not self.connection.closed if self.connection is not None else False

    def _start_transaction_under_autocommit(self) -> None:
        """Called by Django when it wants to begin a manual transaction in
        autocommit mode. No-op for us — transactions are faked client-side.
        """


# Imported late to avoid a circular import with django.conf during module init.
from django.core.exceptions import ImproperlyConfigured  # noqa: E402
