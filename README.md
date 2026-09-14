# HouseHunter

HouseHunter is a local-only macOS application that maps FEMA National Risk Index
geographies by published `ALR_NPCTL`. Lower is better. The full-viewport map starts
with all 85,154 tracts; a county mode maps all 3,232 counties using FEMA's official
county table, not an average of tract scores.

The map can also switch to the official 2025 County Health Rankings & Roadmaps
**Community Conditions** Health Group. This is an independent county-level layer:
Group 1 is healthiest and Group 10 least healthy. The groups are data-driven
clusters, not percentiles. Tracts inherit their county's group and are labeled
county-level; HouseHunter never blends this value with FEMA risk.

An optional **Mountain Magnitude** layer summarizes resident exposure to nearby
terrain, public mountain land, and mapped trail access. Its uncapped logarithmic
scale is calibrated independently among U.S. tracts and among U.S. counties: a
one-unit increase means ten times fewer same-grain peers have an equal-or-higher
underlying mountain composite. Tract and county magnitudes are therefore not
cross-grain comparable. The layer is built offline from pinned GIS inputs and
joined to the normal snapshot as a small tract/county Parquet artifact. Normal app
installation does not install GIS libraries or download elevation rasters. If no
validated national Mountain release has been promoted, its fields remain explicitly
unavailable.

`ALR_NPCTL` is FEMA's national percentile for composite Expected Annual Loss Rate,
distinct from FEMA's broader Risk Index. Tract and county percentiles are not
comparable. Detail views and exports also show FEMA's 18 published
`{CODE}_ALR_NPCTL` hazard percentiles at the same grain. Those bars explain the
composite; they are not a HouseHunter blend, and lists still rank only on
composite `ALR_NPCTL`. A HouseHunter score is **not** a property-level
assessment, a loss probability, an insurance quote, or a prediction. Address lookup
maps a house to its 2020 Census tract via the public Census geocoder, then shows
that tract's FEMA score. If Census returns no street match, the loopback server
may query OpenStreetMap Nominatim and convert accepted coordinates back through
Census. Street-level matches need confirmation because a road point can cross
tract boundaries.

## Quick start

HouseHunter requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). The compiled web
interface is committed, so Node is not required to use the app. No API keys are required.

```console
./scripts/dev
```

That installs the small runtime dependency set, downloads the pinned FEMA
tract/county and CHR&R Community Conditions county layers if needed, publishes a snapshot, and
starts the loopback app. Flags:
`--port`, `--no-open`, `--state`, `--skip-prepare`. `--state` only scopes the
snapshot. The first run downloads each source once and reuses verified local caches afterward.

The same steps can be run individually:

```console
uv sync
uv run househunter sources
uv run househunter download --source all
uv run househunter build
uv run househunter rank --state CO --metric mountain --mountain-magnitude-min 1.5 --limit 20
uv run househunter rank --level county --state CO
uv run househunter rank --level county --metric community-conditions --order best
uv run househunter inspect 08013012101
uv run househunter inspect 08013
uv run househunter lookup "1670 Broadway, Denver, CO"
uv run househunter lookup "1720 Lazy Cat Ln, Monument, CO 80132" --allow-approximate
uv run househunter app
```

Runtime data is written beneath `data/` by default. Set `HOUSEHUNTER_DATA_DIR` to use a
different local directory. The server listens only on `127.0.0.1`; it has no telemetry,
accounts, hosted database, or external browser requests.

The map Search panel is a street-address lookup (`Find tract`). It does not
search tract names or FIPS codes; use `househunter inspect` for those. A lookup
first asks Census. If Census returns a valid empty match list, the server may
query Nominatim (`HOUSEHUNTER_NOMINATIM_URL`, default
`https://nominatim.openstreetmap.org`; set to `off` to disable). Nominatim is
not used for Census outages, malformed Census responses, or ambiguous Census
matches. Apartment and unit suffixes are ignored because they do not change
the tract. Street names must be spelled correctly. Do not submit confidential
addresses.

