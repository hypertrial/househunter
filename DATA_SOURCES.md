# Data sources and provenance

HouseHunter v1 pins data vintages. It never discovers or accepts an automatic “latest”
release. A source update requires review, validation, and a HouseHunter release.

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
reconstructs missing or corrupt manifest metadata from the cache file timestamp. If the
final Parquet is corrupt, the pinned remote contract and revision-specific page cache are
used to rebuild it atomically; remote schema or version drift still stops the operation.

State abbreviations are derived from the first two `TRACTFIPS` digits using a bundled map.
Unknown prefixes are retained as `??` and are excluded from known `--state` scopes.

Official references: [FEMA technical documentation](https://www.fema.gov/sites/default/files/documents/fema_national-risk-index_technical-documentation.pdf),
[FEMA ArcGIS item](https://www.arcgis.com/home/item.html?id=9da4eeb936544335a6db0cd7a8448a51&sublayer=0),
and [FEMA data resources](https://hazards.fema.gov/nri/data-resources).

## Census reference assets

Runtime ranking does **not** download or require Census Place/housing assets.
`./scripts/run-app` uses only the pinned FEMA cache.

Maintainer-only Place-generation scripts remain in the tree for historical release assets
and are unused by the local app. Ordinary setup never contacts census.gov.

## Limitations

The score is FEMA's published tract percentile, not risk at a specific address. A
percentile is a national relative ranking, not a probability or expected dollar loss. The
composite combines FEMA consequence types and should not be read as one particular hazard.
