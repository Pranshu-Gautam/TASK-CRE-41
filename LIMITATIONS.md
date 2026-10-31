# TASK-CRE-41 — Limitations

Written to be read before the output is used for anything. Counts refer to the
executed run documented in [`docs/EXECUTION_EVIDENCE.md`](docs/EXECUTION_EVIDENCE.md).

---

## 1. Missing official database DDL

The official DDL for `iris_staging.commercial_poi_staging` was **not provided**
with the task. This submission defines its own local staging schema
([`sql/001_init_staging.sql`](sql/001_init_staging.sql), mapped in
[`docs/SCHEMA_MAPPING.md`](docs/SCHEMA_MAPPING.md)) and does **not** claim it is
the production schema.

**Consequence:** the load is reproducible against *this* schema only. Column
names, the primary-key type, and whether the two auxiliary tables belong in
`iris_staging` all need confirmation from Tariq (six open questions are listed
at the end of the mapping document). Expect a rename/retype migration step
before any shared-environment load.

## 2. Collection coverage

**Mr. Wash** — the official directory (`/de/frontend/default/locations`) lists
36 branches and all 36 were collected. The branch-id sequence has gaps
(10, 11, 17–19, 39, 41, 42); every gap was probed individually and returns
HTTP 404, so the directory is exhaustive rather than paginated. There is no
public API, sitemap, or JSON endpoint.

**Clean Car** — 48 location pages were discovered by unioning the `/standorte/`
index (47 links) with `wp-sitemap-posts-page-1.xml` (48 links); one page appears
only in the sitemap. All 48 were collected.

**Consequence and residual risk:**

* Coverage is *as published*. A site that exists but is absent from both the
  index and the sitemap is invisible to this pipeline. No third-party directory
  (Google Places, OSM, Yelp) was used to cross-check the operator's own count,
  so "36" and "48" are the operators' published figures, not an independently
  verified ground truth.
* Both sites are server-rendered HTML with no stable contract. A redesign
  breaks the parsers. The parsers key on text structure (an address block
  anchored by a `12345 City` line) rather than CSS classes, which survives
  cosmetic changes but not a restructuring.
* Clean Car slugs such as `bad-oldesloe-2`, `eckernfoerde-2` and `strahlsund2`
  are *separate real sites*, not duplicates — verified by distinct street
  addresses. Deduplication is therefore address-based, not slug-based.
* **One Clean Car page publishes no street address at all** (`/standorte/glinde/`,
  heading "Glinde/Reinbeck"): the page carries the service list and prices but
  no postal address. It is stored with `NULL` address, `geocode_status = error`
  and the flags `address_block_not_found`, `street_missing`, `city_missing`,
  `postal_code_missing`. **No city centroid was substituted**, because a town
  centroid is not a site location.

## 3. Website access terms

`robots.txt` was fetched for both hosts before any crawling.
`www.mrwash.de/robots.txt` is empty (no restrictions);
`www.clean-car.com/robots.txt` disallows only `/wp-admin/` and advertises the
sitemap used here. Access is limited to plain `GET` requests of publicly served
pages, one request per second, with an identifying User-Agent, and each page is
fetched at most once per run.

**Consequence:** this is a lawful-access reading of the published terms, not
legal advice, and not a licence to redistribute the operators' content. The
collected addresses are factual business data; the surrounding page text
(prices, programme descriptions) is the operators' copyright and is stored only
as provenance, not republished.

## 4. Geocoding uncertainty — the largest source of positional error

Geocoding uses **OSM Nominatim** (public instance) under its usage policy:
≥ 1 request/second (the configured floor cannot be set below 1.0 s), an
identifying User-Agent, and an on-disk cache so repeat runs do not re-query.