## Commands

```text
./scripts/dev [--port PORT] [--no-open] [--state CO] [--skip-prepare]
househunter sources [--json]
househunter download [--source fema|fema_counties|chrr|all]
househunter build [--state CO]
househunter rank [--state CO] [--county STCOFIPS] [--level tract|county] [--metric risk|community-conditions|mountain] [--mountain-magnitude-min NONNEGATIVE] [--order best|worst] [--limit N] [--include-unranked]
househunter inspect TRACT_FIPS|COUNTY_FIPS
househunter lookup "1670 Broadway, Denver, CO" [--allow-approximate]
househunter export --format parquet|csv|json [--level tract|county] [--output PATH]
househunter app [--port PORT] [--no-open]
```

`build --state` publishes a snapshot explicitly marked as state-scoped. Re-running a build
with identical inputs reuses the same content-addressed directory. A failed or cancelled
build never replaces the current snapshot. Export destinations must be outside HouseHunter's
managed cache and build paths so exports cannot overwrite immutable runtime data.

## Method

For tract `t` and county `c`, HouseHunter uses:

```text
risk_score[t] = ALR_NPCTL[t]
risk_score[c] = ALR_NPCTL[c]
```

County names come from the FEMA county layer. Tract rows join `TRACTFIPS[:5]` to
`STCOFIPS`. The county percentile is FEMA's published county value, ranked among
counties; it is not the mean of tract percentiles. Per-hazard county values are
that county layer's `{CODE}_ALR_NPCTL`, not an average of tract hazards. Null
hazard percentiles mean FEMA published no rating; they are not zero. State is
derived from the first two digits of `TRACTFIPS` using a bundled FIPS map.
HouseHunter does not impute, re-percentile, winsorize, or renormalize around
missing rows. Unrankable rows remain visible as `missing_fema`.

Community Conditions is joined on the same five-digit county FIPS. Its official
`CommunityConditions_Group` is retained as an integer 1–10 or null; null is shown
as `Not grouped` and sorts last in both directions. The processed national artifact
is `data/processed/chrr_county.parquet`, and every immutable snapshot carries its
state-scoped copy and DuckDB table.

Mountain Magnitude starts with measurements for 2020 Census blocks on deliberately
approximate 250 m equal-area cells. National population-weighted lower-rank component
percentiles for 20 km relief, rugged terrain, ring-weighted open public mountain land,
and mapped trail access retain the 45%, 20%, 20%, and 15% weights. Each canonical
two-decimal component is converted to integer hundredths; the block base is
`45R + 20G + 20P + 15H`, and exact integer sums are resident-weighted into tract and
county bases, rounded half-even to six decimals.

Within each grain, every complete or partial non-null geography is then one equally
weighted national peer. If `s_g` is its base and `N` is the eligible peer count,
`M_g = round4(log10(N / count(h: s_h >= s_g)))`. Inclusive equal-or-higher tails give
genuine six-decimal ties the same finite magnitude; singleton and all-tied cohorts are
`M0.0000`. Values are never capped. `M2.32` means roughly 10^2.32 times fewer
same-grain peers have an equal-or-higher base—it does not mean 2.32 times or 232%
more mountainous terrain. Population-zero blocks receive raw measurements but do not
affect component calibration; zero-population, insufficient-coverage, outside-scope,
and null geographies receive no magnitude and do not enter `N`.

Promotable releases cover exactly the 50 states plus DC; Puerto Rico and the other
territories remain outside scope. Connecticut tract values are reconciled to FEMA's
planning-region identifiers by their unique six-digit tract codes. Connecticut's nine
planning-region county rows
remain unavailable because the Mountain release contains the eight 2020 counties;
HouseHunter does not substitute approximate county aggregates.
The magnitude does not claim property views, trail quality, drive time, or guaranteed
public access.

Read [DATA_SOURCES.md](DATA_SOURCES.md) for source provenance, release maintenance, and
limitations.

## Development

