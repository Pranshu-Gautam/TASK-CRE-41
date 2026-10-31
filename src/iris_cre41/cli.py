"""Command line entry points.

    iris-cre41 init-db          create/verify the staging schema (staging only)
    iris-cre41 collect          scrape both official location directories
    iris-cre41 normalize        normalise + deduplicate
    iris-cre41 geocode          geocode and project to EPSG:25832
    iris-cre41 enrich           match OSM building/parking geometries
    iris-cre41 load             idempotent upsert into iris_staging
    iris-cre41 run-all          every stage in order
    iris-cre41 verify           read-only database evidence queries
    iris-cre41 samples          write output samples
    iris-cre41 emit-ddl         print/write the staging DDL
    iris-cre41 smoke-test       offline end-to-end run against fixtures
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import TASK_ID, __version__
from .config import load_settings
from .logging_setup import get_logger, setup_logging

log = get_logger(__name__)

OPERATOR_CHOICES = ("mrwash", "cleancar", "all")


def _operators(value: str) -> tuple[str, ...]:
    return ("mrwash", "cleancar") if value == "all" else (value,)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iris-cre41",
        description=f"{TASK_ID} — Mr. Wash & CleanCar POI pipeline (v{__version__})",
    )
    parser.add_argument(
        "--version", action="version", version=f"{TASK_ID} pipeline {__version__}"
    )
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument(
        "--log-text",
        action="store_true",
        help="human-readable logs instead of JSON lines",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="create the staging schema and tables")

    collect = subparsers.add_parser("collect", help="scrape official location sources")
    collect.add_argument("--operator", choices=OPERATOR_CHOICES, default="all")
    collect.add_argument("--limit", type=int, default=None,
                         help="cap locations per operator (debugging)")

    subparsers.add_parser("normalize", help="normalise and deduplicate")

    geocode = subparsers.add_parser("geocode", help="geocode and project to EPSG:25832")
    geocode.add_argument("--limit", type=int, default=None)

    enrich = subparsers.add_parser("enrich", help="match OSM building/parking polygons")
    enrich.add_argument("--limit", type=int, default=None)

    subparsers.add_parser("load", help="idempotent upsert into iris_staging")

    run_all = subparsers.add_parser("run-all", help="run every stage in order")
    run_all.add_argument("--operator", choices=OPERATOR_CHOICES, default="all")
    run_all.add_argument("--limit", type=int, default=None)
    run_all.add_argument("--skip-load", action="store_true")

    subparsers.add_parser("verify", help="read-only database evidence queries")
    samples = subparsers.add_parser("samples", help="write output samples")
    samples.add_argument("--size", type=int, default=6)

    ddl = subparsers.add_parser("emit-ddl", help="print the staging DDL")
    ddl.add_argument("--out", type=Path, default=None)

    smoke = subparsers.add_parser(
        "smoke-test", help="offline end-to-end run against captured fixtures"
    )
    smoke.add_argument("--skip-load", action="store_true")
    return parser


def cmd_init_db(settings) -> dict[str, Any]:
    from .db import create_staging_engine, create_staging_objects, ensure_postgis
    from .db.engine import current_search_path

    engine, guard = create_staging_engine(
        settings.require_db_uri(), settings.staging_schema
    )
    capabilities = ensure_postgis(engine)
    metadata = create_staging_objects(engine, settings.staging_schema)
    payload = {
        "schema": settings.staging_schema,
        "tables": sorted(table.name for table in metadata.sorted_tables),
        "search_path": current_search_path(engine),
        "database": capabilities,
        "schema_guard": guard.stats(),
    }
    engine.dispose()
    return payload


def cmd_emit_ddl(settings, out: Path | None) -> dict[str, Any]:
    from sqlalchemy.schema import CreateIndex, CreateTable

    from .db.schema import build_metadata

    try:
        from sqlalchemy.dialects import postgresql

        dialect = postgresql.dialect()
    except ImportError:  # pragma: no cover
        dialect = None

    metadata = build_metadata(settings.staging_schema)
    statements = [f'CREATE SCHEMA IF NOT EXISTS "{settings.staging_schema}";']
    for table in metadata.sorted_tables:
        statements.append(
            str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip()
            + ";"
        )
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            statements.append(
                str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)).strip()
                + ";"
            )
    ddl = "\n\n".join(statements) + "\n"
    header = (
        f"-- {TASK_ID} — LOCAL staging DDL, generated by `iris-cre41 emit-ddl`\n"
        f"-- pipeline version {__version__}\n"
        "-- NOTE: this is the candidate's local staging schema. The official\n"
        "--       iris_staging.commercial_poi_staging DDL was not provided and\n"
        "--       must be confirmed with Tariq before any shared-environment load.\n\n"
    )
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(header + ddl, encoding="utf-8")
        return {"written": str(out), "statements": len(statements)}
    print(header + ddl)
    return {"statements": len(statements)}


def cmd_smoke_test(settings, skip_load: bool) -> dict[str, Any]:
    from .smoke import run_offline_smoke_test

    return run_offline_smoke_test(settings, skip_load=skip_load)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level, json_logs=not args.log_text)
    settings = load_settings()

    from . import pipeline, reporting

    try:
        if args.command == "init-db":
            _emit(cmd_init_db(settings))
        elif args.command == "collect":
            _emit(
                pipeline.run_collect(
                    settings, operators=_operators(args.operator), limit=args.limit
                )
            )
        elif args.command == "normalize":
            _emit(pipeline.run_normalize(settings))
        elif args.command == "geocode":
            _emit(pipeline.run_geocode(settings, limit=args.limit))
        elif args.command == "enrich":
            _emit(pipeline.run_enrich(settings, limit=args.limit))
        elif args.command == "load":
            _emit(pipeline.run_load(settings))
        elif args.command == "run-all":
            _emit(
                pipeline.run_all(
                    settings,
                    operators=_operators(args.operator),
                    limit=args.limit,
                    skip_load=args.skip_load,
                )
            )
        elif args.command == "verify":
            payload = reporting.run_verification(settings)
            path = reporting.write_verification_report(settings, payload)
            _emit({"report": str(path), "queries": len(payload["queries"])})
        elif args.command == "samples":
            _emit(reporting.export_samples(settings, sample_size=args.size))
        elif args.command == "emit-ddl":
            _emit(cmd_emit_ddl(settings, args.out))
        elif args.command == "smoke-test":
            _emit(cmd_smoke_test(settings, args.skip_load))
        else:  # pragma: no cover - argparse enforces the choices
            parser.error(f"unknown command {args.command}")
    except Exception as exc:
        log.error(
            "command_failed",
            extra={"command": args.command, "error": f"{type(exc).__name__}: {exc}"},
            exc_info=True,
        )
        print(f"ERROR [{type(exc).__name__}] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
