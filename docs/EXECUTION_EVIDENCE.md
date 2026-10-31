# TASK-CRE-41 — Execution evidence

Everything below is **measured output from the executed run**, read back from
the database or from generated files. Machine-readable copies are in
[`../samples/`](../samples) and `data/pipeline_stats.json`.

* Executed: **2026-10-31**, full live run end to end, then re-audited.
* Database: **PostgreSQL 16.4** (Debian) / **PostGIS 3.4.3**.
* Pipeline: `iris-cre41` 1.0.0. Live run on Python 3.14; the shipped image is
  Python 3.13; Python **3.11.18** independently verified (§10).

Regenerate with:

```bash
iris-cre41 run-all          # -> data/pipeline_stats.json
iris-cre41 verify           # -> samples/db_verification.{txt,json}
iris-cre41 samples          # -> samples/*.csv, samples/*.json
DB_URI=... LOADER_DB_URI=... pytest -q
make verify-py311 verify-role
```

---

## 1. Headline counts

| Metric | Mr. Wash | Clean Car | Total |
|---|---:|---:|---:|
| Locations discovered on the official site | 36 | 48 | **84** |
| Pages fetched successfully / failures | 36 / 0 | 48 / 0 | **84 / 0** |
| Normalised / duplicates prevented | 36 / 0 | 48 / 0 | **84 / 0** |
| Address complete (street + PLZ + city) | 36 | 47 | **83** |
| Geocoded to a usable coordinate | 36 | 47 | **83** |
| Projected to EPSG:25832 | 36 | 47 | **83** |
| Building polygon matched (geometric) | 35 | 46 | **81** |
| Parking polygon matched (geometric) | 31 | 41 | **72** |
| **Building area attributable to the operator** | — | — | **24** |
| **Parking area attributable to the operator** | — | — | **0** |
| Rows in `iris_staging.commercial_poi_staging` | 36 | 48 | **84** |

Full-pipeline wall clock: **327 s** with warm caches; the cold first run took
~45 min, dominated by deliberate rate limiting (84 pages at 1 req/s, 83
Overpass queries at 20–30 s each).

## 2. Collection

```
collect: 84 locations, 84 pages fetched, 0 failures
  mrwash   36   directory /de/frontend/default/locations + 36 branch pages
  cleancar 48   /standorte/ index (47 links) ∪ wp-sitemap-posts-page-1.xml (48)
```

Coverage checks actually performed:

* Mr. Wash branch-id gaps (10, 11, 17, 18, 19, 39, 41, 42, 45) were each
  requested individually — **all return HTTP 404**, so the 36-branch directory
  is exhaustive, not paginated.
* The Clean Car index and sitemap disagree by one page; the union is used, so
  neither source alone determines coverage.

## 3. Normalisation and deduplication

```
normalize: input 84 -> normalised 84 -> canonical 84, duplicates prevented 0
           address complete 83/84
```

Zero duplicates is a **measured result**: slugs that look like duplicates
(`bad-oldesloe-2`, `eckernfoerde-2`, `strahlsund2`,
`hamburg-kollaustrasse-2`) resolve to *different street addresses* and are
separate sites. The de-duplication machinery is exercised by unit tests and by
a database test proving a colliding `dedup_key` is rejected.

One record is incomplete: **Clean Car Glinde** publishes no postal address at
all. Stored with `NULL` address, `geocode_status = error` and the flags
`address_block_not_found`, `street_missing`, `city_missing`,
`postal_code_missing`. **No city centroid was substituted.**

## 4. Geocoding

```
geocode: 84 attempted -> 83 usable (71 ok, 12 ambiguous_resolved), 1 error
         projected to EPSG:25832: 83
         strategy used: structured 83, structured_no_city 1
         precision: address 71, street 10, locality 2
         ambiguous 12 | postcode mismatch 2 | city mismatch 1
```

83 of 84 addresses cost exactly **one** request; one needed the no-city
fallback (`Mülheim a.d.R.`). **Nothing was substituted for the failure.**

## 5. Spatial attribution audit — the three assertions

This is the part most easily over-claimed, so the three claims are stored and
counted separately.

### Assertion 1 — "the point lies inside this polygon" (geometry)

| method | geometric confidence | rows |
|---|---|---:|
| `point_in_building` | high | 64 |
| `nearest_building_le_30m` | medium | 7 |
| `nearest_building_le_30m` | low | 9 |
| `nearest_building_le_80m` | low | 1 |
| *(no match)* | none | 3 |

The 10 `low` rows are **confidence-capped**, not weak geometry: the underlying
geocode was street-level, so no spatial match from it may claim better.
Geometry validity: **81 valid, 0 repaired, 0 rejected**.

### Assertion 2 — "this polygon plausibly contains the operator's facility"

Buildings (84 rows):

