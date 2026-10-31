# TASK-CRE-41 — Staging schema and source mapping

> **The official DDL for `iris_staging.commercial_poi_staging` was not supplied
> with this task.** What follows is a *local* staging schema, designed as a
> superset of the required fields with explicit provenance. It is **not** the
> production schema. Every assumption marked **[CONFIRM: Tariq]** needs sign-off
> before any load into a shared environment.

Generated DDL: [`sql/001_init_staging.sql`](../sql/001_init_staging.sql)
(regenerate with `iris-cre41 emit-ddl --out sql/001_init_staging.sql`).

## 1. Tables

| Table | Purpose |
|---|---|
| `iris_staging.commercial_poi_staging` | One row per de-duplicated commercial POI. The task's target table. |
| `iris_staging.commercial_poi_raw_staging` | Immutable verbatim observations, **including** records dropped as duplicates. Provenance and re-processing. |
| `iris_staging.commercial_poi_load_run` | One row per load run: counts, timings, stats. Execution evidence inside the database. |

**[CONFIRM: Tariq]** Whether the two auxiliary tables are acceptable in
`iris_staging`, or whether raw observations and run logs belong elsewhere.

## 2. Provenance vocabulary

Every value in the POI table falls into exactly one of these classes, and the
column groups below are organised by it:

| Class | Meaning | Trust |
|---|---|---|
| `observed` | Read verbatim from the operator's official website. | Highest available. The operator is the authority on its own locations. |
| `derived` | Deterministic transform of an observed value (normalisation, hashing). | As good as the observation; reproducible. |
| `geocoded` | Coordinate produced by OSM Nominatim from an observed address. | Third-party; carries explicit status + consistency flags. |
| `inferred` | Spatial match against third-party open data (OSM). | Weakest. Geometric association only — **never ownership**. |
| `unavailable` | Not published by any consulted source. Stored as `NULL`. | — |

## 3. Column mapping

### 3.1 Identity (`derived`)

| Column | Type | Source |
|---|---|---|
| `poi_uid` | `text` PK | `'cre41-' + sha1(source_uid)[:20]`. Stable across runs; depends only on the operator's own record id. |
| `source_uid` | `text` UNIQUE | `<operator_slug>:<source_system>:<source_record_id>` — e.g. `mrwash:mrwash.de:branch-36`, `cleancar:clean-car.com:bremen`. |
| `dedup_key` | `text` UNIQUE | `sha1(operator_slug + postal_code + normalised street + normalised house number)`. Operator-scoped on purpose: two operators may share a retail-park address. |

**[CONFIRM: Tariq]** Whether production expects a surrogate `bigint` key, a
UUID, or this deterministic text id as the primary key.

### 3.2 Address and operator (`observed` / `derived`)

| Column | Mr. Wash source | Clean Car source |
|---|---|---|
| `operator`, `operator_slug` | constant `Mr. Wash` / `mrwash` | constant `Clean Car` / `cleancar` |
| `location_name` | directory row link text (e.g. "Aachen Rothe Erde") | page heading / `<title>` (e.g. "Hamburg – Steilshoop") |
| `street`, `house_number` | "Anschrift" block on `/de/frontend/branch?id=<n>`, split deterministically | `<p>Street 12<br/>12345 City</p>` (or two sibling `<p>`s), split deterministically |
| `postal_code`, `city` | the `12345 City` line of the same block | the `12345 City` line |
| `address_raw` | the verbatim multi-line block | the verbatim two-line block |
| `address_normalized` | `derived`: `"<street> <no>, <plz> <city>"` | same |
| `country` | constant `DE` (CHECK enforced) | same |
| `phone`, `email`, `opening_hours_raw` | observed on the branch page | observed on the location page |
| `max_vehicle_height_m`, `max_vehicle_width_m` | "Einfahrtsbedingungen" (Höhe/Breite max) | "Durchfahrtshöhe/-breite" |
| `source_attributes` (JSONB) | services list, branch manager, legal entity, directory cross-check values | slug, site type, published programme prices, fax, self-service clearance |

City values are stored **exactly as published**. Clean Car publishes a borough
for one Hamburg site (`22309 Wandsbek`, where the postal city is Hamburg); the
observation is preserved and the mismatch is recorded in
`geocode_city_match` / `data_quality_flags` rather than corrected.

### 3.3 Provenance (`observed`)

`source_system`, `source_record_id`, `source_url`, `retrieved_at`,
`source_html_sha256` — the exact page, when it was read, and a hash of the
response body so a later re-read can be compared.

### 3.4 Geocoding (`geocoded`)

| Column | Notes |
|---|---|
| `geocode_lat`, `geocode_lon`, `geocode_srid` | **Original** WGS84 coordinate as returned, kept separate from the projected one. `geocode_srid` is always 4326. |
| `geocode_status` | `ok` \| `ambiguous_resolved` \| `not_found` \| `invalid_coordinates` \| `error` \| `not_attempted` (CHECK enforced). |
| `geocode_strategy` | Which step of the bounded ladder answered: `structured`, `structured_no_city`, `freeform`, `freeform_no_city`. |
| `geocode_precision` | `address` \| `street` \| `locality` \| `unknown`. A `street` result means the house number could not be resolved. |
| `geocode_ambiguous`, `geocode_ambiguity_spread_m` | Distance between the two best-ranked candidates; `> 150 m` ⇒ ambiguous. |
| `geocode_postcode_match`, `geocode_city_match`, `geocode_name_operator_match` | Consistency of the returned address against the observed one, and whether the returned feature carries the operator's name. |
| `geocode_result_count`, `geocode_place_rank`, `geocode_importance`, `geocode_osm_type`, `geocode_osm_id`, `geocode_display_name`, `geocode_match_type` | Raw service outputs, retained for audit. |
| `geom` | `geometry(POINT, 25832)` — the projected point. CHECK: SRID = 25832 and not empty. |
| `x_25832`, `y_25832` | Scalar easting/northing, for convenience and for cheap verification. |

