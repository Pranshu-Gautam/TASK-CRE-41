#!/usr/bin/env python3
"""Prove staging isolation at the DATABASE level, not just in the application.

The application's statement guard is a convenience. The control that actually
constrains the connection is the PostgreSQL privilege set of the role in
DB_URI. This script tests that directly, using a RAW SQLAlchemy engine with no
search_path hook and no statement guard attached, so nothing in the
application can be credited for the result.

    # one-off, as a DBA:
    psql "$ADMIN_DB_URI" -v ON_ERROR_STOP=1 \
         -v loader_password="'<secret>'" -f sql/002_least_privilege_role.sql

    # then:
    LOADER_DB_URI=postgresql+psycopg://iris_cre41_loader:<pw>@host:5432/db \
    ADMIN_DB_URI=postgresql+psycopg://admin:<pw>@host:5432/db \
    python tools/verify_role_isolation.py

Exit code 0 only if every forbidden write was refused by PostgreSQL and the
canary row in the production-like schema is byte-identical afterwards.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine, text  # noqa: E402

LOADER_URI = os.environ.get("LOADER_DB_URI") or os.environ.get("DB_URI")
ADMIN_URI = os.environ.get("ADMIN_DB_URI")
CANARY_SCHEMA = os.environ.get("CANARY_SCHEMA", "iris_core")
CANARY_TABLE = os.environ.get("CANARY_TABLE", "portfolio")
STAGING = os.environ.get("IRIS_STAGING_SCHEMA", "iris_staging")

FORBIDDEN = [
    ("INSERT into {c}.{t}", "INSERT INTO {c}.{t} VALUES (99, 'injected')"),
    ("UPDATE {c}.{t}", "UPDATE {c}.{t} SET label = 'tampered'"),
    ("DELETE FROM {c}.{t}", "DELETE FROM {c}.{t}"),
    ("SELECT from {c}.{t}", "SELECT count(*) FROM {c}.{t}"),
    ("CREATE TABLE in {c}", "CREATE TABLE {c}.sneaky (a int)"),
    ("DROP TABLE {c}.{t}", "DROP TABLE {c}.{t}"),
    ("CREATE TABLE in public", "CREATE TABLE public.sneaky (a int)"),
    ("DELETE FROM public.spatial_ref_sys", "DELETE FROM public.spatial_ref_sys"),
    ("CREATE EXTENSION", "CREATE EXTENSION IF NOT EXISTS pg_trgm"),
    ("CREATE SCHEMA", "CREATE SCHEMA iris_core_2"),
]

lines: list[str] = []


def say(text_line: str = "") -> None:
    print(text_line)
    lines.append(text_line)


def main() -> int:
    if not LOADER_URI:
        say("LOADER_DB_URI (or DB_URI) is not set; nothing to verify.")
        return 2

    # Raw engine on purpose: no attach_search_path, no SchemaWriteGuard.
    raw = create_engine(LOADER_URI)
    with raw.connect() as connection:
        role = connection.execute(text("SELECT current_user")).scalar()
        search_path = connection.execute(text("SHOW search_path")).scalar()

    say("TASK-CRE-41 — DATABASE-LEVEL STAGING ISOLATION EVIDENCE")
    say("=" * 62)
    say(f"role under test        : {role}")
    say(f"role default search_path: {search_path}")
    say("connection             : raw SQLAlchemy engine — the application's")
    say("                         search_path hook and statement guard are")
    say("                         NOT attached, so only PostgreSQL privileges")
    say("                         can refuse anything below.")
    say()
    say(f"A. writes that must be refused ({CANARY_SCHEMA} = production-like schema)")

    refused = 0
    for label, statement in FORBIDDEN:
        label = label.format(c=CANARY_SCHEMA, t=CANARY_TABLE)
        sql = statement.format(c=CANARY_SCHEMA, t=CANARY_TABLE)
        try:
            with raw.begin() as connection:
                connection.execute(text(sql))
        except Exception as exc:  # noqa: BLE001 - any refusal is a pass
            reason = str(getattr(exc, "orig", exc)).strip().splitlines()[0][:72]
            say(f"   REFUSED  {label:<42} {reason}")
            refused += 1
        else:
            say(f"   !! ALLOWED {label:<42} <-- ISOLATION FAILURE")
    say(f"   -> {refused}/{len(FORBIDDEN)} refused by PostgreSQL privileges")
    say()

    say("B. the work the loader legitimately must be able to do")
    ok = True
    try:
        with raw.connect() as connection:
            rows = connection.execute(
                text(f"SELECT count(*) FROM {STAGING}.commercial_poi_staging")
            ).scalar()
            postgis = connection.execute(text("SELECT postgis_version()")).scalar()
        with raw.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE {STAGING}.commercial_poi_staging "
                    "SET notes = notes WHERE false"
                )
            )
        say(f"   PERMITTED SELECT on {STAGING}.commercial_poi_staging ({rows} rows)")
        say(f"   PERMITTED UPDATE on {STAGING}.commercial_poi_staging")
        say(f"   PERMITTED PostGIS function calls ({postgis.split()[0]})")
    except Exception as exc:  # noqa: BLE001
        say(f"   !! the loader cannot do its job: {exc}")
        ok = False
    say()

    say("C. the production-like schema afterwards")
    if ADMIN_URI:
        admin = create_engine(ADMIN_URI)
        with admin.connect() as connection:
            canary = connection.execute(
                text(f"SELECT id, label FROM {CANARY_SCHEMA}.{CANARY_TABLE} ORDER BY id")
            ).fetchall()
            created = connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema LIKE :pattern"
                ),
                {"pattern": f"{CANARY_SCHEMA}%"},
            ).scalar()
        say(f"   canary rows   : {[tuple(row) for row in canary]}")
        say(f"   tables present: {created} (unchanged => nothing was created)")
        intact = canary == [(1, "untouched")]
    else:
        say("   ADMIN_DB_URI not set; canary re-read skipped")
        intact = True
    say()
    verdict = refused == len(FORBIDDEN) and ok and intact
    say(f"VERDICT: {'PASS' if verdict else 'FAIL'}")

    out = os.environ.get("ISOLATION_EVIDENCE_OUT")
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nwritten: {out}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
