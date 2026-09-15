from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_build_store import _install_dimension_fixture, _promote_mountain_fixture
from test_map_assets import write_assets

import househunter.api as api_module
from househunter import __version__
from househunter.api import create_app
from househunter.build import build_snapshot
from househunter.config import RuntimePaths
from househunter.contracts import MapScoreColumns, MapScoreScope
from househunter.dimensions import SUMMARY_DIMENSION_COLUMNS, empty_home_source
from househunter.errors import AmbiguousPlaceError, HouseHunterError
from househunter.geocode import OSM_ATTRIBUTION, AddressMatch, reset_geocode_runtime
from househunter.home_market import is_stale
from househunter.mountain import MOUNTAIN_RUNTIME_COLUMNS


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@pytest.mark.parametrize("path", ["/api/v1", "/api/v1/meta", "/api/v1/jobs/example"])
def test_api_v1_is_removed_for_every_http_method(
    fixture_environment: tuple[RuntimePaths, object], method: str, path: str
) -> None:
    paths, _ = fixture_environment
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.request(method, path)

    assert response.status_code == 404
    if method != "HEAD":
        assert response.json()["detail"] == "HouseHunter API v1 has been removed"


def test_api_filters_details_exports_and_token(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    with TestClient(create_app(paths, testing=True)) as client:
        assert "HouseHunter" in client.get("/").text
        meta_response = client.get("/api/v2/meta")
        assert meta_response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in meta_response.headers["content-security-policy"]
        meta = meta_response.json()
        assert meta["app_version"] == __version__ == "2.0.0"
        assert meta["reference_assets_ready"] is True
        assert meta["methodology"] == (
            "Separate FEMA tract-level and county-level ALR_NPCTL percentiles"
        )
        assert [layer["key"] for layer in meta["layers"]] == [
            "risk",
            "community-conditions",
            "mountain",
            "cost-of-living",
            "home-costs",
        ]
        response = client.get("/api/v2/places", params={"state": "AL"})
        assert response.status_code == 200
        assert [row["place_id"] for row in response.json()["items"]] == [
            "01001000100",
            "01001000200",
            "01001000300",
        ]
        summary = client.get("/api/v2/places/01001000100").json()["summary"]
        assert summary["risk_score"] == 10.0
        assert summary["place_type"] == "tract"
        assert set(summary) == {
            "place_id",
            "name",
            "state",
            "place_type",
            "population_2020",
            "housing_units_2020",
            "risk_score",
            "coverage_status",
            "fema_vintage",
            "census_vintage",
            "county_fips",
            "county_name",
            "community_conditions_group",
            "community_conditions_geography",
            "chrr_release_year",
            *MOUNTAIN_RUNTIME_COLUMNS,
            *SUMMARY_DIMENSION_COLUMNS,
        }
        assert summary["community_conditions_group"] == 5
        assert summary["community_conditions_geography"] == "county"
        assert summary["chrr_release_year"] == 2025
        assert summary["cost_of_living_coverage_status"] == "source_unavailable"
        assert summary["home_costs_coverage_status"] == "source_unavailable"
        assert summary["housing_stock_release_year"] is None
        assert summary["housing_stock_coverage_status"] == "missing_acs"
        assert (
            "alr_npctl_wfir"
            not in client.get("/api/v2/places", params={"state": "AL"}).json()["items"][0]
        )
        tract_detail = client.get("/api/v2/places/01001000100").json()
        hazards = {item["code"]: item for item in tract_detail["hazard_percentiles"]}
        assert len(tract_detail["hazard_percentiles"]) == 18
        assert hazards["WFIR"]["label"] == "Wildfire"
        assert hazards["WFIR"]["percentile"] == 8.0
        assert hazards["TSUN"]["percentile"] is None
        assert tract_detail["member_tract_count"] is None
        assert summary["county_fips"] == "01001"
        assert summary["county_name"] == "Autauga"
        sources = client.get("/api/v2/sources").json()
        assert [source["source"] for source in sources] == [
            "fema",
            "fema_counties",
            "chrr",
            "bea_rpp",
            "home_market",
            "housing_stock",
        ]
        grouped = client.get(
            "/api/v2/counties",
            params={
                "community_conditions_group": 5,
                "sort": "community_conditions_group",
            },
        )
        assert grouped.status_code == 200
        assert [row["place_id"] for row in grouped.json()["items"]] == ["01001"]
        assert (
            client.get("/api/v2/counties", params={"community_conditions_group": 11}).status_code
            == 422
        )
        map_columns = client.get("/api/v2/map/scores", params={"level": "tract"}).json()["columns"]
        assert map_columns["community_conditions_group"][0] == 5
        assert map_columns["mountain_magnitude"][0] is None
        assert "mountain_score" not in map_columns
        assert "coverage_status" not in map_columns
        assert "mountain_coverage_status" not in map_columns
        assert (
            client.get("/api/v2/places", params={"mountain_magnitude_min": 80}).json()["total"] == 0
        )
        unknown = client.get("/api/v2/places/99999999999")
        assert unknown.status_code == 200
        assert unknown.json()["summary"]["state"] == "??"
        assert unknown.json()["summary"]["county_fips"] == "??"
        county_rows = client.get("/api/v2/counties", params={"state": "AL"}).json()
        assert [row["place_id"] for row in county_rows["items"]] == ["01001"]
        assert county_rows["items"][0]["risk_score"] == 40.0
        county_detail = client.get("/api/v2/counties/01001").json()
        assert "ranked among counties" in county_detail["methodology_notice"]
        assert county_detail["summary"]["risk_score"] == 40.0
        assert county_detail["summary"]["housing_stock_release_year"] == 2024
        assert county_detail["member_tract_count"] == 3
        assert county_detail["source_notices"]
        county_hazards = {item["code"]: item for item in county_detail["hazard_percentiles"]}
        assert county_hazards["WFIR"]["percentile"] == 9.0
        assert county_hazards["TSUN"]["percentile"] is None
        filtered = client.get("/api/v2/places", params={"county": "01001"}).json()
        assert [row["place_id"] for row in filtered["items"]] == [
            "01001000100",
            "01001000200",
            "01001000300",
        ]
        assert client.get("/api/v2/places", params={"county": "0100"}).status_code == 400
        assert client.get("/api/v2/places", params={"county": ""}).status_code == 200
        assert client.get("/api/v2/exports/places.parquet").status_code == 200
        assert client.get("/api/v2/exports/counties.parquet").status_code == 200
        place_json = client.get("/api/v2/exports/places.json").json()
        county_json = client.get("/api/v2/exports/counties.json").json()
        assert place_json[0]["place_id"] == "01001000100"
        assert county_json[0]["place_id"] == "01001"
        for row in (place_json[0], county_json[0]):
            assert "community_conditions_group" in row
            assert row["community_conditions_geography"] == "county"
            assert row["chrr_release_year"] == 2025
            assert "mountain_magnitude" in row
            assert "mountain_magnitude_version" in row
            assert "mountain_score" not in row
            assert "mountain_score_version" not in row
        place_header = client.get("/api/v2/exports/places.csv").text.splitlines()[0]
        county_header = client.get("/api/v2/exports/counties.csv").text.splitlines()[0]
        assert "alr_npctl_wfir" in place_header
        assert "alr_npctl_tsun" in place_header
        assert "alr_npctl_wfir" in county_header
        assert "mountain_magnitude" in place_header
        assert "mountain_score" not in place_header
        assert "mountain_magnitude" in county_header
        assert "mountain_score" not in county_header
        assert client.post("/api/v2/jobs", json={"kind": "build"}).status_code == 403
        accepted = client.post(
            "/api/v2/jobs",
            headers={"X-HouseHunter-Token": meta["mutation_token"]},
            json={"kind": "build", "state": "AL"},
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        for _ in range(100):
            status = client.get(f"/api/v2/jobs/{job_id}").json()
            if status["state"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert status["state"] == "succeeded"


def test_api_refreshes_home_market_staleness_across_immutable_build_reuse(
    fixture_environment: tuple[RuntimePaths, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = fixture_environment
    _install_dimension_fixture(monkeypatch)
    release = SimpleNamespace(
        frame=empty_home_source(),
        manifest={
            "month": "2026-08",
            "source_sha256": "a" * 64,
            "logical_sha256": "b" * 64,
        },
        stale=False,
    )
    monkeypatch.setattr(
        "househunter.build.load_release_lock",
        lambda: {
            "source_page": "https://example.test/research",
            "usage_notice": "Personal local use only.",
        },
    )
    monkeypatch.setattr("househunter.build.load_current_release", lambda _: release)
    first = build_snapshot(paths)
    metadata_path = first / "build.json"
    immutable_metadata = metadata_path.read_bytes()
    on_disk = json.loads(immutable_metadata)
    immutable_build_id = on_disk["build_id"]
    assert next(
        source for source in on_disk["sources"] if source["source"] == "home_market"
    )["stale"] is False

    today = {"value": date(2026, 11, 1)}
    monkeypatch.setattr(
        api_module,
        "home_market_is_stale",
        lambda month: is_stale(month, today=today["value"]),
    )
    with TestClient(create_app(paths, testing=True)) as client:
        fresh_meta = client.get("/api/v2/meta").json()
        assert fresh_meta["build"]["build_id"] == immutable_build_id
        fresh_source = next(
            source
            for source in fresh_meta["build"]["sources"]
            if source["source"] == "home_market"
        )
        assert fresh_source["stale"] is False
        fresh_status = next(
            source
            for source in client.get("/api/v2/sources").json()
            if source["source"] == "home_market"
        )
        assert fresh_status["stale"] is False

        today["value"] = date(2026, 11, 2)
        assert build_snapshot(paths) == first
        assert metadata_path.read_bytes() == immutable_metadata
        stale_meta = client.get("/api/v2/meta").json()
        assert stale_meta["build"]["build_id"] == immutable_build_id
        stale_source = next(
            source
            for source in stale_meta["build"]["sources"]
            if source["source"] == "home_market"
        )
        stale_status = next(
            source
            for source in client.get("/api/v2/sources").json()
            if source["source"] == "home_market"
        )
        assert stale_source["stale"] is True
        assert stale_status["stale"] is True
        assert metadata_path.read_bytes() == immutable_metadata


@pytest.mark.parametrize(
    ("params", "status_code"),
    [
        ({"mountain_magnitude_min": "-0.0001"}, 422),
        ({"mountain_magnitude_min": "nan"}, 422),
        ({"mountain_magnitude_min": "inf"}, 400),
        ({"mountain_magnitude_max": "-inf"}, 422),
        ({"mountain_magnitude_min": "2", "mountain_magnitude_max": "1"}, 400),
    ],
)
@pytest.mark.parametrize("resource", ["places", "counties"])
def test_api_rejects_invalid_magnitude_bounds(
    fixture_environment: tuple[RuntimePaths, object],
    resource: str,
    params: dict[str, str],
    status_code: int,
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.get(f"/api/v2/{resource}", params=params)

    assert response.status_code == status_code


@pytest.mark.parametrize("resource", ["places", "counties"])
def test_api_filters_and_sorts_uncapped_mountain_magnitude(
    fixture_environment: tuple[RuntimePaths, Path], resource: str
) -> None:
    paths, root = fixture_environment
    _promote_mountain_fixture(paths, root)
    build_snapshot(paths)

    with TestClient(create_app(paths, testing=True)) as client:
        ranked = client.get(
            f"/api/v2/{resource}",
            params={"sort": "mountain_magnitude", "direction": "desc"},
        )
        exact_zero = client.get(
            f"/api/v2/{resource}",
            params={"mountain_magnitude_min": 0, "mountain_magnitude_max": 0},
        )
        uncapped = client.get(f"/api/v2/{resource}", params={"mountain_magnitude_min": 100_000})
        legacy_sort = client.get(f"/api/v2/{resource}", params={"sort": "mountain_score"})

    assert ranked.status_code == 200
    assert ranked.json()["items"][0]["state"] == "AK"
    assert all(item["mountain_magnitude"] is not None for item in ranked.json()["items"])
    assert exact_zero.status_code == 200
    assert exact_zero.json()["total"] >= 1
    assert uncapped.status_code == 200
    assert uncapped.json()["total"] == 0
    assert legacy_sort.status_code == 400


@pytest.mark.parametrize("resource", ["places", "counties"])
def test_api_combines_new_dimension_filters_and_sorts(
    fixture_environment: tuple[RuntimePaths, Path],
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    paths, _ = fixture_environment
    _install_dimension_fixture(monkeypatch)
    build_snapshot(paths)

    with TestClient(create_app(paths, testing=True)) as client:
        combined = client.get(
            f"/api/v2/{resource}",
            params={
                "max_community_conditions_group": 2,
                "cost_of_living_index_max": 100,
                "home_sqft_for_1m_min": 1500,
                "housing_built_2000_plus_pct_min": 0,
                "include_unranked": True,
            },
        )
        home_ranked = client.get(
            f"/api/v2/{resource}",
            params={"sort": "home_buying_power_percentile", "direction": "desc"},
        )
        impossible = client.get(
            f"/api/v2/{resource}",
            params={
                "community_conditions_group": 5,
                "max_community_conditions_group": 2,
                "include_unranked": True,
            },
        )

    assert combined.status_code == 200
    expected = "02001" if resource == "counties" else "02001000100"
    assert [row["place_id"] for row in combined.json()["items"]] == [expected]
    assert home_ranked.status_code == 200
    assert home_ranked.json()["items"][0]["state"] == "AK"
    assert impossible.status_code == 200
    assert impossible.json()["total"] == 0


@pytest.mark.parametrize(
    ("params", "status_code"),
    [
        ({"cost_of_living_index_min": "nan"}, 422),
        ({"cost_of_living_index_max": "inf"}, 400),
        ({"cost_of_living_index_min": "2", "cost_of_living_index_max": "1"}, 400),
        ({"home_sqft_for_1m_min": "nan"}, 422),
        ({"home_sqft_for_1m_min": "2", "home_sqft_for_1m_max": "1"}, 400),
        ({"housing_built_2000_plus_pct_min": "-0.1"}, 422),
        ({"housing_built_2000_plus_pct_max": "100.1"}, 422),
        (
            {
                "housing_built_2000_plus_pct_min": "2",
                "housing_built_2000_plus_pct_max": "1",
            },
            400,
        ),
    ],
)
@pytest.mark.parametrize("resource", ["places", "counties"])
def test_api_rejects_invalid_dimension_bounds(
    fixture_environment: tuple[RuntimePaths, object],
    resource: str,
    params: dict[str, str],
    status_code: int,
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.get(f"/api/v2/{resource}", params=params)
    assert response.status_code == status_code


def test_map_scores_and_assets_are_complete_ordered_and_safe(
    fixture_environment: tuple[RuntimePaths, object],
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    filename = write_assets(asset_root, monkeypatch)
    monkeypatch.setattr("househunter.api.asset_directory", lambda: asset_root)
    with TestClient(create_app(paths, testing=True)) as client:
        tract = client.get("/api/v2/map/scores", params={"level": "tract"})
        assert tract.status_code == 200
        assert tract.headers["content-encoding"] == "gzip"
        body = tract.json()
        assert body["schema_version"] == 4
        assert body["level"] == "tract"
        assert body["scope"] == {"kind": "national", "state": None}
        assert set(body["columns"]) == {
            "place_id",
            "risk_score",
            "community_conditions_group",
            "mountain_magnitude",
            "cost_of_living_index",
            "home_buying_power_percentile",
            "home_sqft_for_1m",
            "housing_built_2000_plus_pct",
        }
        assert body["columns"]["place_id"] == sorted(body["columns"]["place_id"])
        assert len({len(column) for column in body["columns"].values()}) == 1
        assert len(body["columns"]["place_id"]) == 5
        assert body["columns"]["risk_score"][-1] == 99.0
        assert tract.headers["cache-control"] == "no-store"
        core = client.get("/api/v2/map/scores/core", params={"level": "tract"})
        assert core.status_code == 200
        core_body = core.json()
        assert set(core_body["columns"]) == {
            "place_id",
            "risk_score",
            "community_conditions_group",
            "mountain_magnitude",
        }
        assert core_body["columns"]["place_id"] == body["columns"]["place_id"]
        cost = client.get(core_body["add_ons"]["cost_of_living"])
        home = client.get(core_body["add_ons"]["home_costs"])
        assert cost.status_code == 200
        assert home.status_code == 200
        assert cost.json()["kind"] == "cost-of-living"
        assert home.json()["kind"] == "home-costs"
        assert cost.json()["columns"]["place_id"] == body["columns"]["place_id"]
        assert home.json()["columns"]["place_id"] == body["columns"]["place_id"]
        assert cost.json()["columns"]["cost_of_living_index"] == body["columns"][
            "cost_of_living_index"
        ]
        assert home.json()["columns"]["home_sqft_for_1m"] == body["columns"]["home_sqft_for_1m"]
        assert client.get(
            "/api/v2/map/scores/addons/home-costs",
            params={"level": "tract", "build_id": "stale"},
        ).status_code == 409
        county = client.get("/api/v2/map/scores", params={"level": "county"}).json()
        assert county["columns"]["place_id"] == ["01001", "02001"]
        assert county["columns"]["risk_score"][0] == 40.0
        meta = client.get("/api/v2/meta").json()
        assert meta["map_assets"] == {
            "ready": True,
            "error": None,
            "schema_version": 1,
            "release": "v1.20",
            "manifest_url": "/map-assets/manifest.json",
        }
        manifest = client.get("/map-assets/manifest.json")
        assert manifest.status_code == 200
        assert manifest.headers["cache-control"] == "no-cache"
        asset = client.get(f"/map-assets/{filename}")
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith("application/topo+json")
        assert "immutable" in asset.headers["cache-control"]
        assert client.get("/map-assets/not-listed.topojson.gz").status_code == 404
        assert client.get("/map-assets/%2e%2e%2fsecret").status_code == 404
        (asset_root / filename).write_bytes(b"corrupt")
        corrupt = client.get(f"/map-assets/{filename}")
        assert corrupt.status_code == 503
        assert "wrong size" in corrupt.json()["detail"]

    write_assets(asset_root, monkeypatch)
    with TestClient(create_app(paths, testing=True)) as client:
        manifest = client.get("/map-assets/manifest.json").json()
        filename = manifest["files"][0]["filename"]
        path = asset_root / filename
        original = path.read_bytes()
        stat = path.stat()
        path.write_bytes(original[:9] + bytes([original[9] ^ 1]) + original[10:])
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        changed = client.get(f"/map-assets/{filename}")
        assert changed.status_code == 503
        assert "checksum mismatch" in changed.json()["detail"]

    build_snapshot(paths, state="AL")
    write_assets(asset_root, monkeypatch)
    with TestClient(create_app(paths, testing=True)) as client:
        scoped = client.get("/api/v2/map/scores", params={"level": "tract"}).json()
        assert scoped["scope"] == {"kind": "state", "state": "AL"}
        assert scoped["columns"]["place_id"] == [
            "01001000100",
            "01001000200",
            "01001000300",
        ]
        scoped_meta = client.get("/api/v2/meta").json()["build"]
        chrr_source = next(
            source
            for source in client.get("/api/v2/sources").json()
            if source["source"] == "chrr"
        )
        assert chrr_source["row_count"] == 3
        assert chrr_source["sha256"] == scoped_meta["input_checksums"]["chrr"]
        assert client.get("/api/v2/map/scores", params={"level": "invalid"}).status_code == 422


def test_map_score_addon_serves_the_retained_build_after_pointer_swap(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, fixture_root = fixture_environment
    first = build_snapshot(paths)
    with TestClient(create_app(paths, testing=True)) as client:
        core = client.get("/api/v2/map/scores/core", params={"level": "tract"}).json()
        old_addon_url = core["add_ons"]["cost_of_living"]

        config_path = fixture_root / "sources.yml"
        config = yaml.safe_load(config_path.read_text())
        config["chrr"]["version"] = "2025 Annual Data Release, pointer-swap fixture"
        config_path.write_text(yaml.safe_dump(config))
        second = build_snapshot(paths)

        assert second != first
        retained = client.get(old_addon_url)
        assert retained.status_code == 200
        assert retained.json()["build_id"] == first.name
        assert client.get(
            "/api/v2/map/scores/addons/cost-of-living",
            params={"level": "tract", "build_id": "../../current.json"},
        ).status_code == 409


def test_map_scores_require_a_current_build(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.get("/api/v2/map/scores")
        assert response.status_code == 404
        assert "No published build" in response.json()["detail"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"risk_score": [10.0]},
        {"place_id": ["01001000200", "01001000100"]},
        {"place_id": ["01001000100", "01001000100"]},
    ],
)
def test_map_score_response_model_rejects_misaligned_or_ambiguous_columns(
    overrides: dict[str, list[object]],
) -> None:
    columns: dict[str, list[object]] = {
        "place_id": ["01001000100", "01001000200"],
        "risk_score": [10.0, None],
        "community_conditions_group": [5, None],
        "mountain_magnitude": [None, 2.5],
        "cost_of_living_index": [95.0, None],
        "home_buying_power_percentile": [50.0, None],
        "home_sqft_for_1m": [4000.0, None],
        "housing_built_2000_plus_pct": [25.0, None],
    }
    columns.update(overrides)
    with pytest.raises(ValidationError):
        MapScoreColumns.model_validate(columns)


@pytest.mark.parametrize(
    ("column", "values"),
    [
        ("risk_score", [101.0, None]),
        ("community_conditions_group", [0, None]),
        ("mountain_magnitude", [-0.1, None]),
        ("cost_of_living_index", [0.0, None]),
        ("home_buying_power_percentile", [100.1, None]),
        ("home_sqft_for_1m", [float("inf"), None]),
        ("housing_built_2000_plus_pct", [-0.1, None]),
    ],
)
def test_map_score_response_model_rejects_invalid_dimension_values(
    column: str, values: list[object]
) -> None:
    columns: dict[str, list[object]] = {
        "place_id": ["01001000100", "01001000200"],
        "risk_score": [10.0, None],
        "community_conditions_group": [5, None],
        "mountain_magnitude": [None, 2.5],
        "cost_of_living_index": [95.0, None],
        "home_buying_power_percentile": [50.0, None],
        "home_sqft_for_1m": [4000.0, None],
        "housing_built_2000_plus_pct": [25.0, None],
    }
    columns[column] = values
    with pytest.raises(ValidationError):
        MapScoreColumns.model_validate(columns)


@pytest.mark.parametrize(
    "scope",
    [
        {"kind": "national", "state": "CO"},
        {"kind": "state", "state": None},
        {"kind": "state", "state": "co"},
        {"kind": "national", "state": None, "extra": True},
    ],
)
def test_map_score_response_model_rejects_invalid_scope(scope: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MapScoreScope.model_validate(scope)


def test_api_rejects_hostile_origin(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.get(
            "/api/v2/meta",
            headers={"origin": "https://attacker.example"},
        )
        assert response.status_code == 403
        cross_site = client.get(
            "/api/v2/map/scores",
            headers={"sec-fetch-site": "cross-site"},
        )
        assert cross_site.status_code == 403
        cross_site_asset = client.get(
            "/map-assets/manifest.json",
            headers={"sec-fetch-site": "cross-site"},
        )
        assert cross_site_asset.status_code == 403


def test_api_rejects_non_loopback_host(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    with TestClient(create_app(paths)) as client:
        response = client.get("/api/v2/meta", headers={"host": "attacker.example"})
        assert response.status_code == 400
        malformed = client.get("/api/v2/meta", headers={"host": "localhost:80@attacker.example"})
        assert malformed.status_code == 400


def test_lookup_maps_an_address_to_tract_detail(
    fixture_environment: tuple[RuntimePaths, object], monkeypatch: object
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)

    def fake_geocode(address: str, *, client: object = None) -> AddressMatch:
        query = address.strip()
        if not query:
            raise HouseHunterError("Address is required")
        return AddressMatch(
            query=query,
            matched_address="1 MAIN ST, AUTAUGA, AL, 36003",
            tract_id="01001000100",
        )

    monkeypatch.setattr("househunter.geocode.geocode_tract", fake_geocode)
    with TestClient(create_app(paths, testing=True)) as client:
        empty = client.post("/api/v2/lookup", json={"address": "   "})
        assert empty.status_code == 400
        assert empty.json()["detail"] == "Address is required"
        response = client.post("/api/v2/lookup", json={"address": "1 Main St, Autauga, AL"})
        assert response.status_code == 200
        body = response.json()
        assert body["tract_id"] == "01001000100"
        assert body["matched_address"] == "1 MAIN ST, AUTAUGA, AL, 36003"
        assert body["detail"]["summary"]["risk_score"] == 10.0
        assert "alr_npctl_wfir" not in body["detail"]["summary"]
        assert len(body["detail"]["hazard_percentiles"]) == 18


def test_lookup_reports_a_geoid_missing_from_the_snapshot(
    fixture_environment: tuple[RuntimePaths, object], monkeypatch: object
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    monkeypatch.setattr(
        "househunter.geocode.geocode_tract",
        lambda address, client=None: AddressMatch(
            query=address, matched_address="1 MAIN ST", tract_id="08013012101"
        ),
    )
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.post("/api/v2/lookup", json={"address": "1 Main St, Boulder, CO"})
        assert response.status_code == 400
        assert response.json()["detail"] == "Tract not found: 08013012101"


def test_lookup_returns_candidates_when_matches_disagree(
    fixture_environment: tuple[RuntimePaths, object], monkeypatch: object
) -> None:
    paths, _ = fixture_environment

    def fake_geocode(address: str, *, client: object = None) -> AddressMatch:
        raise AmbiguousPlaceError(
            address,
            [
                {"place_id": "08013012101", "matched_address": "1 MAIN ST, BOULDER, CO"},
                {"place_id": "01001000100", "matched_address": "1 MAIN ST, AUTAUGA, AL"},
            ],
        )

    monkeypatch.setattr("househunter.geocode.geocode_tract", fake_geocode)
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.post("/api/v2/lookup", json={"address": "1 Main St"})
        assert response.status_code == 400
        body = response.json()
        assert body["detail"].startswith("Ambiguous place name:")
        assert [item["place_id"] for item in body["candidates"]] == [
            "08013012101",
            "01001000100",
        ]


def test_lookup_confirms_a_street_fallback_and_rejects_tampering(
    fixture_environment: tuple[RuntimePaths, object], monkeypatch: object
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    reset_geocode_runtime()
    nominatim_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal nominatim_calls
        if request.url.path.endswith("/search"):
            nominatim_calls += 1
            return httpx.Response(
                200,
                json=[
                    {
                        "lat": "32.5",
                        "lon": "-86.5",
                        "display_name": "Lazy Cat Lane, Monument, Colorado, United States",
                        "addresstype": "road",
                        "category": "highway",
                        "address": {"country_code": "us"},
                    }
                ],
            )
        if "coordinates" in request.url.path:
            return httpx.Response(
                200,
                json={"result": {"geographies": {"Census Tracts": [{"GEOID": "01001000100"}]}}},
            )
        return httpx.Response(200, json={"result": {"addressMatches": []}})

    class FakeClient(httpx.Client):
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("househunter.geocode.httpx.Client", FakeClient)
    with TestClient(create_app(paths, testing=True)) as client:
        first = client.post(
            "/api/v2/lookup",
            json={"address": "1720 Lazy Cat Ln, Monument, CO 80132"},
        )
        assert first.status_code == 200
        body = first.json()
        assert body["status"] == "confirmation_required"
        assert body["attribution"] == OSM_ATTRIBUTION
        assert "detail" not in body
        assert "lat" not in body["candidates"][0]
        candidate_id = body["candidates"][0]["candidate_id"]
        tampered = client.post(
            "/api/v2/lookup",
            json={
                "address": "1720 Lazy Cat Ln, Monument, CO 80132",
                "candidate_id": "forged",
            },
        )
        assert tampered.status_code == 400
        confirmed = client.post(
            "/api/v2/lookup",
            json={
                "address": "1720 Lazy Cat Ln, Monument, CO 80132",
                "candidate_id": candidate_id,
            },
        )
        assert confirmed.status_code == 200
        resolved = confirmed.json()
        assert resolved["status"] == "resolved"
        assert resolved["tract_id"] == "01001000100"
        assert resolved["provider"] == "nominatim"
        assert resolved["precision"] == "street"
        assert resolved["approximate"] is True
        assert resolved["detail"]["summary"]["place_id"] == "01001000100"
    assert nominatim_calls == 1


def test_lookup_does_not_fallback_when_census_is_down(
    fixture_environment: tuple[RuntimePaths, object], monkeypatch: object
) -> None:
    paths, _ = fixture_environment
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host or "")
        return httpx.Response(500, text="nope")

    class FakeClient(httpx.Client):
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("househunter.geocode.httpx.Client", FakeClient)
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.post("/api/v2/lookup", json={"address": "1 Main St, Autauga, AL"})
        assert response.status_code == 400
        assert response.json()["detail"] == "Census geocoder request failed"
    assert hosts == ["geocoding.geo.census.gov"]
