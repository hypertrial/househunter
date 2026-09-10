#!/usr/bin/env python3
"""Validate packaged reference assets and the pinned FEMA cache before a release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl

from househunter.build import compute_scores, logical_checksum
from househunter.config import RuntimePaths, load_config
from househunter.download import validate_cached_fema
from househunter.reference import reference_assets, validate_reference_assets


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("src/househunter/assets/reference_validation.json"),
    )
    arguments = parser.parse_args()
    paths = RuntimePaths.from_root()
    assets = reference_assets()
    places, weights = validate_reference_assets(assets)
    metadata = json.loads(assets.metadata.read_text())
    expected_states = {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
    }
    actual_states = set(places["state"].unique())
    if actual_states != expected_states:
        raise ValueError(
            f"Reference state scope differs: {sorted(actual_states ^ expected_states)}"
        )
    for name, frame in {
        "places_2020": places,
        "place_tract_weights_2020": weights,
    }.items():
        sort = ["place_id", "tract_id"] if "tract_id" in frame.columns else ["place_id"]
        actual = logical_checksum(frame, frame.columns, sort)
        expected = metadata["logical_checksums"][name]
        if actual != expected:
            raise ValueError(
                f"Reference logical checksum differs for {name}: {actual} != {expected}"
            )
        if metadata["row_counts"][name] != frame.height:
            raise ValueError(f"Reference row count differs for {name}")
    ct_audit = metadata["connecticut_reconciliation"]
    if set(ct_audit["splits"]) != {"09001990000"}:
        raise ValueError("Connecticut reconciliation audit lacks the sole water-tract split")
    source = load_config()["fema"]
    fema, _ = validate_cached_fema(paths.cache / "fema_nri_tracts.parquet", source)
    scored, _ = compute_scores(places, weights, fema, fema_vintage=source["version"])
    ranked = scored.filter(pl.col("coverage_status") == "complete")
    if ranked.filter(pl.col("coverage_ratio") != 1).height:
        raise ValueError("A ranked Place lacks complete positive-housing coverage")
    ct_weights = weights.filter(pl.col("place_id").str.starts_with("09"))
    missing_ct = ct_weights.join(fema.select("tract_id"), on="tract_id", how="anti")
    if missing_ct.height:
        raise ValueError(f"Connecticut has {missing_ct.height} unmatched positive-housing weights")
    single_tract = weights.group_by("place_id").len().filter(pl.col("len") == 1).select("place_id")
    single_scores = (
        ranked.join(single_tract, on="place_id", how="inner")
        .join(weights.select("place_id", "tract_id"), on="place_id")
        .join(fema.select("tract_id", "alr_npctl"), on="tract_id")
    )
    if single_scores.filter(pl.col("risk_score") != pl.col("alr_npctl")).height:
        raise ValueError("Single-tract identity validation failed")
    report = {
        "places": places.height,
        "positive_housing_intersections": weights.height,
        "ranked_places": ranked.height,
        "unranked_places": scored.height - ranked.height,
        "fema_tracts": fema.height,
        "status": "PASS",
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
