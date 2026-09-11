# Release-generated assets

This directory may still hold historical Place parquet files from earlier
HouseHunter releases. Runtime ranking uses FEMA tract and county `ALR_NPCTL`
only and does not read these files. Inspect/detail/export also pass through the
same layers' published `{CODE}_ALR_NPCTL` hazard percentiles. `./scripts/run-app`
does not generate Census assets here or under `data/`.
