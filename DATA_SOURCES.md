# Data sources and provenance

HouseHunter v3 pins data vintages. It never discovers or accepts an automatic “latest”
release. A source update requires review, validation, and a HouseHunter release.

## Cost of Living — BEA Regional Price Parities

HouseHunter pins the 2024 BEA metropolitan-area Regional Price Parities archive
in `config/sources.yml`. The map value is the all-items RPP, where the United
States equals 100; goods, housing rents, utilities, and other services are
retained for details and exports. Counties in metropolitan statistical areas
inherit that MSA's values. All other counties in the 50 states and District of
Columbia, including micropolitan counties, inherit BEA's single U.S.
Nonmetropolitan Portion value. Puerto Rico and other territories are outside
scope. A future annual release requires an explicit checksum and schema review.

The initial lock is `MARPP.zip`, 146,479 bytes, SHA-256
`5dbf2e6ac2af222cc9abc205586c9b480344d89392752eb689c3ec823a34c83e`.
The normalized table contains the exact MARPP line codes 1–5 and 387 MSAs plus
geography `00999`. Values are preserved as published; 80–120 is only a visual
map domain. BEA is optional: a network, checksum, schema, or cache failure emits
a source warning and stable null/status columns while the FEMA snapshot remains
publishable. There is no state fallback.

### Updating BEA RPP

1. Review BEA's release page and confirm that MARPP geography, line-code meaning,
   units, and the U.S. nonmetropolitan row are unchanged.
2. Download the candidate archive outside the repository. Record its release year,
   filename, bytes, SHA-256, contained table filename/bytes/header SHA-256, MSA count,
   and a canonical normalized checksum in `config/sources.yml`.
3. Re-run exact line-code, MSA/nonmetro, territory, county assignment, tract
   inheritance, schema-drift, checksum, and representative value tests from a clean
   runtime data root.
4. Review coverage against the pinned ACS county-to-CBSA asset, update spot checks and
   documentation, run both verification scripts, and ship the lock change only with a
   new HouseHunter release. Never select an annual release automatically.

## Ranking v2 — pinned public county bundle

Normal setup, `househunter top-counties`, County Fit, and ordinary builds do not fetch
Census, NOAA, FCC, FBI, HRSA, EPA, or Realtor.com ranking inputs. Maintainers generate a compact
`ranking_v2` bundle with `scripts/generate_ranking_reference.py` from
`config/ranking/source-lock-v2.json`. The packaged artifacts are county rows,
calibration knots/bounds and their hash, source vintages, citations, coverage
statuses, and checksums. Raw agency dumps are never packaged.

Population is Census PEP 2025 at current FIPS geography. It is an eligibility/context
input, never a higher-is-better utility. Ranking utilities remain calibrated on the
full source-valid national universe, but every County Fit pillar and Custom Fit excludes
counties with missing population or population below the inclusive 25,000 floor before
national ranks are assigned. Higher user thresholds apply after that fixed rank;
thresholds below 25,000 are rejected. Equal values use competition ranks (`1, 1, 3`),
with FIPS only ordering tied rows. Connecticut planning regions stay explicitly
null for legacy-county inputs instead of using an invented allocation or crosswalk.

FBI crime uses 2023–2025 attributed agency aggregates only when every year has at least
90% reporting-population coverage; violent/property person-year rates are inverse
average-tie ECDFs combined 60/40. Suppression stays null, never zero. EPA water uses
active retail community-system boundaries allocated by 2020 Census block population,
including visibly labeled EPA-modeled boundaries, and is null below 90% allocatable
coverage. Public-water share is context, not evidence of private-well use.
It excludes private wells and must not be interpreted as countywide water quality;
the water-violation utility, not the coverage share, contributes to Safety Factors.