```console
uv sync
uv run pytest
uv run ruff check .

cd web
npm ci
npm test
npm run build
```

The map-only `GET /api/v2/map/scores?level=tract|county` contract is schema
version 3. It returns equal-length, column-oriented `place_id`, `risk_score`,
`community_conditions_group`, and `mountain_magnitude` arrays in ascending unique
`place_id` order. Its `build_id`, `level`, and `scope` identify the snapshot.
Coverage-status fields remain available from place, county, detail, and export
interfaces; they are intentionally absent from this compact rendering payload.
All public HTTP routes are under `/api/v2`; `/api/v1/*` is intentionally unsupported.
List endpoints filter with nonnegative `mountain_magnitude_min` and
`mountain_magnitude_max` bounds, reject inverted or nonfinite ranges, and impose no
upper magnitude limit. The map uses a visual domain of M0–M5 for tracts and M0–M4 for
counties without capping stored values; common M0–M4 values keep the same colors across
grains.

The real-data interaction benchmark is kept separate from fixture CI because its
timings are machine-sensitive:

```console
cd web
npm run test:perf
```

It records canonical Chromium and WebKit evidence at 1600×900 and DPR 2 and
enforces the fixed gesture, settle, pick, detail, startup, long-task, and payload
ceilings.

At runtime, same-origin loader and renderer workers keep topology parsing,
projection, exact `Path2D` picking, and DPR-aware rasterization off the main
thread. A neutral national outline is committed while the full tract dataset is
prepared; it is explicitly non-interactive until the complete indexed frame is
ready. Gestures transform the last bitmap through the compositor, and detailed
tract geometry is prefetched with four bounded requests while retaining the 24
most-recent non-visible states.

The native GIS stack is maintainer-only and optional. An installation with an active,
owned, nationally complete full v1 Mountain release can migrate it offline without
re-fetching GIS inputs:

```console
uv run househunter mountain rescore-v1 --source-lock config/mountain/source-lock-v2.json
```

The single-purpose migration validates the v1 release, source identity, raw blocks,
components, internal scores, and aggregates, then derives v2 from the validated raw
block columns. It never trusts persisted aggregate scores or edits v1 artifacts in
place. Full release, compact release, and schema-9 snapshot candidates are staged and
validated before any pointer changes. A small atomic journal makes an interrupted
pointer commit forward-recoverable: rerunning the same command completes the same v2
transaction. A v1, mixed, symlinked, partial, compact-only, foreign, or source-drifted
installation fails with an actionable error.

A full rebuild from pinned sources uses:

