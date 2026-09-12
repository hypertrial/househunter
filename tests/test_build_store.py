from __future__ import annotations

import json
from pathlib import Path

import duckdb
import polars as pl
import pytest
import yaml

from househunter.build import BUILD_SCHEMA_VERSION, build_snapshot
from househunter.config import RuntimePaths
from househunter.errors import HouseHunterError
from househunter.geography import STATE_BY_FIPS
from househunter.mountain import IN_SCOPE_STATES, promote_release, write_release
from househunter.store import Store


def _promote_mountain_fixture(paths: RuntimePaths, root: Path) -> None:
    raw = pl.DataFrame(
        {
            "block_geoid": ["010010001001001", "020010001001001"],
            "tract_geoid": ["01001000100", "02001000100"],
            "county_fips": ["01001", "02001"],
            "state": ["AL", "AK"],
            "pop20": [10, 20],
            "relief_5km_m": [100.0, 500.0],
            "relief_10km_m": [150.0, 600.0],
            "relief_20km_m": [200.0, 700.0],
            "relief_40km_m": [250.0, 800.0],
            "rugged_fraction_20km": [0.1, 0.8],
            "public_mountain_access_raw": [1.0, 8.0],
            "trail_access_raw": [0.5, 5.0],
            "open_mountain_km2_5": [1.0, 5.0],
            "open_mountain_km2_15": [2.0, 8.0],
            "open_mountain_km2_30": [3.0, 12.0],
            "restricted_mountain_km2_30": [0.0, 1.0],
            "closed_mountain_km2_30": [0.0, 1.0],
            "unknown_mountain_km2_30": [0.0, 1.0],
            "nearest_mountain_trail_km": [8.0, 1.0],
            "mountain_trail_km_10": [0.5, 4.0],
            "mountain_trail_km_25": [1.0, 6.0],
        }
    )
    additions = []
    for fips, state in STATE_BY_FIPS.items():
        if state not in IN_SCOPE_STATES or state in {"AL", "AK"}:
            continue
        block_geoid = f"{fips}0010001001001"
        additions.append(
            raw.head(1).with_columns(
                pl.lit(block_geoid).alias("block_geoid"),
                pl.lit(block_geoid[:11]).alias("tract_geoid"),
                pl.lit(block_geoid[:5]).alias("county_fips"),
                pl.lit(state).alias("state"),
                pl.lit(1, dtype=pl.Int64).alias("pop20"),
            )
        )
    raw = pl.concat([raw, *additions])
    expectations = {
        row["state"]: {"blocks": row["blocks"], "population": row["population"]}
        for row in raw.group_by("state")
        .agg(pl.len().alias("blocks"), pl.col("pop20").sum().alias("population"))
        .iter_rows(named=True)
    }
    candidate = write_release(
        raw,
        root / "mountain-candidate",
        data_release="fixture-2020",
        sources={
            "items": [
                {
                    "name": "fixture",
                    "url": "https://example.invalid/fixture",
                    "acquired_at": "2026-01-01T00:00:00Z",
                    "crs": "EPSG:5070",
                    "schema": ["fixture"],
                    "count": 1,
                    "filename": "fixture.parquet",
                    "size": 1,
                    "sha256": "0" * 64,
                }
            ]
        },
        national_expectations=expectations,
    )
    promote_release(paths, candidate)


