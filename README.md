# TASK-CRE-41 — Scrape & Geocode High-Throughput Car Wash Chains (Mr. Wash & CleanCar)

Project IRIS, Wave 4 · Category: commercial footprints and POIs
Target table: `iris_staging.commercial_poi_staging`

A reproducible pipeline that discovers every location published by two German
car-wash operators, geocodes it, attaches OpenStreetMap building and parking
geometries where they can be defended, and loads the result idempotently into
PostgreSQL 16 / PostGIS 3.4 — **writing only to `iris_staging`**.

> **Read [`LIMITATIONS.md`](LIMITATIONS.md) before using the output.** In
> particular: a building footprint is not usable PV area, proximity is not
> ownership, and the official target DDL was not supplied (see
> [`docs/SCHEMA_MAPPING.md`](docs/SCHEMA_MAPPING.md)).

---

## 1. Quick start (Docker — nothing but Docker required)

```bash
git clone <repo> && cd iris-cre41
cp .env.example .env            # local throwaway credentials; edit IRIS_USER_AGENT
docker compose up -d db         # PostgreSQL 16 + PostGIS 3.4
docker compose build pipeline

# 0) offline end-to-end smoke test: no network, no database
docker compose run --rm pipeline smoke-test --skip-load

# 1) create the staging schema (only ever touches iris_staging)
docker compose run --rm pipeline init-db

# 2) run the whole pipeline against the live sources (~45 min, rate-limited)
docker compose run --rm pipeline run-all

# 3) evidence: read-only verification queries + output samples
docker compose run --rm pipeline verify
docker compose run --rm pipeline samples
```

`verify` writes `samples/db_verification.txt` (human-readable) and
`samples/db_verification.json`.

### Local Python instead of Docker

Python 3.12+ is needed for the pinned lock (`pyproj==3.8.0` declares
`Requires-Python >= 3.12`). The package itself supports 3.11 — pin
`pyproj==3.7.2` if a 3.11 runtime is mandatory. The Docker image uses 3.13;
this submission was executed on 3.14.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e .
cp .env.example .env
docker compose up -d db                      # or point DB_URI at your own PostGIS
export DB_URI='postgresql+psycopg://iris:iris_local_dev_only@localhost:55432/iris'

iris-cre41 init-db
iris-cre41 run-all
iris-cre41 verify && iris-cre41 samples
```

### Tests

```bash
pytest -q                                    # unit tests only; no network, no DB
DB_URI='postgresql+psycopg://iris:iris_local_dev_only@localhost:55432/iris' \
  pytest -q                                  # adds the integration tests
pytest -q -m "not integration"               # explicitly exclude them
```

Integration tests **skip with a stated reason** when `DB_URI` is missing or
unreachable — an unexecuted integration test can never look like a pass.

---

## 2. What it does, stage by stage

```
collect ─→ normalize ─→ geocode ─→ enrich ─→ load
   │          │            │          │        │
   │          │            │          │        └─ iris_staging.commercial_poi_staging
   │          │            │          └─ Overpass: building + parking polygons → EPSG:25832
   │          │            └─ Nominatim: WGS84 → validated → projected to EPSG:25832
   │          └─ deterministic address normalisation, stable ids, deduplication
   └─ official operator websites only