| Issue | How it is handled | Residual risk |
|---|---|---|
| Not every address resolves to a building | `geocode_precision` records `address`/`street`/`locality`/`unknown` | A `street` result sits on the road centreline, typically 10–40 m from the entrance |
| The service may return several candidates | Distance between the two best-ranked candidates is stored; `> 150 m` ⇒ `ambiguous_resolved` | The *chosen* candidate can still be wrong; `geocode_ambiguity_spread_m` says how wrong it could be |
| The returned address may not be the one asked for | `geocode_postcode_match`, `geocode_city_match` | A `false` flag is a warning, not a rejection — the row is kept and marked |
| The matched feature may belong to a *different* operator | `geocode_name_operator_match` | One Clean Car site resolves to an OSM feature named "A - Z Autowaschanlagen GmbH" at the same address |
| An abbreviated city defeats the structured query | Bounded 4-step ladder: structured → structured-without-city → free-form → free-form-without-city | More requests for hard addresses; every step is recorded in `geocode_strategy` |

**Nothing is ever substituted for a failed geocode.** A failure yields `NULL`
geometry plus a status, never an approximate coordinate.

**Verified operational note:** `nominatim.openstreetmap.org` answers **HTTP 403**
to library-default User-Agents (`python-requests/...`) *and* to placeholder
contacts containing `example.org`. `IRIS_USER_AGENT` must carry a real contact
before any sizeable run.

## 5. OSM geometry completeness

Building and parking polygons come from **OpenStreetMap via the Overpass API**.

* OSM coverage is **uneven and volunteer-maintained**. A missing footprint is
  common and is not an error: the row keeps its point geometry and leaves the
  area `NULL`.
* OSM is a moving target. The same query re-run next month can return a
  different polygon. `geometry_provenance` records the provider, the licence,
  the search radii, the candidate counts and the query timestamp so a result
  can be explained after the fact, but it is **not** a frozen snapshot.
* Only `way`/`relation` features with areal geometry are used. Nodes carry no
  footprint and are used only as tag-level corroboration.
* `relation` multipolygons are assembled by stitching member ways into rings
  (inner rings assigned to the containing outer ring). A relation whose members
  never close is rejected, not approximated.
* Invalid geometry is repaired with `shapely.make_valid()` and the repair is
  recorded in `*_geometry_validity = 'repaired'`. Empty geometry and
  non-polygonal types are rejected outright.
* Overpass is a shared free service. Observed latency during this run was
  **20–30 s per request**, so 83 sites take ~35 minutes at the configured 2 s
  interval. Responses are cached. For a materially larger input set the
  obvious change is one batched `around:` query per group of sites instead of
  one per site; it was not implemented here because per-site queries keep the
  provenance trail one-to-one.

**Licence:** OSM data (both the geocodes and the polygons) is
© OpenStreetMap contributors, **ODbL 1.0**. Attribution is mandatory for any
downstream publication, and ODbL share-alike obligations attach to derived
databases. The attribution string is stored in the enrichment artefact and in
`geometry_provenance`.

## 6. Building/parking matching uncertainty

The matching rules are fixed and documented (`src/iris_cre41/enrich/matching.py`,
summarised in the README). The important caveats:

* **`match_confidence` is a statement about geometry, not about the business.**
  `high` means "the geocoded point lies inside this polygon", nothing more.
* **Proximity does not prove ownership, tenancy, or any right of use.** This
  applies with full force to parking: `parking_area_total_sqm` is the parking
  supply found within the search radius, which may belong to a neighbouring
  supermarket. `parking_ownership_verified` is `NOT NULL DEFAULT false` and is
  always `false` in this output.
* A matched footprint may be **a whole retail building** that merely contains
  the car wash, or conversely the car-wash *site* polygon including forecourt
  and canopy rather than the roofed structure. One verified example: the Aachen
  Mr. Wash matches a 5 382 m² feature tagged `amenity=car_wash` + `building` —
  that is the site, not a 5 382 m² roof.
* Confidence is capped at `low` whenever the underlying geocode precision is
  not `address`, and polygon matching is **skipped entirely** for
  `locality`-precision geocodes, because attributing a footprint to a town
  centroid would manufacture a false association.