### 3.5 Building footprint (`inferred` from OSM)

| Column | Notes |
|---|---|
| `building_source`, `building_source_url`, `building_feature_id` | `openstreetmap`, the feature permalink, and `way/<id>` or `relation/<id>`. |
| `building_match_method` | `point_in_building` \| `nearest_building_le_30m` \| `nearest_building_le_80m`. |
| `building_match_confidence` | `high` \| `medium` \| `low` \| `none` — **geometric** association only. Capped at `low` when the geocode precision is not `address`. |
| `building_distance_m` | Point-to-polygon distance in metres (EPSG:25832). `0` means containment. |
| `building_footprint_area_sqm` | `ST_Area` in EPSG:25832. **Gross roof footprint, not usable PV area.** |
| `building_geometry_validity` | `valid` \| `repaired` (`shapely.make_valid()` was applied). |
| `building_geom` | `geometry(MULTIPOLYGON, 25832)`. CHECK: SRID, non-empty, `ST_IsValid`. |
| `building_tags` | The OSM tags of the matched feature, verbatim. |

A CHECK constraint enforces `(building_geom IS NULL) = (building_footprint_area_sqm IS NULL)`
— an area can never exist without the geometry it was computed from.

### 3.6 Parking (`inferred` from OSM)

Same shape as the building columns, plus:

| Column | Notes |
|---|---|
| `parking_features_within_radius` | Count of `amenity=parking` polygons inside the search radius. |
| `parking_area_total_sqm` | Sum of their areas — **parking supply in the vicinity, not operator-owned capacity**. |
| `parking_ownership_verified` | `boolean NOT NULL DEFAULT false`. Always `false`: no ownership source was consulted or available. |

### 3.7 Status and audit

`osm_operator_tag_match`, `osm_car_wash_feature_id`, `enrichment_status`
(`enriched` \| `partial` \| `point_only` \| `failed` \| `not_attempted`),
`geometry_provenance` (JSONB: provider, licence, radii, candidate counts,
match method, `ownership_verified: false`), `data_quality_flags` (JSONB array),
`notes`, `row_hash`, `collection_run_id`, `load_run_id`, `first_seen_at`,
`last_seen_at`, `updated_at`.

`enrichment_status` definitions:

| Value | Meaning |
|---|---|
| `enriched` | A building **and** a parking polygon were matched. |
| `partial` | One of the two was matched. |
| `point_only` | Geocoded, but no polygon met the matching rules (or the geocode was too coarse to attribute one). |
| `failed` | No usable coordinate, so no spatial work was attempted. |
| `not_attempted` | Row loaded before the enrichment stage ran. |

## 4. Constraints and indexes

* `PRIMARY KEY (poi_uid)`; `UNIQUE (source_uid)`; `UNIQUE (dedup_key)`.
* `CHECK` on: postal-code format `^[0-9]{5}$`, `country = 'DE'`, the four
  status/confidence enumerations, latitude/longitude ranges, non-negative
  areas, SRID = 25832 for all three geometry columns, non-empty geometries,
  `ST_IsValid` for both polygon columns, and area-requires-geometry.
* GiST indexes on `geom`, `building_geom`, `parking_geom`; btree on
  `operator_slug`, `postal_code`, `geocode_status`.

## 5. Upsert semantics

```sql
INSERT INTO iris_staging.commercial_poi_staging (...) VALUES (...)
ON CONFLICT (poi_uid) DO UPDATE SET ...,
    last_seen_at = now(), updated_at = now()
  WHERE commercial_poi_staging.row_hash <> excluded.row_hash
RETURNING poi_uid, (xmax = 0) AS was_insert;
```

* conflict target `poi_uid` (stable) ⇒ re-running never duplicates;
* the `WHERE row_hash <> excluded.row_hash` guard ⇒ an unchanged row is not
  rewritten, so a second identical run reports 0 inserts and 0 updates;
* `RETURNING (xmax = 0)` ⇒ inserts and updates are *measured*, not assumed;
* `first_seen_at` is excluded from the update set and therefore preserved;
* `row_hash` excludes timestamps and run ids so they cannot cause false
  "changed" verdicts.

## 6. Open questions for Tariq

1. Authoritative DDL and column names for `commercial_poi_staging`.
2. Primary-key type and whether `dedup_key` uniqueness is wanted at the
   database level (it currently makes a colliding address fail loudly).
3. Whether `iris_staging` already holds a POI category/operator dimension that
   `operator_slug` should reference instead of being free text.
4. Whether raw observations and run logs should live in `iris_staging` or in a
   separate audit schema.
5. Expected `geometry_provenance` / `data_quality_flags` encoding (JSONB here;
   `text[]` is equally easy if that is the house style).
6. Whether a POI with no usable coordinate should be loaded at all (this
   pipeline loads it with `NULL` geometry and an explicit status, so the
   discovered location is not silently lost).
