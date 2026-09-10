# Data sources and provenance

HouseHunter v1 pins data vintages. It never discovers or accepts an automatic “latest”
release. A source update requires review, regeneration, validation, and a HouseHunter release.

## FEMA National Risk Index

- Dataset: National Risk Index Census Tracts
- Release: v1.20, December 2025
- ArcGIS item: `9da4eeb936544335a6db0cd7a8448a51`
- Requested fields: `TRACTFIPS`, `ALR_NPCTL`, `NRI_VER`
- Geometry: never requested
- Metric: composite Expected Annual Loss Rate national percentile (`ALR_NPCTL`)

The downloader validates the layer edit timestamp, field names and ArcGIS types, release
label, unique 11-character tract identifiers, row count, range `[0, 100]`, and the canonical
logical checksum in `config/sources.yml`. Pages are cached independently; publication uses a
temporary file and atomic rename. The local source manifest records retrieval time, logical
and physical SHA-256 checksums, row count, schema fingerprint, source URL, and terms URL.
On a verified cache hit, `download` validates that manifest against the cached Parquet and
reconstructs missing or corrupt manifest metadata from the cache file timestamp. If the final
Parquet is corrupt, the pinned remote contract and revision-specific page cache are used to
rebuild it atomically; remote schema or version drift still stops the operation.

Official references: [FEMA technical documentation](https://www.fema.gov/sites/default/files/documents/fema_national-risk-index_technical-documentation.pdf),
[FEMA ArcGIS item](https://www.arcgis.com/home/item.html?id=9da4eeb936544335a6db0cd7a8448a51&sublayer=0),
and [FEMA data resources](https://hazards.fema.gov/nri/data-resources).

## Census reference assets

Ordinary setup uses three compact release assets packaged with HouseHunter:

- `places_2020.parquet`: canonical name, state, type, population, and housing for every
  incorporated Place and CDP in the 50 states and DC.
- `place_tract_weights_2020.parquet`: positive-housing Place/tract intersections and weights.
- `acs_2024_context.parquet`: 2024 ACS 5-year population, housing units, and median home
  value (`B01003_001E`, `B25001_001E`, `B25077_001E`).

The maintainer-only `scripts/generate_reference_assets.py` streams official 2020
population-and-housing block DBFs and joins the official incorporated-Place/CDP Block
Assignment File. It aggregates `HOUSING20` at `(place_id, tract_id)` and normalizes only after
discarding zero-housing intersections. Place names/types come from the 2020 Place DBF. ACS
context comes from the official 2024 ACS 5-year API and requires a Census API key.

Raw archives are cached beneath ignored `data/reference-source/` and are never packaged or
committed. The generator emits sorted Parquet plus `reference_metadata.json` containing
logical and raw source checksums, row counts, source URLs, generation time, and a Connecticut
audit. The validator emits `reference_validation.json`; both JSON records are committed with
the three Parquet assets. Every runtime build rechecks the packaged table row counts, logical
checksums, national scope, and pinned vintages against that metadata before scoring.

ACS context is left-joined onto the canonical 2020 Place universe. Places introduced only in
the 2024 geography are excluded; 2020 Places without a 2024 ACS row are retained with null
context values. Both identifier sets are recorded in `reference_metadata.json`, and neither
case changes the risk score.

### Release regeneration

First retrieve and validate FEMA, then run:

```console
uv run househunter download --source fema
CENSUS_API_KEY=... uv run python scripts/generate_reference_assets.py
uv run python scripts/validate_release.py
```

The Census bulk host may require maintainers to download the named official archives into
the expected `data/reference-source/<state FIPS>/` cache paths when automated access is
rate-limited. Existing files are verified structurally and reused.

Official references: [2020 TIGER/Line technical documentation](https://www.census.gov/programs-surveys/geography/technical-documentation/complete-technical-documentation/tiger-geo-line.2020.html),
[2020 Block Assignment Files](https://www.census.gov/geographies/reference-files/2020/geo/block-assignment-files.html),
and [2024 ACS 5-year API](https://api.census.gov/data/2024/acs/acs5.html).

### Connecticut

FEMA v1.20 uses post-2020 Connecticut planning-region tract identifiers. During reference
generation, old Connecticut tract IDs are reconciled against the exact pinned FEMA tract
universe by retained six-digit tract code. The script fails unless the mapping is one-to-one,
apart from former tract `09001990000`, whose two documented successors are `09120990000` and
`09190990000`. FEMA omits those water-only successors. The exception is accepted only when
its block sum confirms zero housing units. The audit is preserved in release metadata. No
missing positive-housing tract is dropped or redistributed.

## Limitations

The score represents where 2020 housing units are distributed across Census tracts, not risk
at a specific address. A percentile is a national relative ranking, not a probability or
expected dollar loss. The composite combines FEMA consequence types and should not be read as
one particular hazard. ACS estimates are context only and never affect rank. Census and FEMA
vintages differ by design and are shown in every output.
