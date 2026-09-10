from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from househunter.config import RuntimePaths, canonical_json, sha256_bytes


def _logical_checksum(frame: pl.DataFrame, sort_by: list[str]) -> str:
    return sha256_bytes(canonical_json([list(row) for row in frame.sort(sort_by).iter_rows()]))


@pytest.fixture
def fixture_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RuntimePaths, Path]:
    assets = tmp_path / "assets"
    assets.mkdir()
    places = pl.DataFrame(
        {
            "place_id": ["0100001", "0100002", "0100003", "0200001", "0200002"],
            "name": ["Alpha", "Single", "Empty", "Missing", "Unmatched"],
            "state": ["AL", "AL", "AL", "AK", "AK"],
            "place_type": ["city", "CDP", "city", "CDP", "CDP"],
            "population_2020": [1000, 200, 0, 100, 100],
            "housing_units_2020": [100, 20, 0, 10, 10],
        }
    )
    weights = pl.DataFrame(
        {
            "place_id": ["0100001", "0100001", "0100002", "0200001", "0200002"],
            "tract_id": ["01001000100", "01001000200", "01001000300", "02001000100", None],
            "housing_units": [70, 30, 20, 10, 10],
            "housing_weight": [0.7, 0.3, 1.0, 1.0, 1.0],
        },
        schema={
            "place_id": pl.String,
            "tract_id": pl.String,
            "housing_units": pl.Int64,
            "housing_weight": pl.Float64,
        },
    )
    acs = pl.DataFrame(
        {
            "place_id": places["place_id"],
            "population_2024": [1100, 220, 0, 105, 95],
            "housing_units_2024": [105, 22, 0, 11, 11],
            "median_home_value_2024": [200000, 250000, None, 180000, 175000],
        }
    )
    places.write_parquet(assets / "places_2020.parquet")
    weights.write_parquet(assets / "place_tract_weights_2020.parquet")
    acs.write_parquet(assets / "acs_2024_context.parquet")
    metadata = {
        "schema_version": 1,
        "scope": "50 states and District of Columbia",
        "census_decennial_vintage": 2020,
        "acs_vintage": 2024,
        "row_counts": {
            "places_2020": places.height,
            "place_tract_weights_2020": weights.height,
            "acs_2024_context": acs.height,
        },
        "logical_checksums": {
            "places_2020": _logical_checksum(places, ["place_id"]),
            "place_tract_weights_2020": _logical_checksum(weights, ["place_id", "tract_id"]),
            "acs_2024_context": _logical_checksum(acs, ["place_id"]),
        },
    }
    (assets / "reference_metadata.json").write_text(json.dumps(metadata) + "\n")
    config = {
        "schema_version": 1,
        "fema": {
            "name": "fixture",
            "item_id": "fixture",
            "layer_url": "https://example.test/FeatureServer/0",
            "item_url": "https://example.test/item",
            "terms_url": "https://example.test/terms",
            "version": "December 2025",
            "release": "v1.20",
            "item_modified_ms": 1,
            "data_last_edit_ms": 1,
            "layer_last_edit_ms": 2,
            "expected_row_count": 4,
            "fields": {
                "TRACTFIPS": "esriFieldTypeString",
                "ALR_NPCTL": "esriFieldTypeDouble",
                "NRI_VER": "esriFieldTypeString",
            },
            "schema_fingerprint": (
                "fdbbc3313928b19a8334cf0883667b996c64e5fc33b956dc42bbff55adf7723e"
            ),
            "canonical_sha256": None,
        },
        "census": {"decennial_vintage": 2020, "acs_vintage": 2024},
    }
    config_path = tmp_path / "sources.yml"
    import yaml

    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("HOUSEHUNTER_ASSETS_DIR", str(assets))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    monkeypatch.setenv("HOUSEHUNTER_DATA_DIR", str(tmp_path / "data"))
    paths = RuntimePaths.from_root(tmp_path)
    paths.ensure()
    fema = pl.DataFrame(
        {
            "tract_id": ["01001000100", "01001000200", "01001000300", "99999999999"],
            "alr_npctl": [10.0, 50.0, 80.0, 99.0],
            "nri_version": ["December 2025"] * 4,
        }
    )
    fema.write_parquet(paths.cache / "fema_nri_tracts.parquet")
    return paths, assets
