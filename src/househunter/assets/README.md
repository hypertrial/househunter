# Release-generated assets

This directory may still hold historical Place parquet files from earlier
HouseHunter releases. Runtime ranking uses the derived tract and county
`RES_HAZARD_NPCTL` from the immutable runtime snapshot and does not read these files.
Inspect/detail/export retain FEMA raw building-loss inputs and HouseHunter's 17 derived
hazard percentiles. `./scripts/dev` does not generate Census assets here or under
`data/`.

A release may include a validated `mountain/` directory containing only the
Mountain manifest plus tract and county Parquet files. The promoted build automatically
generates the same compact runtime fallback under `data/mountain/compact/`; maintainers
use `househunter mountain bundle` to copy an accepted release into this package asset
for installations without managed data. Raw block detail, GIS sources, prepared packs,
and work shards are never bundled.

The `housing_stock/` directory is a separate deterministic ACS 2024 five-year
reference bundle. It contains tract and county B25034/B25035 estimates and
component margins of error, direct-grain built-year shares, and the ACS
summary-level 313 county-to-CBSA relationship used by Cost of Living. Its
manifest binds the two reviewed raw source hashes, the OMB delineation vintage,
and every compact artifact. Runtime preparation only validates and reads this
bundle; raw ACS files and Census network access remain maintainer-only.

The package also carries the reviewed home-market release lock, never Realtor.com
source rows or derived market values. Real imports and normalized releases stay under
the user's untracked runtime data root. `scripts/check_private_data_boundary.py`
inspects the repository, staged index, wheel, and source distribution for forbidden
source signatures, filenames, headers, and derived market fields.
