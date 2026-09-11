from __future__ import annotations

import json
import runpy
from pathlib import Path

import polars as pl
import pytest

from househunter.config import canonical_json, sha256_bytes
from househunter.errors import HouseHunterError
from househunter.reference import (
    ReferenceAssets,
    reference_asset_status,
    validate_reference_assets,
)

generator = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "generate_reference_assets.py")
)
frame_checksum = generator["frame_checksum"]


def _logical_checksum(frame: pl.DataFrame, sort_by: list[str]) -> str:
    return sha256_bytes(canonical_json([list(row) for row in frame.sort(sort_by).iter_rows()]))


def _assets(tmp_path: Path) -> Path:
    assets = tmp_path / "assets"
    assets.mkdir()
    places = pl.DataFrame(
        {
            "place_id": ["0100001", "0100002"],
            "name": ["Alpha", "Single"],
            "state": ["AL", "AL"],
            "place_type": ["city", "CDP"],
            "population_2020": [1000, 200],
            "housing_units_2020": [100, 20],
        }
    )
    weights = pl.DataFrame(
        {
            "place_id": ["0100001", "0100001", "0100002"],
            "tract_id": ["01001000100", "01001000200", "01001000300"],
            "housing_units": [70, 30, 20],
            "housing_weight": [0.7, 0.3, 1.0],
        },
        schema={
            "place_id": pl.String,
            "tract_id": pl.String,
            "housing_units": pl.Int64,
            "housing_weight": pl.Float64,
        },
    )
    places.write_parquet(assets / "places_2020.parquet")
    weights.write_parquet(assets / "place_tract_weights_2020.parquet")
    metadata = {
        "schema_version": 2,
        "scope": "50 states and District of Columbia",
        "census_decennial_vintage": 2020,
        "row_counts": {
            "places_2020": places.height,
            "place_tract_weights_2020": weights.height,
        },
        "logical_checksums": {
            "places_2020": _logical_checksum(places, ["place_id"]),
            "place_tract_weights_2020": _logical_checksum(weights, ["place_id", "tract_id"]),
        },
    }
    (assets / "reference_metadata.json").write_text(json.dumps(metadata) + "\n")
    return assets


def test_generator_frame_checksum_matches_logical_checksum_for_any_row_order() -> None:
    frame = pl.DataFrame(
        {
            "place_id": ["0100002", "0100001"],
            "name": ["Pe\u00f1a", "Alpha"],
            "population_2020": [200, 1000],
        }
    )

    expected = _logical_checksum(frame, ["place_id"])

    assert frame_checksum(frame, ["place_id"]) == expected
    assert frame_checksum(frame.reverse(), ["place_id"]) == expected


def test_reference_validation_rejects_nonfinite_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _assets(tmp_path)
    monkeypatch.setenv("HOUSEHUNTER_ASSETS_DIR", str(assets))
    weights_path = assets / "place_tract_weights_2020.parquet"
    weights = pl.read_parquet(weights_path).with_columns(
        pl.when(pl.col("place_id") == "0100002")
        .then(pl.lit(float("nan")))
        .otherwise(pl.col("housing_weight"))
        .alias("housing_weight")
    )
    weights.write_parquet(weights_path)
    with pytest.raises(HouseHunterError, match="finite"):
        validate_reference_assets(
            ReferenceAssets(
                places=assets / "places_2020.parquet",
                weights=weights_path,
                metadata=assets / "reference_metadata.json",
            )
        )


def test_reference_validation_rejects_asset_metadata_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _assets(tmp_path)
    monkeypatch.setenv("HOUSEHUNTER_ASSETS_DIR", str(assets))
    places_path = assets / "places_2020.parquet"
    pl.read_parquet(places_path).with_columns(
        (pl.col("population_2020") + 1).alias("population_2020")
    ).write_parquet(places_path)
    with pytest.raises(HouseHunterError, match="checksum differs for places_2020"):
        validate_reference_assets(
            ReferenceAssets(
                places=places_path,
                weights=assets / "place_tract_weights_2020.parquet",
                metadata=assets / "reference_metadata.json",
            )
        )
    present, error = reference_asset_status()
    assert present is True
    assert error and "checksum differs for places_2020" in error
