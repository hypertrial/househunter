# Data sources and provenance

HouseHunter v1 pins data vintages. It never discovers or accepts an automatic “latest”
release. A source update requires review, validation, and a HouseHunter release.

## FEMA National Risk Index tracts

- Dataset: National Risk Index Census Tracts
- Release: v1.20, December 2025
- ArcGIS item: `9da4eeb936544335a6db0cd7a8448a51`
- Requested fields: `TRACTFIPS`, `ALR_NPCTL`, `NRI_VER`
- Geometry: never requested
- Metric: composite Expected Annual Loss Rate national percentile (`ALR_NPCTL`)
- Cache: `data/cache/fema_nri_tracts.parquet`

Official references: [FEMA technical documentation](https://www.fema.gov/sites/default/files/documents/fema_national-risk-index_technical-documentation.pdf),
[FEMA ArcGIS item](https://www.arcgis.com/home/item.html?id=9da4eeb936544335a6db0cd7a8448a51&sublayer=0),
and [FEMA data resources](https://hazards.fema.gov/nri/data-resources).

## FEMA National Risk Index counties

- Dataset: National Risk Index Counties
- Release: v1.20, December 2025
- ArcGIS item: `39485e8035d446a5bff03259508ae355`
- Requested fields: `STCOFIPS`, `COUNTY`, `COUNTYTYPE`, `STATEABBRV`, `ALR_NPCTL`, `NRI_VER`
- Geometry: never requested
- Metric: FEMA's published **county** `ALR_NPCTL`, ranked among counties
- Cache: `data/cache/fema_nri_counties.parquet`

County names and the county ranking come from this layer. Tract rows join
`TRACTFIPS[:5]` to `STCOFIPS`. HouseHunter does **not** average tract percentiles to
produce a county score. Tract and county percentiles are not comparable.

## Download contract

The downloader validates each layer's edit timestamp, field names and ArcGIS types,
release label, unique identifiers, row count, range `[0, 100]`, and the canonical
logical checksum in `config/sources.yml` when one is pinned. Pages are cached
independently; publication uses a temporary file and atomic rename. The local source
manifest records retrieval time, logical and physical SHA-256 checksums, row count,
schema fingerprint, source URL, and terms URL for each source. On a verified cache
hit, `download` validates that manifest against the cached Parquet and reconstructs
missing or corrupt manifest metadata from the cache file timestamp. If the final
Parquet is corrupt, the pinned remote contract and revision-specific page cache are
used to rebuild it atomically; remote schema or version drift still stops the operation.

State abbreviations for tracts are derived from the first two `TRACTFIPS` digits using
a bundled map. Unknown prefixes are retained as `??` / `Unknown` and are excluded from
known `--state` scopes.

## Census reference assets

Runtime ranking does **not** download or require Census Place/housing assets.
`./scripts/run-app` uses only the pinned FEMA caches.

Maintainer-only Place-generation scripts remain in the tree for historical release assets
and are unused by the local app. Ordinary setup never contacts census.gov.

## Limitations

The score is FEMA's published percentile for that geography, not risk at a specific
address. A percentile is a national relative ranking, not a probability or expected
dollar loss. The composite combines FEMA consequence types and should not be read as
one particular hazard. County scores are not a summary of the tracts inside the county.