* `osm_operator_tag_match` is the only operator-level corroboration available
  and comes from volunteer-entered OSM tags. It is a hint, not evidence.

## 7. Roof area is not usable PV area

`building_footprint_area_sqm` is the **gross 2-D footprint** computed as
`ST_Area` in EPSG:25832. It is **not**:

* usable rooftop PV area — no deduction for roof pitch, orientation, plant,
  HVAC, skylights, walkways, setbacks, fire-service access, shading, or
  statics;
* an indication of roof type (flat vs pitched) or load-bearing capacity;
* a measure of available grid connection capacity.

No solar yield, BESS or HPC sizing figure is derived anywhere in this pipeline.
Converting footprint to installable kWp requires a structural and electrical
assessment that this data cannot support. Treat the footprint as a *screening
filter*, not an input to a yield model.

## 8. No verified property ownership, no valuation data

* **No ownership data.** No cadastral (ALKIS), land-registry (Grundbuch), or
  commercial-register source was consulted — none is freely available.
  A site's operator is known from the operator's own website; who owns the
  land or building is **unknown**, as is whether the site is owned or leased.
* **No lease or WALT data.** Nothing about lease expiry, break options, rent,
  or tenant covenant is available from public sources, so nothing is inferred.
* **No valuation data and no GRZ/GFZ assessment.** No ImmoWertV-compliant
  valuation input, no comparables, no `Bodenrichtwert`, and no
  `Bebauungsplan`/GRZ/GFZ figures were retrieved. Unused building rights
  **cannot** be computed from this output: that needs the municipal land-use
  plan for each parcel, plus parcel boundaries, neither of which is present.
* **Parcel geometry is absent**, so footprint-to-parcel ratios (the actual GRZ
  input) are not computable.

**Consequence:** this dataset answers "where are these car washes, how big is
the building OSM believes is there, and how confident is that link". It does
not answer any question about value, rights, or ownership.

## 9. Unverified external services

| Service | Role | Verification status |
|---|---|---|
| `nominatim.openstreetmap.org` | Geocoding | Community-operated, no SLA, no accuracy guarantee. Rate-limited, cached, every result flagged for consistency. Not independently validated against a reference geocoder. |
| `overpass-api.de` | OSM geometry | Community-operated, no SLA. Observed 20–30 s latency, intermittent `406`/`429` under load. Retried with bounded backoff; failures recorded as `overpass_request_failed`, never as "no building". |
| `www.mrwash.de`, `www.clean-car.com` | Primary source | The operators' own sites. Authoritative for their own locations; no availability guarantee and no change notification. |

No commercial geocoder was used, so there is **no cross-provider check** on any
coordinate. A second provider (or a manual spot-check against aerial imagery)
is the cheapest way to raise confidence and is the first thing worth adding.

## 10. Pipeline-level caveats

* **Not production-ready.** It is a reproducible assessment deliverable. What
  is missing for production: schema reconciliation (§1), a frozen OSM snapshot
  or change-detection strategy (§5), monitoring/alerting, a retry queue for
  partial failures, and a second geocoding provider for cross-validation.
* **Single-process, single-threaded** by design. 84 locations in ~45 minutes
  end-to-end, dominated by deliberate rate limiting. It will not scale to tens
  of thousands of POIs without batching.
* **Integration tests require a live database.** Without a reachable `DB_URI`
  they SKIP with an explicit "NOT executed" reason — they never silently pass.
  Unit tests never touch the network or the database.
* **Fixtures are point-in-time.** The captured HTML is dated in
  `tests/fixtures/MANIFEST.json` with the sha256 of each original response. A
  passing fixture test proves the parsers handle *that* markup, not today's.
* **The local database credentials in `.env.example` and `compose.yaml` are
  throwaway local values.** No production credential appears anywhere in this
  repository, and `.env` is git-ignored.
