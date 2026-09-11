#!/usr/bin/env python3
"""Validate pinned FEMA caches and packaged map assets before a release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from househunter.config import RuntimePaths, load_config
from househunter.download import validate_cached_fema, validate_cached_fema_counties
from househunter.map_assets import load_manifest, topology_ids


def validate_release(paths: RuntimePaths) -> dict[str, int | str]:
    config = load_config()
    tracts, _ = validate_cached_fema(
        paths.cache / "fema_nri_tracts.parquet", config["fema"]
    )
    counties, _ = validate_cached_fema_counties(
        paths.cache / "fema_nri_counties.parquet", config["fema_counties"]
    )
    manifest = load_manifest()
    if topology_ids(manifest, "tracts-national") != set(tracts["tract_id"]):
        raise ValueError("Map tract identifiers do not match the pinned FEMA cache")
    if topology_ids(manifest, "counties-national") != set(counties["county_fips"]):
        raise ValueError("Map county identifiers do not match the pinned FEMA cache")
    return {
        "tracts": tracts.height,
        "ranked_tracts": tracts["alr_npctl"].is_not_null().sum(),
        "counties": counties.height,
        "ranked_counties": counties["alr_npctl"].is_not_null().sum(),
        "map_assets": len(manifest["files"]),
        "status": "PASS",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    paths = RuntimePaths.from_root()
    report = validate_release(paths)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    output = arguments.report or paths.data / "release_validation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
