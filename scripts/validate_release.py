#!/usr/bin/env python3
"""Validate pinned FEMA/CHR&R caches and packaged map and Mountain assets."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import polars as pl

from househunter.chrr import raw_paths, validate_cached_chrr
from househunter.config import RuntimePaths, load_config
from househunter.download import validate_cached_fema, validate_cached_fema_counties
from househunter.map_assets import load_manifest, topology_ids
from househunter.mountain import (
    BUNDLED_COMPACT_RELEASE,
    COMPACT_RELEASE_MAX_BYTES,
    MAGNITUDE_VERSION,
    RELEASE_SCHEMA_VERSION,
    load_compact_release,
)

WESTERN_STATES = {
    "AK",
    "AZ",
    "CA",
    "CO",
    "HI",
    "ID",
    "MT",
    "NV",
    "NM",
    "OR",
    "UT",
    "WA",
    "WY",
}
MAGNITUDE_REGRESSIONS = {
    "tracts": {"count": 83_848, "maximum": 4.9235, "median": 0.88, "p95": 1.94},
    "counties": {"count": 3_143, "maximum": 3.4973, "median": 0.96, "p95": 2.14},
}
COUNTY_MAGNITUDE_ANCHORS = {
    "08097": 3.4973,
    "08117": 2.3213,
    "49051": 2.2421,
    "49035": 1.6109,
    "08013": 1.2349,
}
EXPECTED_MOUNTAIN_RELEASE_ID = "95e0f7fba44f7309"
EXPECTED_MOUNTAIN_FULL_MANIFEST_SHA256 = (
    "e2b4a540d2da501f2980d197696dec766b041fab23e0e35b33357dec5f5c1e94"
)


def validate_bundled_mountain() -> dict[str, object]:
    manifest, tracts, counties = load_compact_release(BUNDLED_COMPACT_RELEASE)
    if (
        manifest.get("schema_version") != RELEASE_SCHEMA_VERSION
        or manifest.get("magnitude_version") != MAGNITUDE_VERSION
    ):
        raise ValueError("Packaged Mountain fallback is not a schema-2 v2 release")
    if (
        manifest.get("release_id") != EXPECTED_MOUNTAIN_RELEASE_ID
        or manifest.get("full_manifest_sha256") != EXPECTED_MOUNTAIN_FULL_MANIFEST_SHA256
    ):
        raise ValueError("Packaged Mountain fallback identity differs from the reviewed release")
    for frame in (tracts, counties):
        if "mountain_score" in frame.columns or "mountain_score_version" in frame.columns:
            raise ValueError("Packaged Mountain fallback exposes legacy score columns")
        if (
            frame["mountain_magnitude_version"].null_count()
            or frame["mountain_magnitude_version"].unique().to_list() != [MAGNITUDE_VERSION]
            or frame["mountain_pipeline_version"].null_count()
            or frame["mountain_pipeline_version"].unique().to_list() != ["mountain_pipeline_v1"]
        ):
            raise ValueError("Packaged Mountain fallback has inconsistent version columns")

    summaries: dict[str, object] = {}
    for label, frame in (("tracts", tracts), ("counties", counties)):
        expected = MAGNITUDE_REGRESSIONS[label]
        artifact = BUNDLED_COMPACT_RELEASE / manifest["files"][label]["filename"]
        geography = pl.read_parquet(artifact, columns=["place_id", "state"])
        scored = frame.join(geography, on="place_id", how="inner").filter(
            pl.col("mountain_magnitude").is_not_null()
        )
        maximum = float(scored["mountain_magnitude"].max())
        if scored.height != expected["count"] or not math.isclose(
            maximum, float(expected["maximum"]), abs_tol=0.0001
        ):
            raise ValueError(f"Packaged Mountain {label} miss pinned count or maximum")
        western = scored.filter(scored["state"].is_in(WESTERN_STATES))["mountain_magnitude"]
        median = float(western.quantile(0.5, interpolation="linear"))
        p95 = float(western.quantile(0.95, interpolation="linear"))
        western_maximum = float(western.max())
        if not math.isclose(median, float(expected["median"]), abs_tol=0.02) or not math.isclose(
            p95, float(expected["p95"]), abs_tol=0.02
        ):
            raise ValueError(f"Packaged Mountain {label} miss pinned western quantiles")
        if p95 - median < 0.75 or western_maximum - p95 < 0.75:
            raise ValueError(f"Packaged Mountain {label} lack required western separation")
        summaries[label] = {
            "scored": scored.height,
            "maximum": maximum,
            "western_median": median,
            "western_p95": p95,
        }

    anchors = dict(
        counties.filter(counties["place_id"].is_in(COUNTY_MAGNITUDE_ANCHORS))
        .select("place_id", "mountain_magnitude")
        .iter_rows()
    )
    if anchors != COUNTY_MAGNITUDE_ANCHORS:
        raise ValueError(f"Packaged Mountain county anchors differ: {anchors}")
    files = manifest["files"]
    compact_bytes = sum(
        (BUNDLED_COMPACT_RELEASE / files[name]["filename"]).stat().st_size
        for name in ("tracts", "counties")
    )
    if compact_bytes > COMPACT_RELEASE_MAX_BYTES:
        raise ValueError("Packaged Mountain fallback exceeds the compact release ceiling")
    return {
        "release_id": manifest["release_id"],
        "schema_version": manifest["schema_version"],
        "magnitude_version": manifest["magnitude_version"],
        "compact_bytes": compact_bytes,
        "regressions": summaries,
        "county_anchors": anchors,
    }


def validate_release(paths: RuntimePaths) -> dict[str, object]:
    config = load_config()
    tracts, _ = validate_cached_fema(paths.cache / "fema_nri_tracts.parquet", config["fema"])
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
        actual_groups = dict(chrr.select("county_fips", "community_conditions_group").iter_rows())
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
    mountain = validate_bundled_mountain()
    return {
        "tracts": tracts.height,
        "ranked_tracts": tracts["alr_npctl"].is_not_null().sum(),
        "counties": counties.height,
        "ranked_counties": counties["alr_npctl"].is_not_null().sum(),
        "chrr_counties": chrr.height,
        "chrr_grouped_counties": chrr["community_conditions_group"].is_not_null().sum(),
        "chrr_fema_matches": len(set(chrr["county_fips"]) & set(counties["county_fips"])),
        "map_assets": len(manifest["files"]),
        "mountain": mountain,
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
