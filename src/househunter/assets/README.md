# Release-generated assets

This directory may still hold historical Place parquet files from earlier
HouseHunter releases. Runtime ranking uses FEMA tract and county `ALR_NPCTL`
only and does not read these files. Inspect/detail/export also pass through the
same layers' published `{CODE}_ALR_NPCTL` hazard percentiles. `./scripts/run-app`
does not generate Census assets here or under `data/`.

A release may include a validated `mountain/` directory containing only the
Mountain manifest plus tract and county Parquet files. The promoted build automatically
generates the same compact runtime fallback under `data/mountain/compact/`; maintainers
use `househunter mountain bundle` to copy an accepted release into this package asset
for installations without managed data. Raw block detail, GIS sources, prepared packs,
and work shards are never bundled.