| `building_operator_attribution_status` | rows | raw OSM areas | **operator areas** | max raw m² |
|---|---:|---:|---:|---:|
| `unverified` | 42 | 42 | **0** | 15 284 |
| `operator_tagged` | 24 | 24 | **24** | 5 432 |
| `third_party_named` | 12 | 12 | **0** | 47 136 |
| `car_wash_tagged` | 3 | 3 | **0** | 1 216 |
| `no_match` | 3 | 0 | **0** | — |

Parking (84 rows):

| `parking_operator_attribution_status` | rows | raw OSM areas | **operator areas** | max raw m² |
|---|---:|---:|---:|---:|
| `unverified` | 57 | 57 | **0** | 21 901 |
| `third_party_named` | 15 | 15 | **0** | 14 554 |
| `no_match` | 12 | 0 | **0** | — |

**No parking polygon in OSM names either operator**, so every
`parking_operator_attributable_area_sqm` is NULL. That is the correct result,
not a pipeline gap.

### Assertion 3 — "demonstrably controlled by the operator"

**Not reached for a single row.** `operator_usable_area_verified` is `false`
on all 84 rows, and a CHECK constraint prevents it being set true. Reaching it
needs a cadastre/ALKIS extract, a land-registry entry, a lease, or an
operator-supplied site register. Volunteer-entered OSM tags are none of those.

### What this does to the area totals

| | m² |
|---|---:|
| Raw matched OSM footprint, all 81 matches | 229 504 |
| …of which sits on polygons **named after a third party** | 127 068 |
| **Operator-attributable footprint (24 rows)** | **46 387** |
| **Operator-attributable parking** | **0** |

Two verified worked examples of why this separation exists:

* **Clean Car Kiel, Mühlendamm 1** — the point really is inside
  `way/25922585`, so geometric confidence is `high`. But that polygon is the
  **47 136 m² Citti-Park Kiel shopping mall** (`shop=mall`,
  `name=Citti-Park Kiel`). Status `third_party_named`, operator area **NULL**.
* **Clean Car Kiel, Winterbeker Weg 44** — inside a **22 922 m² REWE Center**.
  Status `third_party_named`, operator area **NULL**.

Database-enforced guarantee, re-queried after the load:

```
operator_area_never_asserted_without_attribution -> violating_rows = 0
```

## 6. Data-quality flags across all 84 rows

| count | flag |
|---:|---|
| 72 | `parking_operator_area_not_attributable` |
| 57 | `building_operator_area_not_attributable` |
| 42 | `building_operator_attribution_unverified` |
| 42 | `geocode_name_not_operator` |
| 15 | `parking_attributed_to_other_occupant` |
| 12 | `building_attributed_to_other_occupant` |
| 12 | `geocode_ambiguous` |
| 10 | `building_match_low_confidence` |
| 10 | `geocode_precision_street` |
| 9 | `osm_no_parking_match` |
| 8 | `parking_area_outlier_gt_10000_sqm` |
| 5 | `building_area_outlier_gt_10000_sqm` |
| 2 | `geocode_postcode_mismatch` |
| 2 | `geocode_precision_locality`, `enrichment_skipped_locality_precision` |
| 1 | `geocode_city_mismatch` |
| 1 each | `address_unavailable`, `address_block_not_found`, `street_missing`, `city_missing`, `postal_code_missing`, `geocode_error`, `enrichment_skipped_no_point` |

## 7. Database validation

From [`../samples/db_verification.txt`](../samples/db_verification.txt)
(17 read-only queries):

| Check | Result |
|---|---|
| `search_path` | `iris_staging, public` |
| Server | PostgreSQL 16.4, PostGIS 3.4 (USE_GEOS=1 USE_PROJ=1) |
| Rows / points / buildings / parking | 84 / 83 / 81 / 72 |
| Distinct `ST_SRID` on all three geometry columns | **25832** only |
| `geometry_columns` registration | `geom` POINT 25832; `building_geom`, `parking_geom` MULTIPOLYGON 25832 |
| Rows with invalid geometry | **0** |
| Duplicate `dedup_key` groups | **0** |
| Stored area vs `ST_Area()` recomputed by PostGIS | max delta **0.0 m²** over all 81 rows |
| Operator area asserted without attribution | **0 rows** |
| Tables in `iris_staging` | the 3 expected |
| Pipeline tables outside `iris_staging` | **0 rows returned** |
| Generated DDL applied to a clean schema | 3 tables, 94 columns, 3 geometry columns @ SRID 25832 |

**What the area cross-check does and does not prove.** It proves the number in
`building_footprint_area_sqm` is a genuine `ST_Area` of the stored geometry in
EPSG:25832 — i.e. the reprojection, the stored SRID and the area computation
are mutually consistent, and the value is neither an estimate nor fabricated.
It says **nothing whatever** about whether the matched building belongs to the
operator. That is Assertion 2, answered only by
`*_operator_attribution_status`.

