from __future__ import annotations

import time
from pathlib import Path

import duckdb
import httpx
from fastapi.testclient import TestClient
from test_map_assets import write_assets

from househunter.api import create_app
from househunter.build import build_snapshot
from househunter.config import RuntimePaths
from househunter.errors import AmbiguousPlaceError, HouseHunterError
from househunter.geocode import OSM_ATTRIBUTION, AddressMatch, reset_geocode_runtime
from househunter.mountain import MOUNTAIN_RUNTIME_COLUMNS


def test_api_filters_details_exports_and_token(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    with TestClient(create_app(paths, testing=True)) as client:
        assert "HouseHunter" in client.get("/").text
        meta_response = client.get("/api/v1/meta")
        assert meta_response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in meta_response.headers["content-security-policy"]
        meta = meta_response.json()
        assert meta["reference_assets_ready"] is True
        assert meta["methodology"] == (
            "Separate FEMA tract-level and county-level ALR_NPCTL percentiles"
        )
        response = client.get("/api/v1/places", params={"state": "AL"})
        assert response.status_code == 200
        assert [row["place_id"] for row in response.json()["items"]] == [
            "01001000100",
            "01001000200",
            "01001000300",
        ]
        summary = client.get("/api/v1/places/01001000100").json()["summary"]
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
        }
        assert summary["community_conditions_group"] == 5
        assert summary["community_conditions_geography"] == "county"
        assert summary["chrr_release_year"] == 2025
        assert (
            "alr_npctl_wfir"
            not in client.get("/api/v1/places", params={"state": "AL"}).json()["items"][0]
        )
        tract_detail = client.get("/api/v1/places/01001000100").json()
        hazards = {item["code"]: item for item in tract_detail["hazard_percentiles"]}
        assert len(tract_detail["hazard_percentiles"]) == 18
        assert hazards["WFIR"]["label"] == "Wildfire"
        assert hazards["WFIR"]["percentile"] == 8.0
        assert hazards["TSUN"]["percentile"] is None
        assert tract_detail["member_tract_count"] is None
        assert summary["county_fips"] == "01001"
        assert summary["county_name"] == "Autauga"
        sources = client.get("/api/v1/sources").json()
        assert [source["source"] for source in sources] == ["fema", "fema_counties", "chrr"]
        grouped = client.get(
            "/api/v1/counties",
            params={
                "community_conditions_group": 5,
                "sort": "community_conditions_group",
            },
        )
        assert grouped.status_code == 200
        assert [row["place_id"] for row in grouped.json()["items"]] == ["01001"]
        assert (
            client.get("/api/v1/counties", params={"community_conditions_group": 11}).status_code
            == 422
        )
        map_rows = client.get("/api/v1/map/scores", params={"level": "tract"}).json()["rows"]
        assert map_rows[0]["community_conditions_group"] == 5
        assert map_rows[0]["mountain_coverage_status"] == "unavailable"
        assert map_rows[0]["mountain_score"] is None
        assert client.get("/api/v1/places", params={"mountain_min": 80}).json()["total"] == 0
        unknown = client.get("/api/v1/places/99999999999")
        assert unknown.status_code == 200
        assert unknown.json()["summary"]["state"] == "??"
        assert unknown.json()["summary"]["county_fips"] == "??"
        county_rows = client.get("/api/v1/counties", params={"state": "AL"}).json()
        assert [row["place_id"] for row in county_rows["items"]] == ["01001"]
        assert county_rows["items"][0]["risk_score"] == 40.0
        county_detail = client.get("/api/v1/counties/01001").json()
        assert "ranked among counties" in county_detail["methodology_notice"]
        assert county_detail["summary"]["risk_score"] == 40.0
        assert county_detail["member_tract_count"] == 3
        county_hazards = {item["code"]: item for item in county_detail["hazard_percentiles"]}
        assert county_hazards["WFIR"]["percentile"] == 9.0
        assert county_hazards["TSUN"]["percentile"] is None
        filtered = client.get("/api/v1/places", params={"county": "01001"}).json()
        assert [row["place_id"] for row in filtered["items"]] == [
            "01001000100",
            "01001000200",
            "01001000300",
        ]
        assert client.get("/api/v1/places", params={"county": "0100"}).status_code == 400
        assert client.get("/api/v1/places", params={"county": ""}).status_code == 200
        assert client.get("/api/v1/exports/places.parquet").status_code == 200
        assert client.get("/api/v1/exports/counties.parquet").status_code == 200
        place_header = client.get("/api/v1/exports/places.csv").text.splitlines()[0]
        county_header = client.get("/api/v1/exports/counties.csv").text.splitlines()[0]
        assert "alr_npctl_wfir" in place_header
        assert "alr_npctl_tsun" in place_header
        assert "alr_npctl_wfir" in county_header
        assert client.post("/api/v1/jobs", json={"kind": "build"}).status_code == 403
        accepted = client.post(
            "/api/v1/jobs",
            headers={"X-HouseHunter-Token": meta["mutation_token"]},
            json={"kind": "build", "state": "AL"},
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        for _ in range(100):
            status = client.get(f"/api/v1/jobs/{job_id}").json()
            if status["state"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert status["state"] == "succeeded"


def test_map_scores_and_assets_are_complete_ordered_and_safe(
    fixture_environment: tuple[RuntimePaths, object],
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    with duckdb.connect(str(output / "househunter.duckdb")) as connection:
        connection.execute(
            "UPDATE places SET risk_score = NULL, coverage_status = 'missing_fema' "
            "WHERE place_id = '99999999999'"
        )
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    filename = write_assets(asset_root, monkeypatch)
    monkeypatch.setattr("househunter.api.asset_directory", lambda: asset_root)
    with TestClient(create_app(paths, testing=True)) as client:
        tract = client.get("/api/v1/map/scores", params={"level": "tract"})
        assert tract.status_code == 200
        assert tract.headers["content-encoding"] == "gzip"
        body = tract.json()
        assert body["schema_version"] == 1
        assert body["level"] == "tract"
        assert body["scope"] == {"kind": "national", "state": None}
        assert [row["place_id"] for row in body["rows"]] == sorted(
            row["place_id"] for row in body["rows"]
        )
        assert len(body["rows"]) == 5
        assert any(row["risk_score"] is None for row in body["rows"])
        county = client.get("/api/v1/map/scores", params={"level": "county"}).json()
        assert [row["place_id"] for row in county["rows"]] == ["01001", "02001"]
        assert county["rows"][0]["risk_score"] == 40.0
        meta = client.get("/api/v1/meta").json()
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

    build_snapshot(paths, state="AL")
    write_assets(asset_root, monkeypatch)
    with TestClient(create_app(paths, testing=True)) as client:
        scoped = client.get("/api/v1/map/scores", params={"level": "tract"}).json()
        assert scoped["scope"] == {"kind": "state", "state": "AL"}
        assert [row["place_id"] for row in scoped["rows"]] == [
            "01001000100",
            "01001000200",
            "01001000300",
        ]
        assert client.get("/api/v1/map/scores", params={"level": "invalid"}).status_code == 422


def test_map_scores_require_a_current_build(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.get("/api/v1/map/scores")
        assert response.status_code == 404
        assert "No published build" in response.json()["detail"]


def test_api_rejects_hostile_origin(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.get(
            "/api/v1/meta",
            headers={"origin": "https://attacker.example"},
        )
        assert response.status_code == 403
        cross_site = client.get(
            "/api/v1/map/scores",
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
        response = client.get("/api/v1/meta", headers={"host": "attacker.example"})
        assert response.status_code == 400
        malformed = client.get("/api/v1/meta", headers={"host": "localhost:80@attacker.example"})
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
        empty = client.post("/api/v1/lookup", json={"address": "   "})
        assert empty.status_code == 400
        assert empty.json()["detail"] == "Address is required"
        response = client.post("/api/v1/lookup", json={"address": "1 Main St, Autauga, AL"})
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
        response = client.post("/api/v1/lookup", json={"address": "1 Main St, Boulder, CO"})
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
        response = client.post("/api/v1/lookup", json={"address": "1 Main St"})
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
            "/api/v1/lookup",
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
            "/api/v1/lookup",
            json={
                "address": "1720 Lazy Cat Ln, Monument, CO 80132",
                "candidate_id": "forged",
            },
        )
        assert tampered.status_code == 400
        confirmed = client.post(
            "/api/v1/lookup",
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
        response = client.post("/api/v1/lookup", json={"address": "1 Main St, Autauga, AL"})
        assert response.status_code == 400
        assert response.json()["detail"] == "Census geocoder request failed"
    assert hosts == ["geocoding.geo.census.gov"]
