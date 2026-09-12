# Data sources and provenance

HouseHunter v1 pins data vintages. It never discovers or accepts an automatic “latest”
release. A source update requires review, validation, and a HouseHunter release.

## FEMA National Risk Index tracts

- Dataset: National Risk Index Census Tracts
- Release: v1.20, December 2025
- ArcGIS item: `9da4eeb936544335a6db0cd7a8448a51`
- Requested fields: `TRACTFIPS`, `ALR_NPCTL`, `NRI_VER`, and the 18 published `{CODE}_ALR_NPCTL` doubles (`AVLN`, `CFLD`, `CWAV`, `DRGT`, `ERQK`, `HAIL`, `HWAV`, `HRCN`, `ISTM`, `LNDS`, `LTNG`, `IFLD`, `SWND`, `TRND`, `TSUN`, `VLCN`, `WFIR`, `WNTW`)
- Geometry: requested only by the maintainer map-asset generator; runtime preparation
  and ranking never request geometry
- Metric: composite Expected Annual Loss Rate national percentile (`ALR_NPCTL`). Hazard columns are the same layer's published `{CODE}_ALR_NPCTL` values, used for inspect/detail/export only.
- Cache: `data/cache/fema_nri_tracts.parquet`

Official references: [FEMA technical documentation](https://www.fema.gov/sites/default/files/documents/fema_national-risk-index_technical-documentation.pdf),
[FEMA ArcGIS item](https://www.arcgis.com/home/item.html?id=9da4eeb936544335a6db0cd7a8448a51&sublayer=0),
and [FEMA data resources](https://hazards.fema.gov/nri/data-resources).

## FEMA National Risk Index counties

- Dataset: National Risk Index Counties
- Release: v1.20, December 2025
- ArcGIS item: `39485e8035d446a5bff03259508ae355`
- Requested fields: `STCOFIPS`, `COUNTY`, `COUNTYTYPE`, `STATEABBRV`, `ALR_NPCTL`, `NRI_VER`, and the same 18 `{CODE}_ALR_NPCTL` doubles as the tract layer
- Geometry: requested only by the maintainer map-asset generator; runtime preparation
  and ranking never request geometry
- Metric: FEMA's published **county** `ALR_NPCTL`, ranked among counties. County hazard percentiles come from this layer, not from averaging tracts.
- Cache: `data/cache/fema_nri_counties.parquet`

County names and the county ranking come from this layer. Tract rows join
`TRACTFIPS[:5]` to `STCOFIPS`. HouseHunter does **not** average tract percentiles to
produce a county score. Tract and county percentiles are not comparable.

## CHR&R Community Conditions

- Dataset: County Health Rankings & Roadmaps 2025 county layer
- Release: 2025 Annual Data Release
- ArcGIS layer: `County Health Rankings 2025/FeatureServer/2`
- Requested fields: `fipscode`, `county`, `state`, `CommunityConditions_Group`
- Pinned rows: 3,144 county/county-equivalent records
- Metric: official Community Conditions Health Group, integer 1–10 or null
- Canonical logical SHA-256: `516e1e1408fe3dbb65273c9de75c67cfd6d5eed15f1ad18a8e91e6c6d54ca3fb`
- Raw cache: an atomic `data/raw/chrr/current.json` pointer selects a verified
  generation containing `community_conditions_2025.json` and `metadata.json`;
  legacy flat caches remain readable and are migrated on refresh
- Processed artifact: `data/processed/chrr_county.parquet`

Group 1 represents the healthiest community conditions and Group 10 the least
healthy. These are unequal, data-driven clusters—not percentiles. HouseHunter does
not calculate `qol_sort_score`, rebuild the group from the 24 measures, or blend it
with FEMA. Tracts inherit the county value through their first five FIPS digits and
all user-facing surfaces label it county-level.

The runtime uses the public ArcGIS county representation because the 2025 analytic
CSV does not publish `CommunityConditions_Group`. Download validates the pinned
layer, schema, data edit timestamps, exact row count, unique zero-padded FIPS,
1–10/null domain, and canonical checksum before atomically publishing canonical JSON.
The browser never contacts CHR&R.

Official references: [CHR&R Data & Documentation](https://www.countyhealthrankings.org/health-data/methodology-and-sources/data-documentation),
[CHR&R methods](https://www.countyhealthrankings.org/health-data/methodology-and-sources/methods),
and the [official ArcGIS county layer](https://p3eplmys2rvchkjx.svcs.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/County%20Health%20Rankings%202025/FeatureServer/2).

If CHR&R publication moves after funding ends, update the `chrr` block in
`config/sources.yml` only after verifying the replacement is official and semantically
identical. Recompute and review the schema fingerprint, revision pins, row count,
spot checks, and canonical logical checksum; never repoint automatically to “latest.”

## Mountain Score sources and release contract

Mountain Score is a separate, derived contextual layer. Its maintainer build accepts:

- USGS 3DEP elevation rasters;
- PAD-US 4.1 polygons with an access classification field;
- a national hiking-trail line layer;
- 2020 Census blocks with 15-digit GEOID, `POP20`, geometry, and one internal point.

These large inputs are neither bundled nor fetched by `./scripts/run-app`. A reviewed
JSON source lock v2 supplies the exact HTTPS URL or local path, byte size, SHA-256,
acquisition date, actual CRS/schema/count, and filename for every file. It also pins
reviewed HTTPS hosts, ordered elevation precedence, the exact sorted national block
GEOID digest, per-state block/population expectations, a block-driven tile inventory,
and a storage projection. Production preparation rejects an alias or incomplete region
CRS: CONUS/DC is EPSG:5070, Alaska is EPSG:3338, and Hawaii is a reviewed fixed
equal-area WKT. Region configuration names only locked files.

`househunter mountain inventory` reads only the normalized Census block/internal-point
inputs and emits the exact signed tile coordinates, per-tile counts/population, all-state
expectations, sorted GEOID digest, and a conservative pack/release/work high-water
projection. Review and copy those values into source-lock v2 before acquiring the much
larger elevation and access inputs.

`househunter mountain download` disables ambient proxies, permits only the lock's
reviewed HTTPS hosts, revalidates every redirect, checks declared length while
streaming, verifies bytes before atomic publication, and maintains the unrelated-disk
reserve. `househunter mountain prepare` uses 100 km processing cores with exact 100 km
halos on the CRS-origin 250 m lattice. Every logical tile stores float32 elevation,
uint8 PAD codes, float32 additive trail-hit cells, and int32 block sample indexes. The
pack manifest checksums every file and every tile's canonical raw-metric result; a
separate lock anchors the content-addressed pack.

ZIP inputs require a lock entry for every member, its size and SHA-256, the exact total
expanded size, an extraction root, and the dataset paths consumed by region configuration.
Extraction rejects absolute/parent/backslash paths, links, devices, encryption, nested
archives, duplicate or case-colliding entries, unlisted files, more than 200,000 members,
over 30 GB expanded, or a member compression ratio above 100:1. Extracted inputs are
published atomically. A default managed staging directory is removed only after the
derived prepared pack and its external lock have both validated; explicit source roots
are never cleanup targets.

`househunter mountain build --source-lock ... --prepared-pack ... --prepared-lock ...
--workers 4` verifies both independent locks and the pack, runs
one bounded persistent process pool, writes atomic per-tile raw Parquet shards, and
assembles them in canonical tile/GEOID order. Percentiles remain one national
population-weighted lower-rank ECDF per component; no tile or state percentile or
aggregate is cached. A generated candidate is written inside the release filesystem,
validated once by reconstructing block percentiles/composite and tract/county
aggregates, renamed to its content identity, and then exposed through the Mountain
pointer. The timed path finishes only after the normal HouseHunter snapshot is rebuilt
and queryable. It also publishes one content-addressed compact fallback under
`data/mountain/compact/`. Failure restores the prior full and compact Mountain pointers
and leaves the prior app snapshot intact.

The prepared pack is capped at 22 GB; work shards and any one full release at 4 GB;
active plus rollback releases at 8 GB. The implementation targets 45 GB and hard-stops
before 50,000,000,000 allocated bytes while retaining at least 10 GB of unrelated free
space. Cleanup is limited to direct, marked, non-symlink children of managed Mountain
work/release/prepared/compact directories. Successful preparation retains only the new
prepared pack; successful publication retains one compact fallback. The compact
tract/county fallback must remain below 50 MiB.
The normal snapshot reads only tract/county Parquet and the manifest, so Rasterio,
Shapely, PyProj, SciPy, Pyogrio, and NumPy are optional maintainer dependencies.

The approximation is explicit:

- terrain relief uses focal maxima minus minima at 5, 10, 20, and 40 km;
- a mountain cell has slope at least 15 degrees or 5 km relief at least 300 m;
- ruggedness is the mountain-cell fraction within 20 km;
- open public mountain area uses 0–5, 5–15, and 15–30 km ring weights of 1, 0.75,
  and 0.4; restricted, closed, and unknown land are reported separately;
- trail access records nearest mapped mountain trail and cumulative mapped length
  within 10 and 25 km, with raw access `km_10 + 0.4 * km_10_to_25`;
- component percentiles use a national, population-weighted lower-rank ECDF over
  in-scope blocks with valid DEM cells; population-zero blocks are not calibrators;
- the final block score is `0.45 relief + 0.20 rugged + 0.20 public access + 0.15 trail`;
  tract and county outputs are population-weighted block aggregates.

The manifest records the source lock, pipeline and score versions, release identity,
coverage, row counts, checksums, and whether national completeness was proven. The
validator recomputes release identity, hashes, block domains, every component
percentile and composite from rounded block raw values, tract/county aggregates, and
coverage semantics before a release can be promoted. A promoted release changes
the main HouseHunter build identity; an absent release produces null Mountain fields
with `unavailable` status rather than silently substituting zero.

Source-lock v2 validation reads the real raster/vector/Parquet metadata and requires
its CRS, layer, exact schema, and record/cell count to equal the reviewed contract.
Region configuration must consume every locked GIS dataset in the correct family and
layer, preserve DEM precedence, and assign Alaska, Hawaii, and CONUS/DC Census blocks
to their fixed projected-CRS partitions. Block Parquet is accepted only for the legacy
raw-block path; it cannot stand in for DEM, PAD-US, or trail inputs.

## Download contract

The downloader validates each source's edit timestamp, field names and ArcGIS types,
release label, unique identifiers, row count, composite range `[0, 100]`, optional
hazard percentiles that are **null or** finite `[0, 100]`, and the canonical
logical checksum in `config/sources.yml` when one is pinned. Pages are cached
independently under a directory that includes `schema_fingerprint`, so expanding
`outFields` cannot reuse an older page cache. Publication uses a temporary file
and atomic rename. The local source
manifest records retrieval time, logical and physical SHA-256 checksums, row count,
schema fingerprint, source URL, and terms URL for each source. On a verified cache
hit, `download` validates that manifest against the cached Parquet and reconstructs
missing or corrupt manifest metadata from the cache file timestamp. If the final
Parquet is corrupt, the pinned remote contract and revision-specific page cache are
used to rebuild it atomically; remote schema or version drift still stops the operation.

State abbreviations for tracts are derived from the first two `TRACTFIPS` digits using
a bundled map. Unknown prefixes are retained as `??` / `Unknown` and are excluded from
known `--state` scopes.

## Derived map assets

The maintainer-only `scripts/generate_map_assets.py` requests polygon geometry from the
same pinned FEMA FeatureServer layers after validating their item IDs and item/data/layer
edit timestamps. It verifies exact equality with the cached attribute identifier sets:
85,154 unique tract FIPS and 3,232 unique county FIPS across the 50 states, DC, Puerto
Rico, US Virgin Islands, Guam, American Samoa, and Northern Mariana Islands.

Only deterministic, quantized, content-addressed TopoJSON gzip files and their manifest
are committed under `src/househunter/map_assets/`. The package includes a simplified
national tract topology, detailed tract topologies loaded by jurisdiction after zoom, a
national county topology, and state/territory outlines and labels. Raw downloads remain
ignored under `data/cache/map-geometry/`. Geometry never contains scores: the active local
snapshot is the sole score source. At startup the loopback server validates the manifest,
pinned revisions, complete national/detail inventory, feature identities, sizes, SHA-256
digests, gzip streams, and polygonal TopoJSON before advertising the map as ready. Reusing
ignored raw geometry also requires matching recorded revisions and file digests.

## Address lookup

On explicit user action (`househunter lookup` or Find tract in the map Search
panel), the loopback server calls the public
[Census geocoder](https://geocoding.geo.census.gov/geocoder/) with
`vintage=Census2020_Current` and no API key. The Search panel accepts a street
address only; apartment and unit suffixes are stripped before either geocoder.
If that Census response is valid and contains zero `addressMatches`, the server
may then query public
[Nominatim](https://nominatim.openstreetmap.org/) (`format=jsonv2`, 1 request/second,
identifying User-Agent). Accepted coordinates are converted through Census
`geographies/coordinates` (`Census2020_Current`) so the tract GEOID stays a 2020
Census identifier. The browser never contacts census.gov or nominatim.openstreetmap.org.
Addresses are not written under `data/`. Prepare, download, and build still do not
use these services.

House/building Nominatim results resolve automatically only when the returned house
number agrees with the query. Road matches are approximate: the UI requires
“Use approximate street location”, and the CLI requires `--allow-approximate`.
Street names must be spelled correctly; a complete Census outage is not covered.

Set `HOUSEHUNTER_NOMINATIM_URL` to a HTTPS Nominatim endpoint, or `off` to disable
fallback without a software release. OpenStreetMap data is © OpenStreetMap
contributors (ODbL).

The returned 11-digit GEOID is the HouseHunter tract `place_id`. The score remains
that tract's published FEMA `ALR_NPCTL`, not a property-level rating. The public
Census geocoder only matches streets in its address-range file, so new
subdivisions often need the Nominatim street confirmation.

## Census reference assets

Runtime ranking does **not** download or require Census Place/housing assets.
`./scripts/run-app` uses only the pinned FEMA and CHR&R caches.

Maintainer-only Place-generation scripts remain in the tree for historical release assets
and are unused by the local app. Prepare, download, and build never contact census.gov.

## Limitations

The score is FEMA's published composite percentile for that geography, not risk at a
specific address. A percentile is a national relative ranking, not a probability or
expected dollar loss. The composite combines FEMA consequence types and should not be
read as one particular hazard. The 18 `{CODE}_ALR_NPCTL` values are FEMA's published
percentiles at the same grain; HouseHunter does not blend them. Null means FEMA
published no rating, never zero. County scores and county hazard percentiles are not
a summary of the tracts inside the county.
Mountain Score is an approximate regional context metric, not a parcel or address
assessment. Coarse cells and source completeness can miss narrow ridges, informal or
unmapped trails, seasonal closures, entrances, legal access details, travel time, and
views. The land rings measure classified mountain area around a block point, not a
guaranteed route from that point. Release-to-release comparisons require review of
source and score versions; raw values and percentiles may move when source coverage or
the national reference population changes.
National tract boundaries are simplified for overview rendering, so fine boundary detail
appears only after zoom loads the jurisdiction asset. Small urban tracts can be subpixel at
the national extent; they are not aggregated or enlarged.