Provider supply is an availability proxy: AHRF primary-care and dental counts plus the
CHR&R/NPPES broad mental-health measure are converted to rates, national average-tie
ECDF utilities, and combined equally. FCC input is the December 2025 terrestrial fixed
100/20 served share of broadband-serviceable locations, not people; Location Fabric is
denied. NOAA 1991–2020 climate values average equal-weight, fully qualifying in-county
stations with no nearest-station fallback. Mountain Landscape uses Mountain Magnitude
only; climate remains filter-only. Homeschool Policy Fit is an approximate,
project-authored state preference rubric, so counties within a state tie. It includes
official citations and effective dates but is not legal advice, legal-compliance or
school-quality evidence, or a recommendation.

Ranking Cost of Living uses MSA MARPP or official state all-items RPP labeled `state`.
Map Cost of Living remains MSA or U.S. Nonmetropolitan Portion `00999`. Do not advertise
`00999` as state-specific.

Private Realtor.com history is explicit `import-home-market` / `--history` with
`--acknowledge-personal-use`. Trailing-12-month medians feed ranking only; the map Home
Costs layer stays the latest single approved month. Historical artifacts remain local
and unexported in bulk.

### Updating ranking sources

1. Review each lock entry: URL, vintage, grain, license/terms, PII class, byte/hash/
   schema/row/FIPS contracts, and staleness.
2. Download into a maintainer staging directory with the shared HTTPS host allowlist,
   `trust_env=False`, redirect, size, archive, and checksum controls.
3. Recompute national utilities on the full 50-state/DC source-valid universe, persist
   calibration identity, exact per-source status distributions, bundle-pillar
   availability, and complete/partial public-core counts, then publish a new compact
   bundle. The validator recomputes those coverage aggregates from `counties.parquet`.
   Changing gates must not recompute calibration. Snapshot identity hashes the full
   bundle manifest (file checksums, coverage, scope, source lock) plus trailing-12
   home-market month digests, not only `calibration_hash` or the latest-month pointer.
4. Rebuild a schema-13 snapshot and re-run ranking golden fixtures plus both
   verification scripts.

## Housing stock — ACS 2024 five-year estimates

The maintainer-only reference generator uses the raw B25034 and B25035 table
files pinned in `config/housing-stock/source-lock-2024.json`. It emits compact
tract, county, and county-to-CBSA reference assets with a manifest. Normal
downloads, builds, and application startup read only those bundled assets and
must never contact Census. County shares are calculated from county estimates;
tract shares use tract estimates directly. Missing-value sentinels and zero
denominators produce explicit null statuses rather than invented values.
The county-to-CBSA relationship comes from B25034's pinned summary-level 313
geography records, using OMB Bulletin No. 23-01 delineations; it is not fetched
from an additional live crosswalk.

Built 2020+ is `B25034_E002 / B25034_E001`; built 2010+ adds `E003`; built
2000+ also adds `E004`. Source estimates and the component margins of error are
retained in the bundled audit tables. HouseHunter does not calculate a combined
percentage margin of error. Puerto Rico may retain ACS housing-stock context even
though its Cost of Living and current-market Home Costs fields are unavailable.

### Updating the ACS housing-stock bundle

1. Review the new ACS five-year table schemas and the OMB delineation used by the
   summary-level 313 county-to-CBSA records. Add a new pinned source lock; do not edit
   the meaning of the 2024 lock in place.
2. Record raw byte size, SHA-256, header SHA-256, row counts, required columns,
   geography counts, relationship counts, and expected BEA MSA matches.
3. Run the maintainer-only generator, never a normal prepare/build flow:

   ```console
   uv run python scripts/generate_housing_stock_assets.py \
     --source-lock config/housing-stock/source-lock-2024.json \
     --output src/househunter/assets/housing_stock
   ```

4. Review the generated manifest and diffs; reproduce the bundle from the same raw
   locks in a separate output directory and compare every hash. Exercise GEO_ID,
   sentinel, zero-denominator, direct-county formula, and no-network runtime tests.
5. Run release/package validation and both verification scripts before shipping the
   new public reference assets. Raw Census tables stay outside the repository.

## Home Costs — private local market import

