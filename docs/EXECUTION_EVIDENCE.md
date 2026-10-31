# TASK-CRE-41 — Execution evidence

Everything below is **measured output from the executed run**, not an estimate.
Machine-readable copies are in [`../samples/`](../samples) and
`data/pipeline_stats.json`.

* Executed: **2026-10-31**, full live run end to end.
* Database: **PostgreSQL 16.4** (Debian) / **PostGIS 3.4.3**.
* Pipeline: `iris-cre41` 1.0.0, Python 3.14 locally, Python 3.13 in the image.

Regenerate with:

```bash
iris-cre41 run-all          # -> data/pipeline_stats.json
iris-cre41 verify           # -> samples/db_verification.{txt,json}
iris-cre41 samples          # -> samples/*.csv, samples/*.json
DB_URI=... pytest -q        # unit + integration
```

---

## 1. Headline counts

| Metric | Mr. Wash | Clean Car | Total |
|---|---:|---:|---:|
| Locations discovered on the official site | 36 | 48 | **84** |
| Pages fetched successfully | 36 | 48 | **84** |
| Collection failures | 0 | 0 | **0** |
| Normalised | 36 | 48 | **84** |
| Duplicates prevented | 0 | 0 | **0** |
| Address complete (street + PLZ + city) | 36 | 47 | **83** |
| Geocoded to a usable coordinate | 36 | 47 | **83** |
| Projected to EPSG:25832 | 36 | 47 | **83** |
| Building footprint matched | 35 | 46 | **81** |
| Parking polygon matched | 31 | 41 | **72** |
| OSM operator-tag corroboration | 30 | 24 | **54** |
| Rows in `iris_staging.commercial_poi_staging` | 36 | 48 | **84** |

Full-pipeline wall clock: **327 s** (5 min 27 s) with warm geocode/Overpass
caches; the cold first run took ~45 min, dominated by deliberate rate limiting
(84 pages at 1 req/s, 83 Overpass queries at 20–30 s each).

## 2. Collection

```
collect: 84 locations, 84 pages fetched, 0 failures, 326 s
  mrwash   36   directory /de/frontend/default/locations + 36 branch pages
  cleancar 48   /standorte/ index (47 links) ∪ wp-sitemap-posts-page-1.xml (48 links)
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

Zero duplicates is a **measured result, not an assumption**: slugs that look
like duplicates (`bad-oldesloe-2`, `eckernfoerde-2`, `strahlsund2`,
`hamburg-kollaustrasse-2`) resolve to *different street addresses* and are
therefore genuinely separate sites. The de-duplication machinery is exercised
by unit tests (`tests/test_normalize.py`) and by a database-level test that
proves a colliding `dedup_key` is rejected
(`test_duplicate_dedup_key_cannot_be_inserted`).

One record is incomplete: **Clean Car Glinde** (`/standorte/glinde/`) publishes
no postal address at all. Stored with `NULL` address, `geocode_status = error`
and flags `address_block_not_found`, `street_missing`, `city_missing`,
`postal_code_missing`. **No city centroid was substituted.**

## 4. Geocoding

```
geocode: 84 attempted -> 83 usable (71 ok, 12 ambiguous_resolved), 1 error
         projected to EPSG:25832: 83
         strategy used: structured 83, structured_no_city 1
         precision: address 71, street 10, locality 2
         ambiguous 12 | postcode mismatch 2 | city mismatch 1
```

* The bounded strategy ladder cost **one request for 83 of 84 addresses**;
  exactly one needed the no-city fallback (`Weseler Straße 53, 45478
  Mülheim a.d.R.` — the abbreviated city defeats the structured query).
* 12 results are flagged `ambiguous_resolved`: the two best-ranked candidates
  were more than 150 m apart. The chosen candidate is kept and the spread is
  stored, so a reviewer can see how wrong it could be.
* 42 results carry `geocode_name_not_operator`: the matched OSM feature does
  not carry the operator's name. That is expected for a plain building match
  and is recorded rather than treated as a failure.
* **Nothing was substituted for the single failure.**

## 5. Geospatial enrichment

```
enrich: 84 input, 2 skipped (locality-precision geocode), 1 skipped (no point)
        -> 81 matching attempts, 81 building matches, 72 parking matches
        0 Overpass errors (after one retried read-timeout on the cold run)
        geometry validity: 81 valid, 0 repaired, 0 rejected
        building confidence: high 64, medium 7, low 10
        parking  confidence: high  2, medium 7, low 63
        enrichment_status: enriched 72, partial 9, point_only 2, failed 1
