from __future__ import annotations

import json
from pathlib import Path

import duckdb
import polars as pl
import pytest
import yaml

from househunter.build import (
    BUILD_SCHEMA_VERSION,
    MOUNTAIN_RUNTIME_GEOGRAPHY_VERSION,
    SNAPSHOT_FILES,
    _attach_current_mountain,
    _cached_snapshot_artifacts_valid,
    _hazard_values_are_valid,
    build_snapshot,
)
from househunter.config import RuntimePaths
from househunter.dimensions import attach_dimensions as attach_test_dimensions
from househunter.errors import BuildNotFoundError, HouseHunterError
from househunter.geography import STATE_BY_FIPS
from househunter.housing_stock import HousingStockBundle
from househunter.mountain import (
    IN_SCOPE_STATES,
    MOUNTAIN_RUNTIME_COLUMNS,
    national_block_geoid_sha256,
    promote_release,
    write_release,
)
from househunter.store import Store


def _install_dimension_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    rpp = pl.DataFrame(
        {
            "cost_of_living_geography_id": ["00999", "33860"],
            "cost_of_living_geography_name": [
                "U.S. Nonmetropolitan Portion",
                "Montgomery, AL MSA",
            ],
            "cost_of_living_index": [90.0, 110.0],
            "cost_of_living_goods_index": [91.0, 111.0],
            "cost_of_living_housing_rents_index": [92.0, 112.0],
            "cost_of_living_utilities_index": [93.0, 113.0],
            "cost_of_living_other_services_index": [94.0, 114.0],
            "cost_of_living_release_year": [2024, 2024],
        }
    )
    home = pl.DataFrame(
        {
            "county_fips": ["01001", "02001"],
            "home_sqft_for_1m": [1000, 2000],
            "home_buying_power_percentile": [50.0, 100.0],
            "home_median_listing_price": [500000.0, 600000.0],
            "home_median_listing_price_per_square_foot": [1000.0, 500.0],
            "home_median_square_feet": [1200.0, 1400.0],
            "home_active_listing_count": [10.0, 20.0],
            "home_market_month": ["2026-08", "2026-08"],
            "home_costs_coverage_status": ["complete", "complete"],
        }
    )

    def attach(places: pl.DataFrame, counties: pl.DataFrame, **kwargs: object):
        housing = kwargs["housing"]
        assert isinstance(housing, HousingStockBundle)
        alaska_county = housing.counties.filter(pl.col("county_fips") == "01001").with_columns(
            pl.lit("02001").alias("county_fips"),
            pl.lit(60.0).alias("housing_built_2000_plus_pct"),
        )
        alaska_tract = housing.tracts.head(1).with_columns(
            pl.lit("02001000100").alias("tract_id"),
            pl.lit(60.0).alias("housing_built_2000_plus_pct"),
        )
        housing = HousingStockBundle(
            manifest=housing.manifest,
            tracts=pl.concat([housing.tracts, alaska_tract]).sort("tract_id"),
            counties=pl.concat([housing.counties, alaska_county]).sort("county_fips"),
            county_msa=housing.county_msa,
        )
        attached = attach_test_dimensions(
            places,
            counties,
            rpp=rpp,
            home=home,
            housing=housing,
            bea_source={"expected_msa_count": 1, "nonmetropolitan_geofips": "00999"},
            home_attribution="Realtor.com Research Data",
            home_usage_notice="Personal local use only",
        )
        return (
            *attached,
            {"cost_of_living": True, "home_market": True, "housing_stock": True},
            [],
        )

    monkeypatch.setattr("househunter.build.attach_dimensions_fail_open", attach)


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
    raw = pl.concat(
        [
            raw,
            raw.head(1).with_columns(
                pl.lit("010010002001001").alias("block_geoid"),
                pl.lit("01001000200").alias("tract_geoid"),
            ),
            raw.head(1).with_columns(
                pl.lit("010010003001001").alias("block_geoid"),
                pl.lit("01001000300").alias("tract_geoid"),
            ),
        ]
    )
    expectations = {
        row["state"]: {"blocks": row["blocks"], "population": row["population"]}
        for row in raw.group_by("state")
        .agg(pl.len().alias("blocks"), pl.col("pop20").sum().alias("population"))
        .iter_rows(named=True)
    }
    source_item = {
        "name": "fixture",
        "acquired_at": "2026-01-01T00:00:00Z",
        "crs": "EPSG:5070",
        "schema": ["fixture"],
        "count": 1,
        "filename": "fixture.parquet",
        "size": 1,
        "sha256": "0" * 64,
    }
    candidate = write_release(
        raw,
        root / "mountain-candidate",
        data_release="fixture-2020",
        sources={
            "source_lock_schema_version": 2,
            "source_lock_sha256": "1" * 64,
            "items": [source_item],
        },
        national_expectations=expectations,
    )
    promote_release(
        paths,
        candidate,
        reviewed_source_lock={
            "schema_version": 2,
            "expected_states": expectations,
            "block_geoid_sha256": national_block_geoid_sha256(raw),
            "sources": [source_item],
        },
        reviewed_source_lock_sha256="1" * 64,
        expected_raw_blocks=raw,
    )


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
    assert first_metadata["housing_stock_reference"]["available"] is True
    assert first_metadata["housing_stock_reference"]["release_year"] == 2024
    assert first_metadata["housing_stock_reference"]["omb_delineation"] == (
        "OMB Bulletin No. 23-01"
    )
    assert first_metadata["source_vintages"]["omb_delineation"] == "OMB Bulletin No. 23-01"
    assert {warning["source"] for warning in first_metadata["optional_source_warnings"]} == {
        "bea_rpp",
        "home_market",
    }
    assert {layer["key"] for layer in first_metadata["layers"]} == {
        "residential-hazard",
        "community-conditions",
        "mountain",
        "cost-of-living",
        "home-costs",
    }
    layers = {layer["key"]: layer for layer in first_metadata["layers"]}
    assert layers["home-costs"]["availability"] == "unavailable"
    assert "direct tract and county" in layers["home-costs"]["geography"]
    assert "OMB Bulletin No. 23-01" in layers["cost-of-living"]["geography"]
    assert first_metadata["logical_checksums"] == second_metadata["logical_checksums"]
    assert all((first / filename).is_file() for filename in SNAPSHOT_FILES)
    assert paths.data.stat().st_mode & 0o077 == 0
    assert paths.builds.stat().st_mode & 0o077 == 0
    assert first.stat().st_mode & 0o077 == 0
    assert all((first / filename).stat().st_mode & 0o077 == 0 for filename in SNAPSHOT_FILES)
    assert {
        "cost_of_living",
        "home_market",
        "housing_stock_tract",
        "housing_stock_county",
        "housing_stock_county_msa",
    } <= set(first_metadata["logical_checksums"])
    with Store(paths) as store:
        rows = store.list_places(limit=10)
        assert [item["place_id"] for item in rows["items"]] == [
            "01001000100",
            "02001000100",
            "01001000200",
            "01001000300",
        ]
        assert rows["items"][0]["res_hazard_npctl"] == 0.0
        assert rows["items"][0]["alr_npctl"] == 10.0
        assert rows["items"][0]["state"] == "AL"
        assert rows["items"][0]["county_fips"] == "01001"
        assert rows["items"][0]["county_name"] == "Autauga"
        detail = store.place_detail("01001000100")
        assert detail["summary"]["res_hazard_coverage_ratio"] == 1.0
        assert len(detail["hazard_percentiles"]) == 17
        counties = store.list_counties(limit=10)
        assert [item["place_id"] for item in counties["items"]] == ["02001", "01001"]
        assert counties["items"][0]["res_hazard_npctl"] == 0.0
        assert counties["items"][0]["name"] == "Aleutians East Borough"
        assert counties["items"][1]["res_hazard_npctl"] == 100.0
        assert counties["items"][1]["res_hazard_npctl"] != pytest.approx(
            (10.0 + 50.0 + 80.0) / 3
        )
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