```console
uv sync --extra mountain
SOURCE_LOCK=config/mountain/source-lock-v2.json
REGIONS=config/mountain/regions-v1.json
STAGING=data/mountain/staging/055a8855d12e5062
PREPARED_LOCK=data/mountain/prepared/national-prepared-lock-v1.json
uv run househunter mountain download --source-lock $SOURCE_LOCK --family blocks
uv run househunter mountain prepare --source-lock $SOURCE_LOCK --regions $REGIONS --source-root $STAGING --through-family blocks
for BATCH in $(uv run python -c 'import json; print(*[x["id"] for x in json.load(open("config/mountain/source-lock-v2.json"))["preparation_batches"] if x["family"] == "elevation"])'); do
  uv run househunter mountain download --source-lock $SOURCE_LOCK --batch $BATCH
  uv run househunter mountain prepare --source-lock $SOURCE_LOCK --regions $REGIONS --source-root $STAGING --source-batch $BATCH
done
uv run househunter mountain download --source-lock $SOURCE_LOCK --family pad_us
uv run househunter mountain prepare --source-lock $SOURCE_LOCK --regions $REGIONS --source-root $STAGING --through-family pad_us
for BATCH in $(uv run python -c 'import json; print(*[x["id"] for x in json.load(open("config/mountain/source-lock-v2.json"))["preparation_batches"] if x["family"] == "trails"])'); do
  uv run househunter mountain download --source-lock $SOURCE_LOCK --batch $BATCH
  uv run househunter mountain prepare --source-lock $SOURCE_LOCK --regions $REGIONS --source-root $STAGING --source-batch $BATCH
done
uv run househunter mountain prepare --source-lock $SOURCE_LOCK --regions $REGIONS --source-root $STAGING --prepared-lock-output $PREPARED_LOCK
PACK_ID=$(uv run python -c 'import json; print(json.load(open("data/mountain/prepared/national-prepared-lock-v1.json"))["pack_id"])')
COMPARISON_ID=$(uv run python -c 'import json; print(json.load(open("data/mountain/prepared/national-prepared-lock-v1.json"))["comparison_id"])')
uv run python scripts/compare_mountain_builds.py --source-lock $SOURCE_LOCK --regions $REGIONS --reference-shards data/mountain/work/$COMPARISON_ID --prepared-pack data/mountain/prepared/$PACK_ID --prepared-lock $PREPARED_LOCK --data-release 2026q3
uv run python scripts/benchmark_mountain.py --source-lock $SOURCE_LOCK --prepared-pack data/mountain/prepared/$PACK_ID --prepared-lock $PREPARED_LOCK --data-release 2026q3
RELEASE_ID=$(uv run python -c 'import json; print(json.load(open("data/mountain/current.json"))["release_id"])')
uv run househunter mountain validate data/mountain/releases/$RELEASE_ID
uv run househunter mountain inspect 08013012101
uv run househunter mountain bundle data/mountain/releases/$RELEASE_ID --output src/househunter/assets/mountain
```

Production preparation requires source-lock v2: exact checksums and metadata for every
consumed input, reviewed HTTPS hosts and redirects, ordered DEM precedence, complete
region CRS definitions, exact block and population totals for all 50 states plus DC,
the sorted national GEOID digest, and a qualified tile/storage projection. Preparation
uses the official anonymous USGS PAD-US MapServer snapshot contract; it requires no
account, token, or interactive login. The snapshot is accepted only when its locked
service metadata, full `OBJECTID` inventory, page checksums, access totals, and final
artifact checksum all match.
Preparation
downloads and derives blocks and PAD-US one source family at a time, and large
elevation/trail inputs in reviewed source-to-tile batches, so compressed inputs, extraction,
`.part` duplication, and the growing pack remain inside the qualified phase peaks.
Each successful family or batch is checksummed before its managed raw dependencies are
removed; explicit external sources are never removed. Trail batches ingest clipped
fragments in canonical source order, then group them by locked `GLOBALID` before one
national rasterization pass, so cross-state copies are not counted twice and an
interruption cannot replay a contribution.
Preparation writes 100 km cores with exact 100 km halos to a content-addressed pack and
emits a separate prepared-pack lock. Mountain Magnitude v2 never emits partial releases:
every build requires the reviewed national 50-states-plus-DC peer universe, including
non-promoted validation candidates.

Prepared builds require both locks: the reviewed source lock is the independent trust
anchor for the source inventory, while the prepared lock anchors every immutable tile.
Promoting a separately supplied release additionally requires the prepared pack so its
raw block metrics can be recomputed and compared exactly; validation without promotion
does not require those inputs.

Prepared builds use one bounded pool of one to four worker processes. `--resume` reuses
only checksum-valid shards for the same pack and pipeline; `--fresh` clears only the
marked work directory for that pack. A successful promoted prepared build rebuilds the
schema-9 normal snapshot, removes obsolete owned v2 releases and work shards, publishes
the validated under-50-MiB managed compact fallback, and writes a timing report under
`data/mountain/reports/`. Release schema 2 binds the unchanged
`mountain_pipeline_v1`, validation-only internal `mountain_score_v1`, public
`mountain_magnitude_v2`, exact magnitude formula and precision rules, same-grain peer
counts, provenance, and artifact hashes into one content identity. The Mountain workflow
enforces a 45 GB engineering ceiling, stops before 50,000,000,000 managed bytes, and
preserves 10 GB of unrelated free filesystem space.

