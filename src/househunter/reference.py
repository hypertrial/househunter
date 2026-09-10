from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from .config import canonical_json, sha256_bytes
from .errors import HouseHunterError


@dataclass(frozen=True)
class ReferenceAssets:
    places: Path
    weights: Path
    metadata: Path


def reference_asset_paths() -> ReferenceAssets:
    configured = os.environ.get("HOUSEHUNTER_ASSETS_DIR")
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(__file__).with_name("assets")
    )
    return ReferenceAssets(
        places=root / "places_2020.parquet",
        weights=root / "place_tract_weights_2020.parquet",
        metadata=root / "reference_metadata.json",
    )


def reference_assets() -> ReferenceAssets:
    assets = reference_asset_paths()
    missing = [str(path) for path in assets.__dict__.values() if not path.is_file()]
    if missing:
        raise HouseHunterError(
            "Packaged Census reference assets are missing: "
            + ", ".join(missing)
            + ". Run scripts/generate_reference_assets.py as documented in DATA_SOURCES.md."
        )
    return assets


def reference_asset_status() -> tuple[bool, str | None]:
    assets = reference_asset_paths()
    if not all(path.is_file() for path in assets.__dict__.values()):
        return False, None
    try:
        validate_reference_assets(assets)
    except HouseHunterError as exc:
        return True, str(exc)
    return True, None


def _require_columns(frame: pl.DataFrame, required: set[str], name: str) -> None:
    missing = required - set(frame.columns)
    if missing:
        raise HouseHunterError(f"{name} is missing columns: {', '.join(sorted(missing))}")


def _logical_checksum(frame: pl.DataFrame, sort_by: list[str]) -> str:
    rows = [list(row) for row in frame.sort(sort_by).iter_rows()]
    return sha256_bytes(canonical_json(rows))


def validate_reference_assets(
    assets: ReferenceAssets,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    try:
        places = pl.read_parquet(assets.places)
        weights = pl.read_parquet(assets.weights)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise HouseHunterError(f"Cannot read Census reference assets: {exc}") from exc
    _require_columns(
        places,
        {"place_id", "name", "state", "place_type", "population_2020", "housing_units_2020"},
        "places_2020",
    )
    _require_columns(
        weights,
        {"place_id", "tract_id", "housing_units", "housing_weight"},
        "place_tract_weights_2020",
    )
    if places["place_id"].n_unique() != places.height:
        raise HouseHunterError("places_2020 place_id values are not unique")
    invalid_places = places.filter(
        pl.col("place_id").is_null() | ~pl.col("place_id").str.contains(r"^\d{7}$")
    )
    if invalid_places.height:
        raise HouseHunterError(f"places_2020 contains {invalid_places.height} invalid place IDs")
    invalid_identity = places.filter(
        pl.col("name").is_null()
        | (pl.col("name").str.len_chars() == 0)
        | pl.col("state").is_null()
        | ~pl.col("state").str.contains(r"^[A-Z]{2}$")
        | pl.col("place_type").is_null()
        | (pl.col("place_type").str.len_chars() == 0)
    )
    if invalid_identity.height:
        raise HouseHunterError("places_2020 contains invalid identity fields")
    if places.filter(
        pl.col("population_2020").is_null()
        | pl.col("housing_units_2020").is_null()
        | (pl.col("population_2020") < 0)
        | (pl.col("housing_units_2020") < 0)
    ).height:
        raise HouseHunterError("places_2020 contains null or negative counts")
    if weights.select(pl.struct(["place_id", "tract_id"]).n_unique()).item() != weights.height:
        raise HouseHunterError("place_tract_weights_2020 contains duplicate Place/tract rows")
    invalid_tracts = weights.filter(
        pl.col("tract_id").is_not_null() & ~pl.col("tract_id").str.contains(r"^\d{11}$")
    )
    if invalid_tracts.height:
        raise HouseHunterError(
            f"place_tract_weights_2020 contains {invalid_tracts.height} invalid tract IDs"
        )
    if weights.filter(
        pl.col("housing_units").is_null()
        | pl.col("housing_weight").is_null()
        | (pl.col("housing_units") <= 0)
        | (pl.col("housing_weight") <= 0)
    ).height:
        raise HouseHunterError("Place/tract weights must have positive housing and weights")
    if weights.filter(
        ~pl.col("housing_weight").is_finite() | (pl.col("housing_weight") > 1)
    ).height:
        raise HouseHunterError("Place/tract weights must be finite and at most 1")
    sums = weights.group_by("place_id").agg(pl.col("housing_weight").sum().alias("weight_sum"))
    bad_sums = sums.filter((pl.col("weight_sum") - 1.0).abs() > 1e-9)
    if bad_sums.height:
        raise HouseHunterError(f"Housing weights do not sum to 1 for {bad_sums.height} Places")
    unknown = weights.join(places.select("place_id"), on="place_id", how="anti")
    if unknown.height:
        raise HouseHunterError(f"Weights reference {unknown['place_id'].n_unique()} unknown Places")
    housing_by_place = weights.group_by("place_id").agg(
        pl.col("housing_units").sum().alias("weighted_housing")
    )
    housing_check = places.join(housing_by_place, on="place_id", how="left").with_columns(
        pl.col("weighted_housing").fill_null(0)
    )
    if housing_check.filter(pl.col("housing_units_2020") != pl.col("weighted_housing")).height:
        raise HouseHunterError("Place housing totals do not match Place/tract housing totals")
    try:
        metadata = json.loads(assets.metadata.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Census reference metadata: {exc}") from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") != 2
        or metadata.get("scope") != "50 states and District of Columbia"
        or metadata.get("census_decennial_vintage") != 2020
    ):
        raise HouseHunterError("Census reference metadata has an unsupported scope or vintage")
    frames = {
        "places_2020": (places, ["place_id"]),
        "place_tract_weights_2020": (weights, ["place_id", "tract_id"]),
    }
    row_counts = metadata.get("row_counts", {})
    checksums = metadata.get("logical_checksums", {})
    for name, (frame, sort_by) in frames.items():
        if row_counts.get(name) != frame.height:
            raise HouseHunterError(f"Census reference row count differs for {name}")
        actual_checksum = _logical_checksum(frame, sort_by)
        if checksums.get(name) != actual_checksum:
            raise HouseHunterError(f"Census reference checksum differs for {name}")
    return places.sort("place_id"), weights.sort(["place_id", "tract_id"])
