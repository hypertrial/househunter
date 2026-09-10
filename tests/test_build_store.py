from __future__ import annotations

import json

import polars as pl
import pytest

from househunter.build import build_snapshot
from househunter.config import RuntimePaths, canonical_json, sha256_bytes
from househunter.errors import AmbiguousPlaceError, HouseHunterError
from househunter.reference import (
    reference_asset_status,
    reference_assets,
    validate_reference_assets,
)
from househunter.store import Store


def test_build_is_content_addressed_and_queryable(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    first = build_snapshot(paths)
    first_metadata = json.loads((first / "build.json").read_text())
    second = build_snapshot(paths)
    second_metadata = json.loads((second / "build.json").read_text())
    assert first == second
    assert first_metadata["logical_checksums"] == second_metadata["logical_checksums"]
    with Store(paths) as store:
        rows = store.list_places(limit=10)
        assert [item["name"] for item in rows["items"]] == ["Alpha", "Single"]
        assert rows["items"][0]["risk_score"] == 22.0
        detail = store.place_detail("0100001")
        assert detail["coverage_ratio"] == 1.0
        assert len(detail["tract_contributions"]) == 2


def test_state_build_records_scope(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths, state="AL")
    metadata = json.loads((output / "build.json").read_text())
    assert metadata["scope"] == {"kind": "state", "state": "AL"}
    assert metadata["place_count"] == 3


def test_ambiguous_place_name_returns_ordered_candidates(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, assets = fixture_environment
    places_path = assets / "places_2020.parquet"
    places = pl.read_parquet(places_path).with_columns(
        pl.when(pl.col("place_id") == "0200001")
        .then(pl.lit("Alpha"))
        .otherwise(pl.col("name"))
        .alias("name")
    )
    places.write_parquet(places_path)
    metadata_path = assets / "reference_metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["logical_checksums"]["places_2020"] = sha256_bytes(
        canonical_json([list(row) for row in places.sort("place_id").iter_rows()])
    )
    metadata_path.write_text(json.dumps(metadata))
    build_snapshot(paths)
    with Store(paths) as store, pytest.raises(AmbiguousPlaceError) as caught:
        store.resolve_place("Alpha")
    assert [candidate["place_id"] for candidate in caught.value.candidates] == [
        "0100001",
        "0200001",
    ]


def test_failed_build_preserves_current_pointer(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, assets = fixture_environment
    published = build_snapshot(paths)
    pointer = paths.current.read_text()
    weights_path = assets / "place_tract_weights_2020.parquet"
    weights = pl.read_parquet(weights_path).with_columns(
        pl.when(pl.col("place_id") == "0100001")
        .then(pl.col("housing_weight") / 2)
        .otherwise(pl.col("housing_weight"))
        .alias("housing_weight")
    )
    weights.write_parquet(weights_path)
    with pytest.raises(HouseHunterError, match="weights do not sum"):
        build_snapshot(paths)
    assert paths.current.read_text() == pointer
    assert published.is_dir()


def test_existing_build_rejects_a_corrupt_database(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    (output / "househunter.duckdb").write_bytes(b"not a database")
    with pytest.raises(HouseHunterError, match="immutable build failed validation"):
        build_snapshot(paths)


def test_export_cannot_overwrite_managed_data(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    protected = output / "places.parquet"
    before = protected.read_bytes()
    with Store(paths) as store, pytest.raises(HouseHunterError, match="managed HouseHunter"):
        store.export("csv", protected)
    assert protected.read_bytes() == before


def test_reference_validation_rejects_nonfinite_weights(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    _, assets = fixture_environment
    weights_path = assets / "place_tract_weights_2020.parquet"
    weights = pl.read_parquet(weights_path).with_columns(
        pl.when(pl.col("place_id") == "0100002")
        .then(pl.lit(float("nan")))
        .otherwise(pl.col("housing_weight"))
        .alias("housing_weight")
    )
    weights.write_parquet(weights_path)
    with pytest.raises(HouseHunterError, match="finite"):
        validate_reference_assets(reference_assets())


def test_reference_validation_rejects_asset_metadata_mismatch(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    _, assets = fixture_environment
    places_path = assets / "places_2020.parquet"
    pl.read_parquet(places_path).with_columns(
        (pl.col("population_2020") + 1).alias("population_2020")
    ).write_parquet(places_path)
    with pytest.raises(HouseHunterError, match="checksum differs for places_2020"):
        validate_reference_assets(reference_assets())
    present, error = reference_asset_status()
    assert present is True
    assert error and "checksum differs for places_2020" in error


def test_legacy_build_schema_is_not_reused(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    metadata_path = output / "build.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["schema_version"] = 1
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(HouseHunterError, match="immutable build failed validation"):
        build_snapshot(paths)