def test_build_is_content_addressed_and_queryable(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    first = build_snapshot(paths)
    first_metadata = json.loads((first / "build.json").read_text())
    second = build_snapshot(paths)
    second_metadata = json.loads((second / "build.json").read_text())
    assert first == second
    assert first_metadata["schema_version"] == BUILD_SCHEMA_VERSION
    assert first_metadata["logical_checksums"] == second_metadata["logical_checksums"]
    with Store(paths) as store:
        rows = store.list_places(limit=10)
        assert [item["place_id"] for item in rows["items"]] == [
            "01001000100",
            "02001000100",
            "01001000200",
            "01001000300",
            "99999999999",
        ]
        assert rows["items"][0]["risk_score"] == 10.0
        assert rows["items"][0]["state"] == "AL"
        assert rows["items"][0]["county_fips"] == "01001"
        assert rows["items"][0]["county_name"] == "Autauga"
        detail = store.place_detail("01001000100")
        assert detail["coverage_ratio"] == 1.0
        assert detail["tract_contributions"][0]["fema_percentile"] == 10.0
        counties = store.list_counties(limit=10)
        assert [item["place_id"] for item in counties["items"]] == ["02001", "01001"]
        assert counties["items"][0]["risk_score"] == 12.0
        assert counties["items"][0]["name"] == "Aleutians East Borough"
        assert counties["items"][1]["risk_score"] == 40.0
        assert counties["items"][1]["risk_score"] != pytest.approx((10.0 + 50.0 + 80.0) / 3)
        filtered = store.list_places(county="01001")
        assert [item["place_id"] for item in filtered["items"]] == [
            "01001000100",
            "01001000200",
            "01001000300",
        ]
        named = store.list_places(search="Autauga")
        assert {item["place_id"] for item in named["items"]} == {
            "01001000100",
            "01001000200",
            "01001000300",
        }


def test_build_identity_includes_source_vintages(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, fixture_root = fixture_environment
    first = build_snapshot(paths)
    config_path = fixture_root / "sources.yml"
    config = yaml.safe_load(config_path.read_text())
    config["chrr"]["version"] = "2025 Annual Data Release, corrected label"
    config_path.write_text(yaml.safe_dump(config))

    second = build_snapshot(paths)

    assert second != first
    metadata = json.loads((second / "build.json").read_text())
    assert metadata["source_vintages"]["chrr"] == config["chrr"]["version"]


def test_build_joins_promoted_mountain_release_and_changes_identity(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, root = fixture_environment
    without_mountain = build_snapshot(paths)
    _promote_mountain_fixture(paths, root)

    with_mountain = build_snapshot(paths)

    assert with_mountain != without_mountain
    metadata = json.loads((with_mountain / "build.json").read_text())
    assert metadata["source_vintages"]["mountain"] == "fixture-2020"
    with Store(paths) as store:
        alabama = store.place_detail("01001000100")["summary"]
        alaska = store.place_detail("02001000100")["summary"]
        assert alabama["mountain_score"] == 0.0
        assert alaska["mountain_score"] > alabama["mountain_score"]
        assert [row["place_id"] for row in store.list_places(mountain_min=1)["items"]] == [
            "02001000100"
        ]


def test_state_build_records_scope(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths, state="AL")
    metadata = json.loads((output / "build.json").read_text())
    assert metadata["scope"] == {"kind": "state", "state": "AL"}
    assert metadata["place_count"] == 3
    assert metadata["county_count"] == 1
    assert metadata["chrr_county_count"] == 1
    assert metadata["chrr_grouped_count"] == 1
    assert (output / "chrr_county.parquet").is_file()
    places = pl.read_parquet(output / "places.parquet")
    counties = pl.read_parquet(output / "counties.parquet")
    assert places["community_conditions_group"].to_list() == [5, 5, 5]
    assert counties["community_conditions_group"].to_list() == [5]
    assert places["community_conditions_geography"].unique().to_list() == ["county"]
    assert places["chrr_release_year"].unique().to_list() == [2025]
    with Store(paths) as store:
        assert all(item["state"] == "AL" for item in store.list_places(limit=10)["items"])
        assert [item["place_id"] for item in store.list_counties()["items"]] == ["01001"]


def test_unknown_fips_prefix_is_retained(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    with Store(paths) as store:
        detail = store.place_detail("99999999999")
        assert detail["summary"]["state"] == "??"
        assert detail["summary"]["risk_score"] == 99.0
        assert detail["summary"]["county_fips"] == "??"
        assert detail["summary"]["county_name"] == "Unknown"


def test_failed_build_preserves_current_pointer(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    published = build_snapshot(paths)
    pointer = paths.current.read_text()
    (paths.cache / "fema_nri_tracts.parquet").unlink()
    with pytest.raises(HouseHunterError, match="FEMA data is not cached"):
        build_snapshot(paths)
    assert paths.current.read_text() == pointer
    assert published.is_dir()


def test_missing_county_cache_fails_build(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    (paths.cache / "fema_nri_counties.parquet").unlink()
    with pytest.raises(HouseHunterError, match="FEMA county data is not cached"):
        build_snapshot(paths)


def test_missing_chrr_cache_fails_build(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    (paths.raw / "chrr" / "community_conditions_2025.json").unlink()
    with pytest.raises(HouseHunterError, match="CHR&R data is not cached"):
        build_snapshot(paths)


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


@pytest.mark.parametrize("schema_version", [2, 3, 4])
def test_legacy_build_schema_is_not_reused(
    fixture_environment: tuple[RuntimePaths, object],
    schema_version: int,
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    metadata_path = output / "build.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["schema_version"] = schema_version
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(HouseHunterError, match="immutable build failed validation"):
        build_snapshot(paths)


def test_unknown_state_is_rejected(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    with pytest.raises(HouseHunterError, match="Unknown state abbreviation"):
        build_snapshot(paths, state="ZZ")


def test_community_conditions_sort_is_deterministic_and_nulls_last(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    connection = duckdb.connect(str(output / "househunter.duckdb"))
    try:
        connection.execute(
            "UPDATE counties SET community_conditions_group = NULL WHERE place_id = '02001'"
        )
    finally:
        connection.close()
    with Store(paths) as store:
        ascending = store.list_counties(
            sort="community_conditions_group", direction="asc", include_unranked=True
        )["items"]
        descending = store.list_counties(
            sort="community_conditions_group", direction="desc", include_unranked=True
        )["items"]
        assert [row["place_id"] for row in ascending] == ["01001", "02001"]
        assert [row["place_id"] for row in descending] == ["01001", "02001"]
        assert store.list_places(community_conditions_group=5)["total"] == 3