```

Each stage reads and writes a JSON artefact under `data/`, so any stage can be
re-run or inspected on its own:

| Stage | Command | Artefact |
|---|---|---|
| collect | `iris-cre41 collect [--operator mrwash\|cleancar\|all] [--limit N]` | `data/raw_locations.json` |
| normalize | `iris-cre41 normalize` | `data/normalized_locations.json` |
| geocode | `iris-cre41 geocode` | `data/geocoded_locations.json` |
| enrich | `iris-cre41 enrich` | `data/enriched_pois.json` |
| load | `iris-cre41 load` | rows in `iris_staging` |
| everything | `iris-cre41 run-all [--skip-load]` | `data/pipeline_stats.json` |
| evidence | `iris-cre41 verify` | `samples/db_verification.{txt,json}` |
| samples | `iris-cre41 samples` | `samples/*.csv`, `samples/*.json` |
| DDL | `iris-cre41 emit-ddl --out sql/001_init_staging.sql` | SQL |
| smoke | `iris-cre41 smoke-test [--skip-load]` | offline run report |

Logs are JSON lines on stderr (`--log-text` for prose, `--log-level DEBUG` for
per-request detail).

### 2.1 Sources (verified 2026-10-31)

**Mr. Wash** — `robots.txt` is empty (no restrictions).
`/de/frontend/default/locations` is a server-rendered directory of all
36 branches, each linking to `/de/frontend/branch?id=<n>` which carries the
authoritative "Anschrift" block, phone, e-mail, opening hours and vehicle entry
limits. The id sequence has gaps (10, 11, 17–19, 39, 41, 42); each was probed
and returns 404, so the directory is exhaustive.

**Clean Car** — `robots.txt` disallows only `/wp-admin/`. Location pages are
discovered by unioning `/standorte/` (47 links) with
`/wp-sitemap-posts-page-1.xml` (48 links) — one page appears only in the
sitemap, so both are consulted. Each `/standorte/<slug>/` page carries the
address either as `<p>Street 12<br/>12345 City</p>` or as two sibling `<p>`
elements, so parsing works on the **visible text line stream** rather than on
CSS class names.

Access etiquette, applied to every outbound request: identifying User-Agent,
hard timeout, bounded exponential backoff with jitter, per-host minimum
interval, and each page fetched at most once per run.

### 2.2 Normalisation and identity

Pure functions only — same input, same output, no clock, no network:

* umlaut folding (`ä→ae`, `ß→ss`), street-type expansion (`Str.`→`strasse`) and
  suffix gluing, so `Breslauer Str.`, `Breslauer Straße`,
  `Winterbeker Weg` and `Winterbekerweg` all canonicalise consistently;
* house numbers split off the street line and normalised (`93 - 95` → `93-95`,
  `10 a` → `10a`, en-dashes folded);
* `source_uid` = `<operator>:<source system>:<source record id>`;
  `poi_uid` = `cre41-` + `sha1(source_uid)[:20]` — stable across runs;
* `dedup_key` = `sha1(operator + postcode + normalised street + number)`,
  **operator-scoped** so two operators can share a retail-park address.

Deduplication groups by `dedup_key`, keeps the most complete record (ties break
on `source_uid`, so the choice is order-independent) and retains the dropped
records in `commercial_poi_raw_staging` with `is_duplicate = true`.

### 2.3 Geocoding

OSM Nominatim, ≥ 1 req/s (floor cannot be configured below 1.0 s), identifying
User-Agent, SQLite response cache. A bounded 4-step ladder is tried only as far
as needed, and the step that answered is recorded:

1. structured (`street` = `<no> <street>`, `postalcode`, `city`)
2. structured without `city`
3. free-form `"<street> <no>, <plz> <city>, Deutschland"`
4. free-form without the city

Every result carries `status`, `precision` (`address`/`street`/`locality`),
`ambiguity_spread_m` (distance between the two best-ranked candidates;
`> 150 m` ⇒ `ambiguous_resolved`), and consistency flags for postcode, city and
operator name. The **original WGS84 coordinate is stored separately** from the
projected one. A failure stores `NULL` plus a status — never an approximation.

### 2.4 CRS handling

`pyproj` with `always_xy=True` everywhere. Source CRS is validated explicitly
against an allow-list (`EPSG:4326`, `EPSG:25832`); anything else raises
`UnsupportedCRSError` rather than being assumed. Coordinates are range-checked
(including NaN and the `(0, 0)` null island), projected to **EPSG:25832**, and
the projected value is envelope-checked. Areas and distances are computed only
after confirming the target CRS is metric.

### 2.5 Spatial matching rules

Deterministic, ordered, first hit wins. All distances in EPSG:25832 metres.

| Rule | Condition | Method | Confidence |
|---|---|---|---|
| B1 | point inside a `building` polygon | `point_in_building` | `high` |
| B2 | nearest `building` ≤ 30 m | `nearest_building_le_30m` | `medium` |
| B3 | nearest `building` ≤ 80 m (radius) | `nearest_building_le_80m` | `low` |
| P1 | point inside an `amenity=parking` polygon | `point_in_parking` | `high` |
| P2 | nearest parking ≤ 15 m | `adjacent_parking_le_15m` | `medium` |
| P3 | nearest parking ≤ 120 m (radius) | `nearby_parking_le_120m` | `low` |

* **Confidence describes geometry, not business reality.** `high` means the
  point falls inside that polygon — nothing about ownership or tenancy.
* Confidence is **capped at `low`** when the geocode precision is not
  `address`, and polygon matching is **skipped entirely** for `locality`
  precision: attributing a footprint to a town centroid would invent a fact.
* `osm_operator_tag_match` records the only operator-level corroboration OSM
  offers (a matching `amenity`/`shop=car_wash` tag or the operator's name on
  the feature). It is a hint, not evidence.
* Geometry is validated with Shapely: `make_valid()` repairs are recorded as
  `*_geometry_validity = 'repaired'`; empty and non-polygonal geometry is
  rejected; `relation` multipolygons are assembled by stitching member ways
  into rings with inner rings assigned to their containing outer ring.
* A site that cannot be polygonised **keeps its point geometry** and leaves
  every unsupported metric `NULL`.

### 2.6 Persistence and staging isolation

Three independent layers keep writes inside `iris_staging`:

1. **Restricted `search_path`** — every connection is pinned to
   `iris_staging, public` (`public` only so PostGIS `ST_*` functions resolve;
   no project table lives there).
2. **Statement guard** — a `before_cursor_execute` hook inspects every
   statement and raises `StagingIsolationError` unless each write target is
   schema-qualified to `iris_staging`. Unqualified writes are refused rather
   than trusted to `search_path`; `CREATE EXTENSION/ROLE/DATABASE` is refused
   outright; string literals and comments are stripped first so SQL cannot be
   smuggled through them. Reads anywhere are allowed (upstream data is
   read-only by design).
3. **Least-privilege role** — [`sql/002_least_privilege_role.sql`](sql/002_least_privilege_role.sql)
   creates a loader role that owns `iris_staging`, has `USAGE` but **not**
   `CREATE` on `public`, and no rights on `iris_core`-style schemas.

The pipeline deliberately **never** installs PostGIS: `CREATE EXTENSION` is a
privileged cross-schema operation, so a missing extension fails loudly with
DBA instructions instead.

Upsert: `ON CONFLICT (poi_uid) DO UPDATE ... WHERE row_hash <> excluded.row_hash`,
`RETURNING (xmax = 0)` so inserts and updates are measured rather than assumed.
`first_seen_at` is preserved; `row_hash` excludes timestamps and run ids.

---

## 3. Configuration

All configuration is environment-driven; see [`.env.example`](.env.example).
`DB_URI` is mandatory for database commands and is never hard-coded.

| Variable | Default | Notes |
|---|---|---|
| `DB_URI` | — | `postgresql+psycopg://user:pass@host:port/db` |
| `IRIS_STAGING_SCHEMA` | `iris_staging` | The only writable schema |
| `IRIS_USER_AGENT` | identifying string | **Put a real contact in it.** Nominatim answers HTTP 403 to library defaults *and* to placeholder contacts containing `example.org` (verified 2026-10-31) |
| `IRIS_GEOCODER` | `nominatim` | `static` for a no-network test double |
| `IRIS_NOMINATIM_MIN_INTERVAL` | `1.1` | Clamped to ≥ 1.0 s by the usage policy |
| `IRIS_OVERPASS_MIN_INTERVAL` | `2.0` | Clamped to ≥ 1.0 s |
| `IRIS_BUILDING_SEARCH_RADIUS_M` | `80` | Candidate search radius |
| `IRIS_PARKING_SEARCH_RADIUS_M` | `120` | Candidate search radius |
| `IRIS_HTTP_TIMEOUT` / `IRIS_HTTP_MAX_RETRIES` | `30` / `3` | Per request |
| `IRIS_LOG_LEVEL` / `IRIS_LOG_JSON` | `INFO` / `1` | Structured logging |

---

## 4. Repository layout

```
├── src/iris_cre41/
│   ├── cli.py                 CLI entry points
│   ├── config.py              environment-driven settings
│   ├── crs.py                 CRS validation + EPSG:25832 projection
│   ├── httpclient.py          timeouts, bounded retries, rate limiting
│   ├── logging_setup.py       JSON-lines structured logging
│   ├── models.py              stage-to-stage data contracts
│   ├── normalize.py           pure address normalisation + dedup
│   ├── pipeline.py            stage orchestration + counts
│   ├── reporting.py           output samples + DB evidence queries
│   ├── smoke.py               offline end-to-end run
│   ├── collectors/            mrwash.py, cleancar.py, base.py
│   ├── geocode/               nominatim.py, cache.py, base.py
│   ├── enrich/                overpass.py, geometry.py, matching.py
│   └── db/                    schema.py, guard.py, engine.py, loader.py
├── sql/                       001_init_staging.sql, 002_least_privilege_role.sql
├── tests/                     unit + integration + offline smoke; fixtures/
├── tools/capture_fixtures.py  reproducible fixture capture with provenance
├── samples/                   output samples and execution evidence
├── docs/                      SCHEMA_MAPPING.md, EXECUTION_EVIDENCE.md
├── Dockerfile  compose.yaml  .env.example
└── LIMITATIONS.md
```

Fixtures are real captured pages. `tests/fixtures/MANIFEST.json` records each
file's source URL, HTTP status, byte size and the **sha256 of the unmodified
response**; the stored HTML has `<script>/<style>/<link>/<svg>/<noscript>` and
comments removed (the parsers discard them anyway), which cuts size by ~85 %
while leaving the parsed DOM untouched. Regenerate with
`python tools/capture_fixtures.py`.

---

## 5. Data licensing and attribution

* Location names and addresses: published by **Mr. Wash Autoservice AG** and
  **Clean Car Autowaschanlagen GmbH & Co. KG** on their own websites.
* Geocodes and all building/parking geometry: **© OpenStreetMap contributors,
  ODbL 1.0** (via Nominatim and Overpass). Attribution is mandatory for any
  downstream publication, and ODbL share-alike obligations attach to derived
  databases.

No production credentials and no client portfolio data are present in this
repository. `.env` is git-ignored; the values in `.env.example` and
`compose.yaml` are throwaway local-development ones.