## 8. Idempotency

[`../samples/idempotency_evidence.txt`](../samples/idempotency_evidence.txt)
carries the annotated history from `iris_staging.commercial_poi_load_run`.
Summary of the measured counts:

| run | inserted | updated | unchanged | failed |
|---|---:|---:|---:|---:|
| 1 — pre-migration schema drift | 0 | 0 | 0 | **84** |
| 2 — first real load | **84** | 0 | 0 | 0 |
| 3, 4 — repeat, identical input | 0 | 0 | **84** | 0 |
| 5 — enrichment logic changed | 0 | **81** | 3 | 0 |
| 6 — repeat | 0 | 0 | **84** | 0 |
| 7 — `row_hash` definition changed | 0 | **84** | 0 | 0 |
| 8, 9 — repeat | 0 | 0 | **84** | 0 |
| 10 — attribution columns added | 0 | **84** | 0 | 0 |
| 11, 12 — repeat | 0 | 0 | **84** | 0 |
| 13, 14 — repeat **as the restricted role** | 0 | 0 | **84** | 0 |

Row count stayed at **84** throughout; `poi_uid`, `source_uid` and `dedup_key`
are each distinct 84 times. Run 5 is the positive control for row-level change
detection: exactly the 81 rows with a building match changed.

## 9. Database-level staging isolation

[`../samples/role_isolation_evidence.txt`](../samples/role_isolation_evidence.txt),
regenerable with `make verify-role`. Tested through a **raw SQLAlchemy engine
with the application's search_path hook and statement guard removed**, so only
PostgreSQL privileges can refuse anything:

```
role under test: iris_cre41_loader
  10/10 forbidden writes REFUSED by PostgreSQL privileges
    INSERT / UPDATE / DELETE / SELECT / CREATE TABLE / DROP TABLE on iris_core
    CREATE TABLE in public ; DELETE FROM public.spatial_ref_sys
    CREATE EXTENSION ; CREATE SCHEMA
  PERMITTED: SELECT + UPDATE on iris_staging, PostGIS function calls
  canary row iris_core.portfolio -> [(1, 'untouched')]
  tables in iris_core* -> 1 (unchanged; nothing was created)
VERDICT: PASS
```

The full pipeline then ran as that role: `init-db` succeeded and two
consecutive loads reported `inserted=0 updated=0 unchanged=84 failed=0`.

## 10. Python 3.11 verification

`requirements.txt` carries environment markers because `pyproj` 3.8 and
`shapely` 2.2 declare `Requires-Python >= 3.12`. Reproduce with
`make verify-py311`:

```
python 3.11.18
  shapely 2.0.7   pyproj 3.7.2   sqlalchemy 2.1.6
  geoalchemy2 0.20.0   psycopg 3.3.6   requests 2.34.2   lxml 6.1.3
pytest -q -m "not integration"   -> 248 passed, 19 deselected
smoke-test --skip-load           -> "passed": true
```

## 11. Tests

| Invocation | Result |
|---|---|
| `pytest -q -m "not integration"` | **248 passed**, 19 deselected |
| `DB_URI=... LOADER_DB_URI=... pytest -q` | **276 passed, 0 failed, 0 skipped** |
| `pytest -q -m integration` (no `DB_URI`) | **19 skipped**, "NOT executed" |
| Docker (3.13) `pytest -q -m "not integration"` | **248 passed** |
| Docker (3.11) `pytest -q -m "not integration"` | **248 passed** |
| `docker compose run --rm pipeline smoke-test --skip-load` | **`"passed": true`** |
| Unit tests from the extracted submission ZIP | **248 passed** |

248 unit tests (no network, no database) cover both operators' parsing against
real captured fixtures including three Clean Car markup variants, address
normalisation, stable ids, deduplication, CRS validation and projection,
geometry assembly/repair/rejection, the spatial matching rules, **the operator
attribution classifier**, geocoding response interpretation and every failure
mode, HTTP retry/backoff/rate limiting, the staging write guard (31
allow/block cases), row hashing, **JSONB NULL semantics**, and the offline
end-to-end smoke test.

28 integration tests against real PostgreSQL 16.4/PostGIS 3.4.3 add: version
assertions, restricted `search_path`, geometry-column registration, GiST
indexes, insert→idempotent re-load, change detection, `first_seen_at`
preservation, duplicate-key rejection, SRID and geometry-type assertions,
stored-vs-PostGIS area agreement, NULL-metric handling, wrong-SRID and
malformed-postcode rejection by CHECK constraints, **the four
attribution constraints**, **SQL-NULL-not-JSON-null**, **the column-rename
migration preserving data**, the run log, a canary table in a production-like
schema, and **three database-level isolation tests using the restricted role
through an unguarded engine**.
