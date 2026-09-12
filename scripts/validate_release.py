#!/usr/bin/env python3
"""Validate pinned FEMA/CHR&R caches and packaged map assets before a release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from househunter.chrr import raw_paths, validate_cached_chrr
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
    chrr, _ = validate_cached_chrr(raw_paths(paths)[0], config["chrr"])
    if config["chrr"]["expected_row_count"] == 3144:
        expected_groups = {
            "01001": 5,
            "02016": 4,
            "02063": None,
            "06075": 3,
            "08013": 2,
            "36061": 2,
        }
        actual_groups = dict(
            chrr.select("county_fips", "community_conditions_group").iter_rows()
        )
        mismatches = {
            fips: actual_groups.get(fips)
            for fips, group in expected_groups.items()
            if actual_groups.get(fips) != group
        }
        if mismatches:
            raise ValueError(f"CHR&R spot checks failed: {mismatches}")
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
        "chrr_counties": chrr.height,
        "chrr_grouped_counties": chrr["community_conditions_group"].is_not_null().sum(),
        "chrr_fema_matches": len(set(chrr["county_fips"]) & set(counties["county_fips"])),
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
