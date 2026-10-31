"""Hard guard against writing outside the staging schema.

Three independent layers protect ``iris_core`` and every other production
schema; this module implements the first two.

1. **Restricted ``search_path``** — every connection is pinned to
   ``iris_staging, public``. ``public`` is included *only* so PostGIS
   functions (``ST_*``) resolve; no project table lives there.
2. **Statement guard** — a ``before_cursor_execute`` hook inspects every
   statement and raises :class:`StagingIsolationError` when a write targets
   anything that is not schema-qualified to ``iris_staging``.
3. **Least-privilege role** — ``sql/002_least_privilege_role.sql`` creates a
   loader role with no rights outside ``iris_staging`` (defence in depth at
   the database level, applied by a DBA).
"""

from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy import event
from sqlalchemy.engine import Engine

from ..logging_setup import get_logger

log = get_logger(__name__)


class StagingIsolationError(RuntimeError):
    """Raised when a statement would write outside the staging schema."""


# Statements that modify data or structure. SELECT/SET/SHOW are untouched.
_WRITE_PATTERNS = (
    re.compile(r"\bINSERT\s+INTO\s+(?P<target>[^\s(]+)", re.IGNORECASE),
    # (?!SET\b) keeps "... ON CONFLICT DO UPDATE SET ..." out of this pattern:
    # that clause updates the INSERT's own target, which is checked already.
    re.compile(r"\bUPDATE\s+(?!SET\b)(?:ONLY\s+)?(?P<target>[^\s(]+)", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\s+(?:ONLY\s+)?(?P<target>[^\s(]+)", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?(?P<target>[^\s(;]+)", re.IGNORECASE),
    re.compile(
        r"\bCREATE\s+(?:UNIQUE\s+)?(?:TABLE|VIEW|MATERIALIZED\s+VIEW|SEQUENCE|"
        r"INDEX|TRIGGER)\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?P<target>[^\s(]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bDROP\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW|SEQUENCE|INDEX|TRIGGER)\s+"
        r"(?:IF\s+EXISTS\s+)?(?P<target>[^\s(;]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?P<target>[^\s(]+)",
               re.IGNORECASE),
)

# Index DDL may legitimately be unqualified (the index name is resolved via
# search_path); the ON clause is what decides the schema, so it is checked too.
_INDEX_ON_RE = re.compile(r"\bON\s+(?:ONLY\s+)?(?P<target>[^\s(]+)", re.IGNORECASE)

_SCHEMA_DDL_RE = re.compile(
    r"\bCREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<target>[^\s;]+)", re.IGNORECASE
)

# Explicitly forbidden regardless of target: the pipeline must never install
# extensions, touch roles, or reach outside its own schema.
_FORBIDDEN_RE = re.compile(
    r"\b(CREATE|DROP|ALTER)\s+(EXTENSION|ROLE|USER|DATABASE|TABLESPACE)\b",
    re.IGNORECASE,
)


def _normalise(identifier: str) -> str:
    return identifier.strip().strip(";").strip('"').lower()


def _schema_of(identifier: str) -> str | None:
    cleaned = identifier.strip().strip(";")
    if "." not in cleaned:
        return None
    schema_part = cleaned.split(".", 1)[0]
    return _normalise(schema_part)


def _strip_sql_noise(statement: str) -> str:
    """Remove comments and string literals so they cannot smuggle keywords."""
    without_block = re.sub(r"/\*.*?\*/", " ", statement, flags=re.DOTALL)
    without_line = re.sub(r"--[^\n]*", " ", without_block)
    without_strings = re.sub(r"'(?:[^']|'')*'", "''", without_line)
    return without_strings


class SchemaWriteGuard:
    """Validates statements before execution."""

    def __init__(self, allowed_schema: str = "iris_staging") -> None:
        self.allowed_schema = allowed_schema.lower()
        self.checked = 0
        self.writes_allowed = 0
        self.violations: list[str] = []

    def check(self, statement: str) -> None:
        if not statement or not statement.strip():
            return
        self.checked += 1
        cleaned = _strip_sql_noise(statement)

        forbidden = _FORBIDDEN_RE.search(cleaned)
        if forbidden:
            self._fail(
                f"statement type '{forbidden.group(0)}' is not permitted for this "
                f"pipeline",
                statement,
            )

        schema_ddl = _SCHEMA_DDL_RE.search(cleaned)
        if schema_ddl and _normalise(schema_ddl.group("target")) != self.allowed_schema:
            self._fail(
                f"CREATE SCHEMA targets '{schema_ddl.group('target')}'", statement
            )

        is_create_index = bool(
            re.search(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b", cleaned, re.IGNORECASE)
        )
        targets: list[str] = []
        if is_create_index:
            # The index *name* is legitimately unqualified (it inherits the
            # table's schema); the ON clause decides where the write lands.
            on_clause = _INDEX_ON_RE.search(cleaned)
            if on_clause is None:
                self._fail("CREATE INDEX without a resolvable ON target", statement)
            else:
                targets.append(on_clause.group("target"))
        else:
            for pattern in _WRITE_PATTERNS:
                for match in pattern.finditer(cleaned):
                    targets.append(match.group("target"))

        if not targets:
            return

        self.writes_allowed += 1
        for target in targets:
            schema = _schema_of(target)
            if schema is None:
                # Unqualified write target: refuse rather than trust search_path.
                self._fail(
                    f"write target '{target}' is not schema-qualified", statement
                )
            elif schema != self.allowed_schema:
                self._fail(
                    f"write target '{target}' is outside '{self.allowed_schema}'",
                    statement,
                )

    def _fail(self, reason: str, statement: str) -> None:
        self.violations.append(reason)
        log.error(
            "staging_isolation_violation",
            extra={"reason": reason, "statement": statement[:400]},
        )
        raise StagingIsolationError(
            f"blocked: {reason}. Only '{self.allowed_schema}' may be written. "
            f"Statement: {statement[:200]}"
        )

    # -- engine integration ------------------------------------------------
    def attach(self, engine: Engine) -> "SchemaWriteGuard":
        @event.listens_for(engine, "before_cursor_execute")
        def _before_cursor_execute(  # noqa: ANN001, ANN202 - SQLAlchemy signature
            conn, cursor, statement, parameters, context, executemany
        ):
            self.check(statement)

        return self

    def stats(self) -> dict[str, int]:
        return {
            "statements_checked": self.checked,
            "write_statements_allowed": self.writes_allowed,
            "violations": len(self.violations),
        }


def attach_search_path(engine: Engine, schema: str = "iris_staging") -> None:
    """Pin every new connection to the restricted search_path."""

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, connection_record):  # noqa: ANN001
        with dbapi_connection.cursor() as cursor:
            # public is required solely for PostGIS function resolution.
            cursor.execute(f'SET search_path TO "{schema}", public')
        dbapi_connection.commit()


def assert_only_staging(statements: Iterable[str], schema: str = "iris_staging") -> None:
    """Helper for tests: raise on the first statement that breaks isolation."""
    guard = SchemaWriteGuard(schema)
    for statement in statements:
        guard.check(statement)