```

Match-method distribution read back from the database:

| method | confidence | rows |
|---|---|---:|
| `point_in_building` | high | 64 |
| `nearest_building_le_30m` | medium | 7 |
| `nearest_building_le_30m` | low | 9 |
| `nearest_building_le_80m` | low | 1 |
| *(none)* | none | 3 |

The 10 `low` rows are **confidence-capped**, not weak geometry: the underlying
geocode precision was `street`, so no spatial match from it is allowed to claim
better than `low`.

Area statistics (EPSG:25832, m²):

| | n | min | median | max | total |
|---|---:|---:|---:|---:|---:|
| Building footprint | 81 | 33.4 | 476.8 | 47 136.0 | 229 504.0 |
| Parking polygon | 72 | 33.3 | 1 307.8 | 21 901.0 | — |

**The 47 136 m² maximum is a correctly-flagged false friend**, and is the single
most important caveat in this output: Clean Car Kiel (Mühlendamm 1) sits inside
the *Citti-Park Kiel* shopping mall, so `point_in_building` returns the whole
mall. The row carries `building_area_outlier_gt_10000_sqm` and
`building_attributed_to_other_occupant`, and the matched feature's OSM tags
(`shop=mall`, `name=Citti-Park Kiel`) are stored verbatim. The same applies to
a REWE Center at 22 922 m². **5 rows** are area outliers and **12 rows** are
attributed to another named occupant. These footprints must not be read as
car-wash roof area.

## 6. Data-quality flags across all 84 rows

| count | flag | meaning |
|---:|---|---|
| 42 | `geocode_name_not_operator` | matched OSM feature does not carry the operator's name |
| 12 | `building_attributed_to_other_occupant` | footprint belongs to a named third party |
| 12 | `geocode_ambiguous` | top-2 candidates > 150 m apart |
| 10 | `building_match_low_confidence` | capped because the geocode is street-level |
| 10 | `geocode_precision_street` | house number not resolved |
| 9 | `osm_no_parking_match` | no parking polygon in radius |
| 5 | `building_area_outlier_gt_10000_sqm` | footprint implausible for a car-wash site |
| 2 | `geocode_postcode_mismatch` | returned postcode ≠ published |
| 2 | `geocode_precision_locality` + `enrichment_skipped_locality_precision` | town centroid; polygon matching deliberately skipped |
| 1 | `geocode_city_mismatch` | returned city ≠ published (Hamburg vs Wandsbek) |
| 1 each | `address_unavailable`, `address_block_not_found`, `street_missing`, `city_missing`, `postal_code_missing`, `geocode_error`, `enrichment_skipped_no_point` | the single Clean Car Glinde record with no published address |

## 7. Database validation

From [`../samples/db_verification.txt`](../samples/db_verification.txt):

| Check | Result |
|---|---|
| `search_path` | `iris_staging, public` |
| Server | PostgreSQL 16.4, PostGIS 3.4 (USE_GEOS=1 USE_PROJ=1) |
| Rows / points / buildings / parking | 84 / 83 / 81 / 72 |
| Distinct `ST_SRID` on all three geometry columns | **25832** only |
| `geometry_columns` registration | `geom` POINT 25832, `building_geom` MULTIPOLYGON 25832, `parking_geom` MULTIPOLYGON 25832 |
| Rows with invalid geometry | **0** |
| Duplicate `dedup_key` groups | **0** |
| Stored area vs `ST_Area(building_geom)` recomputed by PostGIS | max delta **0.0 m²** across all 81 rows |
| Tables in `iris_staging` | `commercial_poi_staging`, `commercial_poi_raw_staging`, `commercial_poi_load_run` |
| Pipeline tables outside `iris_staging` | **0 rows returned** |
| Schema-guard violations during the load | **0** (168 write statements inspected) |

The area cross-check is the strongest geometric evidence here: the areas were
computed in Python (Shapely, after `pyproj` reprojection) and recomputed
independently by PostGIS from the stored geometry. They agree to the stored
precision of 0.1 m², which confirms the reprojection, the stored SRID and the
area computation are all consistent.

## 8. Idempotency

See [`../samples/idempotency_evidence.txt`](../samples/idempotency_evidence.txt)
for the annotated nine-run history read back from
`iris_staging.commercial_poi_load_run`. Summary:

| run | inserted | updated | unchanged | failed |
|---|---:|---:|---:|---:|
| 1 — pre-migration schema drift | 0 | 0 | 0 | **84** |
| 2 — first real load | **84** | 0 | 0 | 0 |
| 3, 4 — repeat, identical input | 0 | 0 | **84** | 0 |
| 5 — enrichment logic changed (2 new flags) | 0 | **81** | 3 | 0 |
| 6 — repeat | 0 | 0 | **84** | 0 |
| 7 — `row_hash` definition changed | 0 | **84** | 0 | 0 |
| 8, 9 — repeat | 0 | 0 | **84** | 0 |

Row count stayed at **84** throughout; `poi_uid`, `source_uid` and `dedup_key`
are each distinct 84 times. Run 5 is the positive control: exactly the 81 rows
with a building match changed, and the 3 without one were left alone — change
detection is row-level.

## 9. Tests

```
pytest -q -m "not integration"                             229 passed, 19 deselected
DB_URI=... pytest -q                                       248 passed
pytest -q -m integration            (no DB_URI)             19 skipped, "NOT executed"
docker compose run --rm pipeline smoke-test --skip-load    "passed": true
docker ... pytest -q -m "not integration"  (in the image)  229 passed
```

* **229 unit tests** — no network, no database. Cover HTML parsing for both
  operators (real captured fixtures, three Clean Car markup variants), address
  normalisation, stable ids, deduplication, CRS validation and projection,
  geometry assembly/repair/rejection, the spatial matching rules, geocoding
  response interpretation (ambiguity, consistency, precision, every failure
  mode), HTTP retry/backoff/rate limiting, the staging write guard
  (31 allow/block cases), row hashing, and the offline end-to-end smoke test.
* **19 integration tests** — real PostgreSQL 16.4/PostGIS 3.4.3: version
  assertions, restricted `search_path`, geometry-column registration, GiST
  indexes, insert→idempotent re-load, change detection, `first_seen_at`
  preservation, duplicate-key rejection, SRID and geometry-type assertions in
  the database, stored-vs-PostGIS area agreement, NULL-metric handling,
  wrong-SRID rejection by CHECK constraint, malformed-postcode rejection, the
  run log, and four isolation tests including a canary table in a
  production-like schema.
* **0 failed, 0 skipped** in the full run with `DB_URI` set. Without `DB_URI`
  the 19 integration tests report `SKIPPED (DB_URI not set — integration test
  NOT executed)` — they never silently pass.
