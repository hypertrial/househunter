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

An optional **Mountain Score** layer summarizes nearby terrain, public mountain
land, and mapped trail access on a 0–100 national scale. It is built offline from
pinned GIS inputs and joined to the normal snapshot as a small tract/county
Parquet artifact. Normal app installation does not install GIS libraries or
download elevation rasters. If no validated national Mountain release has been
promoted, its fields remain explicitly unavailable.

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
./scripts/run-app
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
uv run househunter rank --state CO --mountain-min 60 --limit 20
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
./scripts/run-app [--port PORT] [--no-open] [--state CO] [--skip-prepare]
househunter sources [--json]
househunter download [--source fema|fema_counties|chrr|all]
househunter build [--state CO]
househunter rank [--state CO] [--county STCOFIPS] [--level tract|county] [--metric risk|community-conditions] [--mountain-min 0..100] [--order best|worst] [--limit N] [--include-unranked]
househunter inspect TRACT_FIPS|COUNTY_FIPS
househunter lookup "1670 Broadway, Denver, CO" [--allow-approximate]
househunter export --format parquet|csv [--level tract|county] [--output PATH]
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

Mountain Score is first calculated for 2020 Census blocks, then population-weighted
to tracts and counties. The deliberately approximate terrain builder uses 250 m
equal-area cells. Its components are national population-weighted lower-rank
percentiles for 20 km relief, rugged terrain, ring-weighted open public mountain
land, and mapped trail access, combined at 45%, 20%, 20%, and 15%. Population-zero
blocks receive raw measurements but do not affect percentile calibration. Promotable
releases cover exactly the 50 states plus DC; Puerto Rico and the other territories
remain outside the scoring scope and are `unavailable` at runtime.
The score does not claim property views, trail quality, drive time, or guaranteed
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

The native GIS stack is maintainer-only and optional:

```console
uv sync --extra mountain
uv run househunter mountain download --source-lock /path/to/source-lock.json --destination /path/to/sources
uv run househunter mountain build --data-release 2026q3 --source-lock /path/to/source-lock.json --regions /path/to/regions.json --source-root /path/to/sources
uv run househunter mountain validate data/mountain/releases/RELEASE_ID
uv run househunter mountain inspect 08013012101
```

Production promotion requires a checksum lock for every consumed input and exact
expected block and population totals for all 50 states plus DC. Using
`--allow-partial --no-promote` is available for small development fixtures; partial releases can
never become the active runtime release.

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
packaged map manifest and writes its report under ignored `data/`. The generator emits
deterministic content-addressed files from a clean staged candidate, validates the full
candidate before publication, and keeps the prior release usable if validation fails. It writes
`src/househunter/map_assets/manifest.json`; `--reuse-raw` rebuilds from the ignored local
geometry download only when its recorded source revisions and digests still match the
live pinned FEMA layers. A revision mismatch or corrupt packaged asset blocks the map
with an explicit repair message.

## Scope

The FEMA layer uses composite `ALR_NPCTL`; the independent Community Conditions layer
uses only CHR&R's published group; Mountain Score remains a separate approximate
context layer. The 18 published FEMA hazard percentiles appear on inspect/detail
and ride along in exports; there is no hazard map layer, sort or filter by hazard,
insurance data, external basemap, hosted service, or native installer.

## License

MIT. Public source data retain their respective attribution and terms.
