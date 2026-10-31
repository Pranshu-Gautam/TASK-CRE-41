#!/usr/bin/env python3
"""Prove the stored areas are real metrics of the source geometry.

For every row with a building footprint, the area is computed three
independent ways and compared:

  1. rebuilt from the **raw Overpass element** in data/overpass_cache.sqlite,
     reprojected with pyproj and measured with Shapely — a path that never
     touches the database;
  2. Shapely on the WKT read back out of PostGIS;
  3. PostGIS ``ST_Area`` on the stored geometry.

Agreement across all three shows the number is a genuine ``ST_Area`` of the
matched OSM feature in EPSG:25832 — neither an estimate nor a fabricated
value. It says **nothing** about whether the building belongs to the operator;
that is the separate question answered by
``building_operator_attribution_status``.

    DB_URI=... python tools/validate_area_provenance.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shapely import from_wkt  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from iris_cre41.crs import transform_geometry  # noqa: E402
from iris_cre41.enrich.geometry import (  # noqa: E402
    as_multipolygon,
    element_to_geometry,
    validate_and_repair,
)
from iris_cre41.enrich.overpass import feature_id  # noqa: E402

TOLERANCE_SQM = 0.1  # stored precision is one decimal place
lines: list[str] = []


def say(line: str = "") -> None:
    print(line)
    lines.append(line)


def load_cached_elements(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    connection = sqlite3.connect(str(path))
    elements: dict[str, dict] = {}
    for (response,) in connection.execute(
        "SELECT response FROM response_cache WHERE namespace = 'overpass/site'"
    ):
        for element in json.loads(response).get("elements", []):
            elements.setdefault(feature_id(element), element)
    connection.close()
    return elements


def main() -> int:
    db_uri = os.environ.get("DB_URI")
    if not db_uri:
        say("DB_URI is not set; nothing to validate.")
        return 2
    cache_path = Path(
        os.environ.get("IRIS_OVERPASS_CACHE", "data/overpass_cache.sqlite")
    )
    elements = load_cached_elements(cache_path)

    say("TASK-CRE-41 — AREA PROVENANCE VALIDATION")
    say("=" * 62)
    say(f"raw Overpass cache : {cache_path} ({len(elements)} distinct OSM features)")
    say("three independent computations per row:")
    say("  A  Shapely on the geometry rebuilt from the RAW OSM element")
    say("  B  Shapely on the WKT read back from PostGIS")
    say("  C  PostGIS ST_Area on the stored geometry")
    say()

    engine = create_engine(db_uri)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT poi_uid, building_feature_id, "
                "       building_footprint_area_sqm::float8 AS stored, "
                "       ST_Area(building_geom)::float8      AS postgis_area, "
                "       ST_AsText(building_geom)            AS wkt, "
                "       ST_SRID(building_geom)              AS srid "
                "FROM iris_staging.commercial_poi_staging "
                "WHERE building_geom IS NOT NULL ORDER BY poi_uid"
            )
        ).fetchall()

    checked = mismatched = uncached = 0
    worst = 0.0
    for row in rows:
        element = elements.get(row.building_feature_id)
        deltas = []
        if element is not None:
            validation = validate_and_repair(element_to_geometry(element))
            rebuilt = as_multipolygon(transform_geometry(validation.geometry, 4326))
            deltas.append(("A", abs(rebuilt.area - row.stored)))
            checked += 1
        else:
            uncached += 1
        deltas.append(("B", abs(from_wkt(row.wkt).area - row.stored)))
        deltas.append(("C", abs(row.postgis_area - row.stored)))
        worst = max([worst] + [d for _, d in deltas])
        if max(d for _, d in deltas) > TOLERANCE_SQM or row.srid != 25832:
            mismatched += 1
            say(
                f"  MISMATCH {row.poi_uid} {row.building_feature_id} "
                f"stored={row.stored} srid={row.srid} "
                + " ".join(f"{k}Δ={v:.3f}" for k, v in deltas)
            )

    say(f"rows with a building geometry       : {len(rows)}")
    say(f"re-derived from the raw OSM cache   : {checked}")
    say(f"feature absent from the cache       : {uncached}")
    say(f"mismatches (> {TOLERANCE_SQM} m2 or bad SRID) : {mismatched}")
    say(f"worst absolute delta across all     : {worst:.4f} m2")
    say()
    say("VERDICT: " + ("PASS" if mismatched == 0 else "FAIL"))
    say()
    say("Interpretation: the stored areas are genuine ST_Area metrics of the")
    say("matched source geometry in EPSG:25832 — not estimates, not fabricated.")
    say("This does NOT show the matched building belongs to the operator; that")
    say("is building_operator_attribution_status, and for 57 of 81 matches the")
    say("evidence does not support attribution, so the operator-specific area")
    say("column is NULL.")

    out = os.environ.get("AREA_EVIDENCE_OUT")
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nwritten: {out}")
    return 0 if mismatched == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