The national laptop acceptance gate runs two clean four-worker builds and checks the
55-minute runtime, 24 GiB aggregate RSS, swap growth, 45/50 GB storage limits, identical
release and Parquet identities, schema-2/9 publication identity, and a queryable
Mountain-ranked snapshot. It also gates 83,848 scored tracts and 3,143 scored counties;
maxima near 4.9235 and 3.4973; western tract median/p95 near 0.88/1.94 and county
median/p95 near 0.96/2.14; at least 0.75 from median to p95 and from p95 to maximum at
both grains; and reviewed county anchors Pitkin 3.4973, Summit 2.3213, Wasatch 2.2421,
Salt Lake 1.6109, and Boulder 1.2349.

The exact command appears in the preparation runbook above. It uses the resolved
`$PACK_ID`, leaves the prior release active if either measured run fails, and promotes
only the accepted second run.

Before the timed gate, `scripts/compare_mountain_builds.py` proves exact canonical raw
table and schema-2 release-hash equality across canonical and shuffled row order and
among the lock-anchored source-derived shards and prepared builds using one and four
workers. The rescore path uses the same v2 release writer, so identical validated raw
inputs produce the same identity as a fresh v2 build; migration lineage is recorded
only in an ignored report and cannot change that identity. Direct national `--regions`
builds are rejected for the state-clipped trail contract because they cannot safely
deduplicate cross-state fragments. Both scripts emit machine-readable reports beneath
`data/mountain/` by default.

Rollback is explicit and fail-closed. A same-major v2 rollback may validate and promote
a retained v2 full release with the source lock, prepared pack, and prepared lock that
created it; promotion is refused if provenance or raw metrics differ. A v1 release is
never advertised as a v2-compatible rollback. Keep the pre-migration v1 full release
unpruned until the installed 2.0.0 wheel passes offline acceptance. Cross-major rollback
restores the v1 application and all of its v1 full, compact, and snapshot pointers as one
operation; do not point the v2 application at v1 data. Source licenses and public release
identifiers are recorded per input in `source-lock-v2.json`. When refreshing a lock,
re-review the official license page and release timestamp, preserve required notices,
and never distribute the fetched GIS archives or the full prepared pack in the wheel.

The committed `web/dist/` must match `npm run build`. The Python wheel packages the
compiled UI and the derived map assets exactly once. Raw third-party files and generated
runtime databases belong under ignored `data/`, never in Git.

Maintainers regenerate boundaries only when the pinned FEMA revisions change:

```console
uv run python scripts/generate_map_assets.py
uv run python scripts/validate_release.py
```

The generator validates the live ArcGIS item/revision metadata, exact tract and county
identifier sets, geometry, jurisdiction coverage, output sizes, and every generated
topology. The release validator checks the pinned tract and county caches against the
packaged map manifest and independently loads the committed schema-2 compact Mountain
bundle, verifies its identity and hashes, absence of public legacy score columns, size
ceiling, peer counts, extrema, western separation, and reviewed anchors. It writes its
report under ignored `data/`. The generator emits
deterministic content-addressed files from a clean staged candidate, validates the full
candidate before publication, and keeps the prior release usable if validation fails. It writes
`src/househunter/map_assets/manifest.json`; `--reuse-raw` rebuilds from the ignored local
geometry download only when its recorded source revisions and digests still match the
live pinned FEMA layers. A revision mismatch or corrupt packaged asset blocks the map
with an explicit repair message.

## Scope

The FEMA layer uses composite `ALR_NPCTL`; the independent Community Conditions layer
uses only CHR&R's published group; Mountain Magnitude remains a separate approximate
context layer. The 18 published FEMA hazard percentiles appear on inspect/detail
and ride along in exports; there is no hazard map layer, sort or filter by hazard,
insurance data, external basemap, hosted service, or native installer.

## License

MIT. Public source data retain their respective attribution and terms.