def test_unavailable_housing_stock_asset_does_not_block_core_snapshot(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = fixture_environment

    def unavailable() -> None:
        raise HouseHunterError("injected missing housing bundle")

    monkeypatch.setattr("househunter.build.validate_housing_stock_assets", unavailable)
    output = build_snapshot(paths)
    metadata = json.loads((output / "build.json").read_text())

    assert metadata["housing_stock_reference"]["available"] is False
    assert metadata["housing_stock_reference"]["coverage_status"] == "asset_unavailable"
    assert metadata["source_vintages"]["housing_stock"] == "unavailable"
    assert metadata["optional_source_warnings"][0] == {
        "source": "housing_stock",
        "status": "asset_unavailable",
        "message": "injected missing housing bundle",
    }
    assert {warning["source"] for warning in metadata["optional_source_warnings"]} == {
        "housing_stock",
        "bea_rpp",
        "home_market",
    }
    with Store(paths) as store:
        assert store.list_places(limit=1)["items"]


def test_malformed_housing_stock_manifest_does_not_block_core_snapshot(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = fixture_environment

    def malformed() -> None:
        raise KeyError("release_year")

    monkeypatch.setattr("househunter.build.validate_housing_stock_assets", malformed)
    output = build_snapshot(paths)
    metadata = json.loads((output / "build.json").read_text())

    assert metadata["housing_stock_reference"]["coverage_status"] == "asset_unavailable"
    assert "release_year" in metadata["optional_source_warnings"][0]["message"]


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
    assert (
        metadata["source_vintages"]["mountain_runtime_geography"]
        == MOUNTAIN_RUNTIME_GEOGRAPHY_VERSION
    )
    assert metadata["source_vintages"]["mountain_magnitude"] == "mountain_magnitude_v2"
    assert metadata["mountain_magnitude_version"] == "mountain_magnitude_v2"
    assert metadata["mountain_release_id"] == metadata["source_vintages"]["mountain_release_id"]
    with Store(paths) as store:
        alabama = store.place_detail("01001000100")["summary"]
        alaska = store.place_detail("02001000100")["summary"]
        assert alabama["mountain_magnitude"] == 0.0
        assert alaska["mountain_magnitude"] > alabama["mountain_magnitude"]
        assert "mountain_score" not in alabama
        assert [
            row["place_id"] for row in store.list_places(mountain_magnitude_min=1)["items"]
        ] == ["02001000100"]


def test_store_magnitude_filter_sort_ties_nulls_and_map_schema_five(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, root = fixture_environment
    _promote_mountain_fixture(paths, root)
    build_snapshot(paths)

    with Store(paths) as store:
        ascending = store.list_places(
            sort="mountain_magnitude", direction="asc", include_unranked=True
        )["items"]
        descending = store.list_places(
            sort="mountain_magnitude", direction="desc", include_unranked=True
        )["items"]
        assert [row["place_id"] for row in ascending] == [
            "01001000100",
            "01001000200",
            "01001000300",
            "02001000100",
            "99999999999",
        ]
        assert [row["place_id"] for row in descending] == [
            "02001000100",
            "01001000100",
            "01001000200",
            "01001000300",
            "99999999999",
        ]
        assert ascending[-1]["mountain_magnitude"] is None
        assert store.list_places(mountain_magnitude_min=0)["total"] == 4
        assert store.list_places(mountain_magnitude_max=0)["total"] == 3
        exact = store.list_places(
            mountain_magnitude_min=ascending[3]["mountain_magnitude"],
            mountain_magnitude_max=ascending[3]["mountain_magnitude"],
        )
        assert [row["place_id"] for row in exact["items"]] == ["02001000100"]
        with pytest.raises(HouseHunterError, match="Unsupported sort"):
            store.list_places(sort="mountain_score")

        payload = store.map_scores("tract")
        assert payload["schema_version"] == 5
        assert set(payload["columns"]) == {
            "place_id",
            "res_hazard_npctl",
            "community_conditions_group",
            "mountain_magnitude",
            "cost_of_living_index",
            "home_buying_power_percentile",
            "home_sqft_for_1m",
            "housing_built_2000_plus_pct",
        }
        assert "mountain_score" not in payload["columns"]
        assert len({len(values) for values in payload["columns"].values()}) == 1
        core = store.map_scores_core("tract")
        assert set(core["columns"]) == {
            "place_id",
            "res_hazard_npctl",
            "community_conditions_group",
            "mountain_magnitude",
        }
        assert core["columns"]["place_id"] == payload["columns"]["place_id"]
        cost = store.map_scores_addon("tract", "cost-of-living")
        home = store.map_scores_addon("tract", "home-costs")
        assert cost["schema_version"] == home["schema_version"] == 1
        assert cost["columns"]["cost_of_living_index"] == payload["columns"]["cost_of_living_index"]
        assert home["columns"]["home_sqft_for_1m"] == payload["columns"]["home_sqft_for_1m"]
        with pytest.raises(HouseHunterError, match="Map score add-on"):
            store.map_scores_addon("tract", "unknown")


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [
        (-0.0001, None),
        (float("nan"), None),
        (float("inf"), None),
        (-float("inf"), None),
        (2.0, 1.0),
    ],
)
def test_store_rejects_invalid_magnitude_bounds(
    fixture_environment: tuple[RuntimePaths, object],
    minimum: float | None,
    maximum: float | None,
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)

    with Store(paths) as store, pytest.raises(HouseHunterError, match="Magnitude"):
        store.list_places(
            mountain_magnitude_min=minimum,
            mountain_magnitude_max=maximum,
        )


def test_store_combines_dimension_filters_sorts_and_null_rules(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = fixture_environment
    _install_dimension_fixture(monkeypatch)
    build_snapshot(paths)

    with Store(paths) as store:
        best_home = store.list_counties(sort="home_sqft_for_1m", direction="desc")["items"]
        lowest_cost = store.list_counties(sort="cost_of_living_index", direction="asc")["items"]
        combined = store.list_counties(
            max_community_conditions_group=2,
            cost_of_living_index_max=100,
            home_sqft_for_1m_min=1500,
            housing_built_2000_plus_pct_min=0,
            include_unranked=True,
        )
        contradictory_groups = store.list_places(
            community_conditions_group=5,
            max_community_conditions_group=2,
            include_unranked=True,
        )
        explicit_bound = store.list_places(
            home_sqft_for_1m_min=0,
            include_unranked=True,
        )

    assert [row["place_id"] for row in best_home] == ["02001", "01001"]
    assert [row["place_id"] for row in lowest_cost] == ["02001", "01001"]
    assert [row["place_id"] for row in combined["items"]] == ["02001"]
    assert contradictory_groups["total"] == 0
    assert explicit_bound["total"] == 4
    assert all(row["home_sqft_for_1m"] is not None for row in explicit_bound["items"])


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"cost_of_living_index_min": float("nan")}, "Cost of Living"),
        ({"cost_of_living_index_min": 2.0, "cost_of_living_index_max": 1.0}, "minimum"),
        ({"home_sqft_for_1m_max": float("inf")}, "Home square feet"),
        ({"home_sqft_for_1m_min": 2.0, "home_sqft_for_1m_max": 1.0}, "minimum"),
        ({"housing_built_2000_plus_pct_min": -0.1}, "Built-2000"),
        ({"housing_built_2000_plus_pct_max": 100.1}, "Built-2000"),
        (
            {
                "housing_built_2000_plus_pct_min": 2.0,
                "housing_built_2000_plus_pct_max": 1.0,
            },
            "minimum",
        ),
        ({"max_community_conditions_group": 0}, "Maximum Community"),
    ],
)
def test_store_rejects_invalid_dimension_bounds(
    fixture_environment: tuple[RuntimePaths, object],
    kwargs: dict[str, float | int],
    message: str,
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    with Store(paths) as store, pytest.raises(HouseHunterError, match=message):
        store.list_places(**kwargs)


def test_state_snapshot_preserves_national_home_percentile(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = fixture_environment
    _install_dimension_fixture(monkeypatch)

    build_snapshot(paths, state="AK")

    with Store(paths) as store:
        summary = store.county_detail("02001")["summary"]
    assert summary["home_buying_power_percentile"] == 100.0
    assert summary["home_sqft_for_1m"] == 2000


def test_snapshot_and_all_exports_exclude_legacy_mountain_score_fields(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, root = fixture_environment
    _promote_mountain_fixture(paths, root)
    build = build_snapshot(paths)
    forbidden = {"mountain_score", "mountain_score_version"}

    for artifact in ("places.parquet", "counties.parquet"):
        columns = set(pl.read_parquet_schema(build / artifact))
        assert "mountain_magnitude" in columns
        assert "mountain_magnitude_version" in columns
        assert "relief_20km_m" in columns
        assert not forbidden & columns

    connection = duckdb.connect(str(build / "househunter.duckdb"), read_only=True)
    try:
        for table in ("places", "counties"):
            columns = {row[0] for row in connection.execute(f"DESCRIBE {table}").fetchall()}
            assert "mountain_magnitude" in columns
            assert not forbidden & columns
    finally:
        connection.close()

    with Store(paths) as store:
        summary = store.place_detail("01001000100")["summary"]
        assert "mountain_magnitude" in summary
        assert not forbidden & set(summary)
        for format in ("csv", "json", "parquet"):
            output = root / f"places-export.{format}"
            store.export(format, output)
            if format == "csv":
                exported_columns = set(pl.read_csv(output).columns)
            elif format == "json":
                exported_columns = set(json.loads(output.read_text())[0])
            else:
                exported_columns = set(pl.read_parquet_schema(output))
            assert "mountain_magnitude" in exported_columns
            assert not forbidden & exported_columns


def test_state_snapshot_keeps_national_magnitude_calibration(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, root = fixture_environment
    _promote_mountain_fixture(paths, root)

    build_snapshot(paths, state="AK")

    with Store(paths) as store:
        alaska = store.place_detail("02001000100")["summary"]
    assert alaska["mountain_magnitude"] > 1


def test_mountain_runtime_reconciles_connecticut_tracts_and_marks_missing_counties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_root(tmp_path)
    places = pl.DataFrame({"place_id": ["09110100100", "72001000100"], "state": ["CT", "PR"]})
    counties = pl.DataFrame({"place_id": ["09110", "72001"], "state": ["CT", "PR"]})
    ids = [
        "09001100100",
        "09001990000",
        "09007990100",
        "09009990000",
        "09011990100",
    ]
    strings = {"mountain_magnitude_version", "mountain_pipeline_version"}
    mountain_tracts = pl.DataFrame(
        {
            "place_id": ids,
            **{
                column: pl.Series(column, [None] * len(ids), dtype=pl.Float64)
                for column in MOUNTAIN_RUNTIME_COLUMNS
                if column not in strings | {"mountain_coverage_status"}
            },
            "mountain_magnitude": [2.42, None, None, None, None],
            "mountain_magnitude_version": ["mountain_magnitude_v2"] * len(ids),
            "mountain_pipeline_version": ["mountain_pipeline_v1"] * len(ids),
            "mountain_population_coverage": [1.0, 0.0, 0.0, 0.0, 0.0],
            "mountain_coverage_status": ["complete"] + ["zero_population"] * 4,
        }
    ).select("place_id", *MOUNTAIN_RUNTIME_COLUMNS)
    mountain_counties = mountain_tracts.head(0)
    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setattr(
        "househunter.mountain.current_compact_release",
        lambda paths: (
            tmp_path,
            {
                "data_release": "fixture",
                "magnitude_version": "mountain_magnitude_v2",
                "release_id": "1" * 16,
            },
            mountain_tracts,
            mountain_counties,
        ),
    )

    attached, attached_counties, _ = _attach_current_mountain(places, counties, paths)
    assert attached["mountain_magnitude"].to_list() == [2.42, None]
    assert attached["mountain_coverage_status"].to_list() == ["complete", "outside_scope"]
    assert attached_counties["mountain_coverage_status"].to_list() == [
        "unavailable",
        "outside_scope",
    ]

    poisoned = mountain_tracts.with_columns(
        pl.when(pl.col("place_id") == "09001990000")
        .then(pl.lit("complete"))
        .otherwise(pl.col("mountain_coverage_status"))
        .alias("mountain_coverage_status")
    )
    monkeypatch.setattr(
        "househunter.mountain.current_compact_release",
        lambda paths: (
            tmp_path,
            {
                "data_release": "fixture",
                "magnitude_version": "mountain_magnitude_v2",
                "release_id": "1" * 16,
            },
            poisoned,
            mountain_counties,
        ),
    )
    with pytest.raises(HouseHunterError, match="tract exceptions differ"):
        _attach_current_mountain(places, counties, paths)

    with pytest.raises(HouseHunterError, match="missing 1 in-scope"):
        _attach_current_mountain(
            pl.DataFrame({"place_id": ["01001000100"], "state": ["AL"]}),
            pl.DataFrame({"place_id": ["01001"], "state": ["AL"]}),
            paths,
        )

    monkeypatch.setattr("househunter.mountain.current_compact_release", lambda paths: None)
    attached, attached_counties, _ = _attach_current_mountain(places, counties, paths)
    assert attached["mountain_coverage_status"].to_list() == ["unavailable", "outside_scope"]
    assert attached_counties["mountain_coverage_status"].to_list() == [
        "unavailable",
        "outside_scope",
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
        assert detail["summary"]["alr_npctl"] == 99.0
        assert detail["summary"]["res_hazard_npctl"] is None
        assert detail["summary"]["county_fips"] == "??"
        assert detail["summary"]["county_name"] == "Unknown"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("res_hazard_coverage_ratio", 0.5),
        ("res_hazard_data_quality", "unavailable"),
        ("res_hazard_spectral", float("inf")),
        ("alrb_wfir", -1.0),
        ("alrb_npctl_wfir", 101.0),
    ],
)
def test_hazard_artifact_validation_rejects_inconsistent_values(
    fixture_environment: tuple[RuntimePaths, object], column: str, value: object
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    frame = pl.read_parquet(output / "places.parquet").with_columns(
        pl.when(pl.col("place_id") == "01001000100")
        .then(pl.lit(value))
        .otherwise(pl.col(column))
        .alias(column)
    )

    assert not _hazard_values_are_valid(frame)


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


def test_build_and_store_reject_same_count_database_mutation(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    connection = duckdb.connect(str(output / "househunter.duckdb"))
    try:
        connection.execute(
            "UPDATE places SET res_hazard_npctl = 1 WHERE "
            "place_id = (SELECT min(place_id) FROM places)"
        )
    finally:
        connection.close()
    (output / "househunter.duckdb.sha256").write_text("refreshed-but-untrusted\n")
    with pytest.raises(HouseHunterError, match="immutable build failed validation"):
        build_snapshot(paths)
    with pytest.raises(BuildNotFoundError, match="canonical data"):
        Store(paths)


def test_store_caches_validation_until_an_artifact_changes(
    fixture_environment: tuple[RuntimePaths, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import househunter.build as build_module

    paths, _ = fixture_environment
    output = build_snapshot(paths)
    _cached_snapshot_artifacts_valid.cache_clear()
    validations = 0
    original = build_module._validate_snapshot_artifacts

    def counted(target: Path) -> bool:
        nonlocal validations
        validations += 1
        return original(target)

    monkeypatch.setattr(build_module, "_validate_snapshot_artifacts", counted)
    with Store(paths), Store(paths):
        pass
    assert validations == 1

    connection = duckdb.connect(str(output / "househunter.duckdb"))
    try:
        connection.execute(
            "UPDATE places SET res_hazard_npctl = 1 WHERE "
            "place_id = (SELECT min(place_id) FROM places)"
        )
    finally:
        connection.close()
    with pytest.raises(BuildNotFoundError, match="canonical data"):
        Store(paths)
    assert validations == 2


def test_build_and_store_reject_symlinked_database(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    database = output / "househunter.duckdb"
    outside = paths.data.parent / "outside.duckdb"
    database.replace(outside)
    database.symlink_to(outside)

    with pytest.raises(HouseHunterError, match="immutable build failed validation"):
        build_snapshot(paths)
    with pytest.raises(BuildNotFoundError, match="incomplete|unsafe"):
        Store(paths)


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


@pytest.mark.parametrize("schema_version", [2, 3, 4, 10, BUILD_SCHEMA_VERSION - 1])
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


def test_schema_ten_current_pointer_requires_a_rebuild(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    pointer = json.loads(paths.current.read_text())
    pointer["schema_version"] = 10
    paths.current.write_text(json.dumps(pointer))

    with pytest.raises(BuildNotFoundError, match=r"Snapshot schema 10.*rebuild.*schema 11"):
        Store(paths)


def test_unknown_state_is_rejected(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    with pytest.raises(HouseHunterError, match="Unknown state abbreviation"):
        build_snapshot(paths, state="ZZ")


def test_community_conditions_sort_is_deterministic_and_nulls_last(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    raw = paths.raw / "chrr" / "community_conditions_2025.json"
    payload = json.loads(raw.read_text())
    for row in payload["rows"]:
        if row["fipscode"] == "02001":
            row["CommunityConditions_Group"] = None
    raw.write_text(json.dumps(payload) + "\n")
    build_snapshot(paths)
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