The Realtor.com county inventory file is never downloaded automatically. A
user must import a locally obtained file with
`househunter import-home-market FILE --acknowledge-personal-use`, and the file
must exactly match an entry in the append-only reviewed-release lock bundled at
`config/home-market/release-lock.json`. The build installs that lock as
`househunter/assets/home_market_release_lock.json` so local imports work from a
wheel without packaging any market rows.

The selected policy assumes personal local use despite the restrictions in
[Realtor.com's general terms](https://www.realtor.com/terms-of-service/). This
is a product constraint, not a determination that reuse is legally permitted.
Neither source rows nor derived market snapshots may be committed, packaged,
uploaded, or published. Derived values are limited to the loopback API/UI and
user-triggered local exports with attribution, vintage, and this notice.

An accepted import must match one reviewed entry's filename, byte size, full SHA-256,
header SHA-256, single release month, unique valid county FIPS, row count, logical
checksum, and recorded quality counts. County IDs must belong to the bundled ACS 2024
50-state/DC county-equivalent universe; territories, retired codes, and invented codes
are rejected before national percentile calibration. Imports are capped at 64 MiB, reject symlinks,
normalize into a new immutable release, validate it, and atomically replace
`data/home-market/current.json`; cancellation or failure leaves the prior pointer
unchanged. Older approved releases remain usable but become stale after 62 days. The
loopback metadata API derives that flag from the snapshot release month at request time,
so an immutable build cannot freeze a release in the fresh state.

Only `quality_flag == 0` rows with finite positive median listing price per square foot
are rankable. Flagged and invalid rows remain in the normalized local table for audit,
but publish null map/ranking values and explicit statuses. HouseHunter calculates
square feet for $1M from the unrounded quotient, rounds only the published square-foot
value, and calibrates equal-weight tied percentiles across all eligible national
counties before applying a state build scope. Source county text is never authoritative;
the app displays its canonical county name.

### Reviewing and importing a new market month

1. Obtain the county file from the Realtor.com Research Data portal under the owner's
   selected personal-use workflow. Keep it outside the checkout and any package or
   distribution directory.
2. Review its terms, month, filename, bytes, full/header/logical hashes, exact header,
   unique FIPS, single-month rule, row count, quality-flag count, null-price count, and
   representative calculations. Do not log or paste real rows into tickets or tests.
3. Append—never replace—the reviewed entry in
   `config/home-market/release-lock.json`, including source-page attribution and review
   date. Use synthetic fixtures for all committed tests.
4. Run the import explicitly:

   ```console
   househunter import-home-market FILE --acknowledge-personal-use
   househunter build
   ```

5. Confirm `househunter sources`, `/api/v3/sources`, `/api/v3/meta`, local exports,
   national percentile samples, stale state, and rollback behavior. Then run
   `scripts/check_private_data_boundary.py`, package inspection, and both verification
   scripts. Delete temporary copies when the review is complete. Never add this source
   to `download --source all`.

## FEMA National Risk Index tracts

- Dataset: National Risk Index Census Tracts
- Release: v1.20, December 2025
- ArcGIS item: `9da4eeb936544335a6db0cd7a8448a51`
- Requested fields: `TRACTFIPS`, `ALR_NPCTL`, `ALR_VALB`, `NRI_VER`, and building `*_ALRB` plus `*_EALR` for 17 residential hazards (`AVLN`, `CFLD`, `CWAV`, `ERQK`, `HAIL`, `HWAV`, `HRCN`, `ISTM`, `LNDS`, `LTNG`, `IFLD`, `SWND`, `TRND`, `TSUN`, `VLCN`, `WFIR`, `WNTW`)
- Geometry: requested only by the maintainer map-asset generator; runtime preparation
  and ranking never request geometry
- Metric: HouseHunter independently derives building-hazard percentiles and `RES_HAZARD_NPCTL`; FEMA `ALR_NPCTL` and `ALR_VALB` remain supporting provenance.
- Cache: `data/cache/fema_nri_tracts.parquet`

Official references: [FEMA technical documentation](https://www.fema.gov/sites/default/files/documents/fema_national-risk-index_technical-documentation.pdf),
[FEMA ArcGIS item](https://www.arcgis.com/home/item.html?id=9da4eeb936544335a6db0cd7a8448a51&sublayer=0),
and [FEMA data resources](https://hazards.fema.gov/nri/data-resources).

## FEMA National Risk Index counties

- Dataset: National Risk Index Counties
- Release: v1.20, December 2025
- ArcGIS item: `39485e8035d446a5bff03259508ae355`
- Requested fields: `STCOFIPS`, `COUNTY`, `COUNTYTYPE`, `STATEABBRV`, `ALR_NPCTL`, `ALR_VALB`, `NRI_VER`, and the same 17 building `*_ALRB`/`*_EALR` pairs as the tract layer
- Geometry: requested only by the maintainer map-asset generator; runtime preparation
  and ranking never request geometry
- Metric: HouseHunter derives **county** building-hazard percentiles and `RES_HAZARD_NPCTL` among counties. Inputs come from this layer, never from averaging tracts.
- Cache: `data/cache/fema_nri_counties.parquet`

County names and county scoring inputs come from this layer. Tract rows join
`TRACTFIPS[:5]` to `STCOFIPS`. HouseHunter does **not** average tract percentiles to
produce a county score. Tract and county percentiles are not comparable. Drought is
excluded because it has no meaningful residential building-loss-rate input.

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

## Mountain Magnitude sources and release contract

Mountain Magnitude is a separate, derived contextual layer. Its maintainer build accepts:

- USGS 3DEP elevation rasters;
- PAD-US 4.1 polygons from the official anonymous USGS
  `PAD_US_gaz_combined` MapServer layer, using `Pub_Access`;
- a national hiking-trail line layer;
- 2020 Census blocks with 15-digit GEOID, `POP20`, geometry, and one internal point.

The reviewed national contract is committed as
[`config/mountain/source-lock-v2.json`](config/mountain/source-lock-v2.json), with its
[`config/mountain/regions-v1.json`](config/mountain/regions-v1.json) region mapping,
[`config/mountain/qualification-v1.json`](config/mountain/qualification-v1.json)
qualification report, and
[`config/mountain/representative-raw-digests-v1.json`](config/mountain/representative-raw-digests-v1.json)
representative raw-metric anchors.

These large inputs are neither bundled nor fetched by `./scripts/dev`. A reviewed
JSON source lock v2 supplies the exact final HTTPS URL, byte size, SHA-256,
acquisition date, public release/license, actual CRS/schema/count, and filename for
every file. It also pins
reviewed HTTPS hosts, ordered elevation precedence, the exact sorted national block
GEOID digest, per-state block/population expectations, a block-driven tile inventory
with per-tile GEOID and canonical sample digests, six representative raw-metric
digests, and a complete phase storage projection. Production preparation rejects an alias or incomplete region
CRS: CONUS/DC is EPSG:5070, Alaska is EPSG:3338, and Hawaii is a reviewed fixed
equal-area WKT. Region configuration names only locked files.

PAD-US acquisition does not require or permit a login. The lock freezes the anonymous
service metadata and complete `OBJECTID` inventory, explicit ID pages, page-level
semantic and FlatGeobuf checksums, public-access totals, and the final assembled
artifact. Acquisition verifies the service and ID inventory both before and after the
capture and consumes polygons in numeric `OBJECTID` order so ArcGIS or FlatGeobuf
storage order cannot change replace precedence.

`househunter mountain inventory` reads only the normalized Census block/internal-point
inputs and emits the exact signed tile coordinates, per-tile counts/population, all-state
expectations, sorted GEOID digest, and a conservative pack/release/work high-water
projection. Review and copy those values into source-lock v2 before acquiring the much
larger elevation and access inputs.

`househunter mountain download --family FAMILY` acquires a bounded whole family;
`--batch elevation-NNN` or `--batch trails-NNN` acquires one reviewed source-to-tile
batch. The downloader disables ambient proxies, permits only the lock's
reviewed HTTPS hosts, revalidates every redirect, checks declared length while
streaming, verifies bytes before atomic publication, and maintains the unrelated-disk
reserve. Managed acquisition is rejected when it would exceed the qualified batch/family
staging peak. `househunter mountain prepare` resumes four fixed phases—blocks,
elevation, PAD-US, then trails—and accepts only checksum-valid completed outputs.
Elevation batches partition the logical tiles; ordered trail batches may overlap tiles
but their clipped fragments are grouped by locked `GLOBALID` before rasterization, so a
cross-state trail is counted once. Each phase or batch verifies its raw inputs
before work and removes managed inputs only after all dependent derived files validate;
external source roots are never removed. Preparation
uses 100 km processing cores with exact 100 km
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
aggregate is cached. A generated schema-2 candidate is written inside the release
filesystem, validated by independently reconstructing block percentiles, the internal
composite, tract/county bases, both same-grain peer calibrations, and final magnitudes,
then renamed to its content identity. The timed path finishes only after the schema-13
HouseHunter snapshot is rebuilt and queryable. It also publishes one content-addressed
compact fallback under `data/mountain/compact/`.

Publication stages and validates the full release, compact release, and snapshot before
changing a pointer. Before pointer commit, a failure leaves all prior pointers
byte-for-byte unchanged. During commit, an atomic `migration-v2.json` journal records
only the expected artifact identities and phase; rerunning after interruption completes
the same transaction until full, compact, and snapshot pointers agree, then removes the
journal. Runtime loading fails closed on a v1 or mixed managed pointer and directs the
operator to `househunter mountain rescore-v1` instead of silently using the bundle.

The source lock records compressed-download totals, largest `.part`, extracted bytes,
largest reviewed family/batch source-staging peak, preparation workspace and atomic duplication,
comparison/build shards, candidate/retained releases, bundle, reports, and reserve. The
validator recomputes acquisition, preparation, timed-build, and overall peaks rather
than trusting reported totals. The prepared pack is capped at 22 GB; work shards and any one full release at 4 GB;
active plus rollback releases at 8 GB. The implementation targets 45 GB and hard-stops
before 50,000,000,000 allocated bytes while retaining at least 10 GB of unrelated free
space. Cleanup is limited to direct, marked, non-symlink children of managed Mountain
work/release/prepared/compact directories. Successful preparation retains only the new
prepared pack; successful publication retains one compact fallback. The compact
tract/county fallback must remain below 50 MiB.
The normal snapshot reads only tract/county Parquet and the manifest, so Rasterio,
Shapely, PyProj, SciPy, Pyogrio, and NumPy are optional maintainer dependencies.

The complete executable acquisition, batch preparation, final pack publication,
source-derived equivalence, benchmark, promotion, rollback, and license-review runbook
is in the Mountain section of [`README.md`](README.md). National source comparison uses
the prepared lock's managed `comparison_id`; a successful comparison removes those
temporary source-derived shards. The direct `--regions` national path is rejected for
the state-clipped trail contract because it cannot perform the required global fragment
deduplication.

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
- each two-decimal component percentile is converted to integer hundredths and the
  validation-only block base units are `45R + 20G + 20P + 15H`;
- tract and county bases use exact population-weighted integer sums and half-even
  rounding to six decimals, eliminating accidental ties from the former two-decimal
  aggregate;
- complete and partial non-null geographies enter one national cohort at their own
  grain, with each geography weighted once regardless of population;
- for base `s_g` and eligible same-grain peer count `N`, the public value is
  `round4(log10(N / count(h: s_h >= s_g)))` using an inclusive equal-or-higher tail;
  genuine six-decimal base ties share a finite magnitude, singleton and all-tied
  cohorts are zero, and values are never capped.

Release schema 2 records the source lock, unchanged `mountain_pipeline_v1`, internal
`mountain_score_v1`, public `mountain_magnitude_v2`, formula, integer weighting,
six/four-decimal precision and half-even rounding, inclusive tie rule, national
same-grain peer scope and counts, release identity, coverage, row counts, hashes, and
whether national completeness was proven. Runtime artifacts expose no legacy aggregate
score field. The validator recomputes release identity, hashes, block domains, every
component percentile and composite from rounded raw block values, tract/county bases,
both peer calibrations, final magnitudes, and coverage semantics before promotion. A
promoted release changes the main HouseHunter build identity; an absent release produces
null Mountain fields with `unavailable` status rather than silently substituting zero.

The committed compact schema-2 bundle is accepted only with 83,848 scored tracts and
3,143 scored counties, maxima approximately 4.9235 and 3.4973, western tract median/p95
approximately 0.88/1.94, and western county median/p95 approximately 0.96/2.14. Both
grains must retain at least 0.75 magnitude from median to p95 and from p95 to maximum.
County anchors are Pitkin 3.4973, Summit 2.3213, Wasatch 2.2421, Salt Lake 1.6109, and
Boulder 1.2349. Package release validation reloads the bundle, verifies its schema,
identity, artifact hashes, peer counts, public columns, size ceiling, and these gates.
The equivalence workflow shuffles block order and compares one- and four-worker builds;
the two-build benchmark requires identical release identities and Parquet hashes.

`househunter mountain rescore-v1 --source-lock config/mountain/source-lock-v2.json`
is the only cross-major data migration. It runs offline under the normal exclusive lock
and accepts only the active, owned, contained, non-symlinked, nationally complete full
v1 release. A migration-only reader validates v1 checksums, source identity, raw blocks,
components, scores, and aggregates, but v2 is derived from the raw block columns and is
never written into the v1 directory. Identical raw inputs therefore produce the same v2
identity as a fresh build; migration lineage stays in an ignored report rather than the
deterministic manifest. The v1 full release remains unpruned through installed-package
acceptance. Cross-major rollback restores both the v1 application and its v1 pointers;
v1 is not a release that the v2 runtime can promote or load.

FEMA v1.20 uses Connecticut planning-region FIPS while the locked 2020 block source
uses legacy Connecticut county FIPS. Runtime tract rows are reconciled only when the
six-digit tract code forms a unique, complete mapping. The four unmatched legacy rows
are zero-population water tracts. The nine FEMA planning-region county rows remain
explicitly unavailable because the release has no exact planning-region aggregates.

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
that tract's derived Residential Hazard Exposure percentile, not a property-level
rating. The public
Census geocoder only matches streets in its address-range file, so new
subdivisions often need the Nominatim street confirmation.

## Census reference assets

Runtime ranking does **not** download or require Census Place/housing assets.
`./scripts/dev` uses only the pinned FEMA and CHR&R caches.

Maintainer-only Place-generation scripts remain in the tree for historical release assets
and are unused by the local app. Prepare, download, and build never contact census.gov.

## Limitations

Residential Hazard Exposure is a HouseHunter composite for that geography, not risk at
a specific address. A percentile is a national relative ranking, not a probability or
expected dollar loss. The model emphasizes elevated tails across 17 building hazards
and should not be read as one particular hazard. Not-applicable hazards and valid zero
rates contribute zero; genuinely missing or invalid rates remain null with an explicit
data-quality flag. County scores and county hazard percentiles are not a summary of the
tracts inside the county. FEMA's own `ALR_NPCTL` remains supporting context, while
`PROPERTY_LOSS_NPCTL` separately ranks `ALR_VALB` and is not part of the composite.
Mountain Magnitude is an approximate regional context metric, not a parcel or address
assessment. Coarse cells and source completeness can miss narrow ridges, informal or
unmapped trails, seasonal closures, entrances, legal access details, travel time, and
views. The land rings measure classified mountain area around a block point, not a
guaranteed route from that point. Release-to-release comparisons require review of
source and magnitude versions; raw values, component percentiles, bases, and peer ranks
may move when source coverage or the national reference population changes. A one-unit
magnitude increase means ten times fewer equal-or-higher same-grain peers; it does not
mean ten times more mountainous terrain. Tract and county magnitudes use different peer
universes and are not comparable with each other.
National tract boundaries are simplified for overview rendering, so fine boundary detail
appears only after zoom loads the jurisdiction asset. Small urban tracts can be subpixel at
the national extent; they are not aggregated or enlarged.
